#!/usr/bin/env python3
"""
dmarc_report.py — DMARC Aggregate Report Collector & Summarizer

Connects to one or more IMAP mailboxes, identifies DMARC aggregate reports,
stores them in SQLite, and sends periodic HTML summary emails.

Usage:
    dmarc_report.py [--config CONFIG] [--mode {collect,summarize,both}]
                    [--since ISO_DATE] [--dry-run] [--verbose]
"""

VERSION = "1.0.0"

import argparse
import email
import email.header
import email.utils
import gzip
import imaplib
import io
import logging
import os
import re
import smtplib
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional
import xml.etree.ElementTree as ET

try:
    import tomllib  # Python 3.11+
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        sys.exit(
            "Error: tomllib not available. Requires Python 3.11+ or: pip install tomli"
        )

logger = logging.getLogger("dmarc_report")

# High-confidence known DMARC report senders — treated as matches even without
# other signals.
KNOWN_SENDERS: set[str] = {
    "reports@fastmaildmarc.com",
    "dmarcreport@microsoft.com",
    "postmaster@amazonses.com",
    "noreply-dmarc-support@google.com",
    "noreply@dmarc.yahoo.com",
}


# ─── Configuration ─────────────────────────────────────────────────────────────


@dataclass
class AccountConfig:
    name: str
    host: str
    port: int
    username: str
    password: str
    folders: list[str] = field(default_factory=list)  # empty → auto-discover
    tls: bool = True
    starttls: bool = False
    timeout: int = 30


@dataclass
class SMTPConfig:
    host: str
    port: int
    from_addr: str
    to_addrs: list[str]
    username: str = ""
    password: str = ""
    mode: str = "starttls"  # starttls | ssl | plain
    timeout: int = 30


@dataclass
class Config:
    accounts: list[AccountConfig]
    smtp: SMTPConfig
    db_path: Path


def _load_passwords(passwords_file: str) -> dict:
    """Load the external JSON passwords file, expanding ~ in the path."""
    import json
    p = Path(passwords_file).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"passwords_file not found: {p}")
    with open(p) as fh:
        return json.load(fh)


def _parse_host_port(host_port: str, default_port: int) -> tuple[str, int]:
    """Split 'hostname:port' into (hostname, port), using default_port if absent."""
    if ":" in host_port:
        host, port_str = host_port.rsplit(":", 1)
        return host, int(port_str)
    return host_port, default_port


def load_config(path: Path) -> Config:
    """
    Load and validate configuration from a TOML file.

    Accounts and the smtp section may reference an external JSON passwords file
    via a [passwords] section + a per-entry ``password_key`` field.  The JSON
    entry is expected to have at least:
        password   — the credential
        imap_host  — "hostname:port"  (for [[account]] entries)
        smtp_host  — "hostname:port"  (for [smtp])
    The username defaults to the key name itself (typically an email address).
    Any value supplied directly in the TOML overrides the passwords-file value.
    """
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    # Load external passwords file if configured
    passwords: dict = {}
    pw_file = raw.get("passwords", {}).get("file", "")
    if pw_file:
        passwords = _load_passwords(pw_file)

    def resolve(section: dict, host_key: str = "imap_host", default_port: int = 993) -> dict:
        """
        Merge passwords-file values into a config section dict.
        `host_key` selects which field in the passwords entry supplies host:port
        ("imap_host" for IMAP accounts, "smtp_host" for SMTP).
        """
        result = dict(section)
        key = section.get("password_key", "")
        if key and key in passwords:
            entry = passwords[key]
            if "host" not in section and host_key in entry:
                h, p = _parse_host_port(entry[host_key], default_port)
                result["host"] = h
                result["port"] = p
            result.setdefault("username", key)
            result.setdefault("password", entry.get("password", ""))
        return result

    accounts: list[AccountConfig] = []
    for acc_raw in raw.get("account", []):
        acc = resolve(acc_raw, host_key="imap_host", default_port=993)
        accounts.append(
            AccountConfig(
                name=acc.get("name", acc.get("host", "unnamed")),
                host=acc["host"],
                port=int(acc.get("port", 993)),
                username=acc["username"],
                password=acc["password"],
                folders=acc.get("folders", []),
                tls=bool(acc.get("tls", True)),
                starttls=bool(acc.get("starttls", False)),
                timeout=int(acc.get("timeout", 30)),
            )
        )

    if not accounts:
        raise ValueError("Config must define at least one [[account]] section.")

    smtp_raw = resolve(raw.get("smtp", {}), host_key="smtp_host", default_port=587)
    # Infer TLS mode from port if not explicitly set
    smtp_port = int(smtp_raw.get("port", 587))
    default_mode = "ssl" if smtp_port == 465 else "starttls"
    smtp = SMTPConfig(
        host=smtp_raw.get("host", "localhost"),
        port=smtp_port,
        from_addr=smtp_raw.get("from_addr", ""),
        to_addrs=smtp_raw.get("to_addrs", []),
        username=smtp_raw.get("username", ""),
        password=smtp_raw.get("password", ""),
        mode=smtp_raw.get("mode", default_mode),
        timeout=int(smtp_raw.get("timeout", 30)),
    )

    db_path = Path(raw.get("database", {}).get("path", "dmarc_reports.db"))

    return Config(accounts=accounts, smtp=smtp, db_path=db_path)


# ─── Database Schema & Helpers ─────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS raw_reports (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id        TEXT    NOT NULL,
    account           TEXT    NOT NULL,
    folder            TEXT    NOT NULL,
    sender            TEXT,
    subject           TEXT,
    date_received     TEXT,
    raw_xml           TEXT,
    report_id         TEXT,
    org_name          TEXT,
    date_range_begin  INTEGER,
    date_range_end    INTEGER,
    processed_at      TEXT    NOT NULL,
    UNIQUE(report_id, org_name)
);

CREATE TABLE IF NOT EXISTS report_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_report_id   INTEGER NOT NULL REFERENCES raw_reports(id) ON DELETE CASCADE,
    source_ip       TEXT,
    count           INTEGER,
    disposition     TEXT,
    dkim_result     TEXT,
    spf_result      TEXT,
    header_from     TEXT,
    envelope_from   TEXT
);

CREATE TABLE IF NOT EXISTS email_headers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_report_id   INTEGER NOT NULL REFERENCES raw_reports(id) ON DELETE CASCADE,
    header_name     TEXT    NOT NULL,
    header_value    TEXT
);

CREATE TABLE IF NOT EXISTS summary_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at         TEXT    NOT NULL,
    report_count    INTEGER NOT NULL,
    record_count    INTEGER NOT NULL,
    period_start    TEXT,
    period_end      TEXT
);
"""


def init_db(db_path: Path) -> sqlite3.Connection:
    """Open (or create) the SQLite database and apply the schema."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    logger.debug("Database ready: %s", db_path)
    return conn


def insert_report(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    account: str,
    folder: str,
    sender: str,
    subject: str,
    date_received: str,
    raw_xml: str,
    report_id: str,
    org_name: str,
    date_range_begin: int,
    date_range_end: int,
) -> Optional[int]:
    """Insert a raw_reports row. Returns the new row id, or None on duplicate."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        cur = conn.execute(
            """
            INSERT INTO raw_reports
                (message_id, account, folder, sender, subject, date_received,
                 raw_xml, report_id, org_name, date_range_begin, date_range_end,
                 processed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                message_id, account, folder, sender, subject, date_received,
                raw_xml, report_id, org_name, date_range_begin, date_range_end,
                now,
            ),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        logger.debug("Duplicate skipped: report_id=%s org=%s", report_id, org_name)
        return None


def insert_record(
    conn: sqlite3.Connection,
    raw_report_id: int,
    *,
    source_ip: str,
    count: int,
    disposition: str,
    dkim_result: str,
    spf_result: str,
    header_from: str,
    envelope_from: str,
) -> None:
    conn.execute(
        """
        INSERT INTO report_records
            (raw_report_id, source_ip, count, disposition, dkim_result,
             spf_result, header_from, envelope_from)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            raw_report_id, source_ip, count, disposition, dkim_result,
            spf_result, header_from, envelope_from,
        ),
    )


def insert_headers(
    conn: sqlite3.Connection, raw_report_id: int, headers: dict[str, str]
) -> None:
    for name, value in headers.items():
        conn.execute(
            "INSERT INTO email_headers (raw_report_id, header_name, header_value) VALUES (?,?,?)",
            (raw_report_id, name, value),
        )


def get_last_summary_time(conn: sqlite3.Connection) -> Optional[str]:
    row = conn.execute(
        "SELECT sent_at FROM summary_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row["sent_at"] if row else None


def get_unsummarized_reports(
    conn: sqlite3.Connection, since: Optional[str] = None
) -> list[sqlite3.Row]:
    """Return raw_reports rows whose processed_at is after `since`."""
    if since is None:
        since = get_last_summary_time(conn) or "1970-01-01T00:00:00+00:00"
    return conn.execute(
        "SELECT * FROM raw_reports WHERE processed_at > ? ORDER BY date_range_begin",
        (since,),
    ).fetchall()


def get_records_for_reports(
    conn: sqlite3.Connection, report_ids: list[int]
) -> list[sqlite3.Row]:
    if not report_ids:
        return []
    placeholders = ",".join("?" * len(report_ids))
    return conn.execute(
        f"SELECT * FROM report_records WHERE raw_report_id IN ({placeholders})",
        report_ids,
    ).fetchall()


def get_headers_for_reports(
    conn: sqlite3.Connection, report_ids: list[int]
) -> dict[int, dict[str, str]]:
    """Return {raw_report_id: {header_name: header_value}} for the given reports."""
    if not report_ids:
        return {}
    placeholders = ",".join("?" * len(report_ids))
    rows = conn.execute(
        f"SELECT raw_report_id, header_name, header_value FROM email_headers "
        f"WHERE raw_report_id IN ({placeholders})",
        report_ids,
    ).fetchall()
    result: dict[int, dict[str, str]] = defaultdict(dict)
    for row in rows:
        result[row["raw_report_id"]][row["header_name"]] = row["header_value"] or ""
    return dict(result)


def log_summary(
    conn: sqlite3.Connection,
    *,
    report_count: int,
    record_count: int,
    period_start: Optional[str],
    period_end: Optional[str],
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO summary_log (sent_at, report_count, record_count, period_start, period_end) VALUES (?,?,?,?,?)",
        (now, report_count, record_count, period_start, period_end),
    )
    conn.commit()


# ─── DMARC Message Classification ─────────────────────────────────────────────


def _decode_header(value: str) -> str:
    """Decode an RFC 2047 encoded header value to a plain string."""
    parts = email.header.decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "ascii", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded)


def classify_dmarc_message(msg: email.message.Message) -> bool:
    """
    Return True if the message is likely a DMARC aggregate report.

    Uses a 2-of-4 signal rule.  Known senders bypass the rule entirely
    and are always treated as high-confidence matches.
    """
    from_raw = _decode_header(msg.get("From", "")).lower()
    subject = _decode_header(msg.get("Subject", "")).lower()
    from_addr = email.utils.parseaddr(from_raw)[1].lower()

    # High-confidence known senders
    if from_addr in KNOWN_SENDERS:
        return True

    signals = 0

    # Signal 1: sender domain/address contains "dmarc"
    if "dmarc" in from_raw:
        signals += 1

    # Signal 2: subject contains keywords
    if any(kw in subject for kw in ("dmarc", "report domain", "submitter")):
        signals += 1

    # Signals from message parts
    attachment_signal = False
    mime_signal = False
    for part in msg.walk():
        ct = part.get_content_type().lower()
        fn = (part.get_filename() or "").lower()

        # Signal 3: attachment name matches dmarc/report patterns
        if not attachment_signal and fn:
            if re.search(r"(dmarc|report)", fn) and fn.endswith(
                (".xml", ".gz", ".zip")
            ):
                signals += 1
                attachment_signal = True

        # Signal 4: MIME type is zip/gzip/xml
        if not mime_signal and ct in (
            "application/zip",
            "application/gzip",
            "application/xml",
            "text/xml",
        ):
            signals += 1
            mime_signal = True

        if signals >= 2:
            return True

    return signals >= 2


# ─── Attachment Extraction & XML Parsing ──────────────────────────────────────


def extract_xml_from_part(part: email.message.Message) -> Optional[str]:
    """
    Try to extract DMARC XML from a single MIME part.
    Handles .zip, .gz, and raw .xml content.
    Returns the XML string, or None if this part doesn't contain usable XML.
    """
    payload = part.get_payload(decode=True)
    if not payload:
        return None

    fn = (part.get_filename() or "").lower()
    ct = part.get_content_type().lower()

    # ZIP archive (may contain the XML)
    if fn.endswith(".zip") or ct == "application/zip":
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as zf:
                for name in zf.namelist():
                    if name.lower().endswith(".xml"):
                        return zf.read(name).decode("utf-8", errors="replace")
        except (zipfile.BadZipFile, Exception) as exc:
            logger.debug("ZIP extraction failed (%s): %s", fn, exc)

    # GZIP file
    if fn.endswith(".gz") or ct == "application/gzip":
        try:
            return gzip.decompress(payload).decode("utf-8", errors="replace")
        except Exception as exc:
            logger.debug("GZIP extraction failed (%s): %s", fn, exc)

    # Plain XML
    if fn.endswith(".xml") or ct in ("application/xml", "text/xml"):
        try:
            return payload.decode("utf-8", errors="replace")
        except Exception as exc:
            logger.debug("XML decode failed (%s): %s", fn, exc)

    return None


def _txt(el: Optional[ET.Element], path: str, default: str = "") -> str:
    """Safely get stripped text from an XML sub-element, or `default`."""
    if el is None:
        return default
    found = el.find(path)
    return found.text.strip() if found is not None and found.text else default


def parse_dmarc_xml(xml_content: str) -> Optional[dict]:
    """
    Parse a DMARC aggregate report XML document (RFC 7489 Appendix C).
    Returns a structured dict, or None on parse failure.
    Tolerates missing optional fields.
    """
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError as exc:
        logger.warning("XML parse error: %s", exc)
        return None

    meta = root.find("report_metadata")
    if meta is None:
        logger.warning("XML missing <report_metadata>")
        return None

    date_range = meta.find("date_range")
    try:
        begin = int(_txt(date_range, "begin") or "0")
        end = int(_txt(date_range, "end") or "0")
    except ValueError:
        begin = end = 0

    policy = root.find("policy_published")
    policy_data: dict[str, str] = {}
    if policy is not None:
        for f in ("domain", "adkim", "aspf", "p", "sp", "pct"):
            policy_data[f] = _txt(policy, f)

    records: list[dict] = []
    for record in root.findall("record"):
        row = record.find("row")
        if row is None:
            continue

        policy_eval = row.find("policy_evaluated")
        identifiers = record.find("identifiers")

        try:
            count = int(_txt(row, "count") or "0")
        except ValueError:
            count = 0

        records.append(
            {
                "source_ip": _txt(row, "source_ip"),
                "count": count,
                "disposition": _txt(policy_eval, "disposition"),
                "dkim_result": _txt(policy_eval, "dkim"),
                "spf_result": _txt(policy_eval, "spf"),
                "header_from": _txt(identifiers, "header_from"),
                "envelope_from": _txt(identifiers, "envelope_from"),
            }
        )

    return {
        "org_name": _txt(meta, "org_name"),
        "report_id": _txt(meta, "report_id"),
        "email": _txt(meta, "email"),
        "date_range_begin": begin,
        "date_range_end": end,
        "policy": policy_data,
        "records": records,
    }


# ─── IMAP Helpers ──────────────────────────────────────────────────────────────


def _imap_connect_once(account: AccountConfig) -> imaplib.IMAP4:
    """Single connection attempt (no retry)."""
    logger.debug("Connecting to %s:%d (tls=%s starttls=%s)",
                 account.host, account.port, account.tls, account.starttls)
    if account.tls and not account.starttls:
        imap: imaplib.IMAP4 = imaplib.IMAP4_SSL(
            account.host, account.port, timeout=account.timeout
        )
    else:
        imap = imaplib.IMAP4(account.host, account.port, timeout=account.timeout)
        if account.starttls:
            imap.starttls()
    imap.login(account.username, account.password)
    return imap


def connect_imap(account: AccountConfig) -> imaplib.IMAP4:
    """Connect to IMAP, retrying once on transient failure."""
    try:
        return _imap_connect_once(account)
    except (imaplib.IMAP4.error, OSError) as exc:
        logger.warning("IMAP connect failed, retrying in 3 s: %s", exc)
        time.sleep(3)
        return _imap_connect_once(account)


def list_imap_folders(imap: imaplib.IMAP4) -> list[str]:
    """Return all folder names visible to the authenticated user."""
    status, lines = imap.list()
    if status != "OK":
        return []
    names: list[str] = []
    for line in lines:
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        # LIST response: (\Flags) "sep" "Name"  or  (\Flags) "sep" Name
        m = re.search(r'"[^"]*"\s+"?([^"]+)"?\s*$', line) or re.search(
            r'"[^"]*"\s+(\S+)\s*$', line
        )
        if m:
            names.append(m.group(1).strip('"'))
    return names


def auto_discover_dmarc_folders(
    imap: imaplib.IMAP4, account: AccountConfig
) -> list[str]:
    """
    Scan all folders for messages that look like DMARC reports.
    Returns candidate folder names for user confirmation.
    """
    all_folders = list_imap_folders(imap)
    logger.info(
        "Auto-discovery: scanning %d folders in account '%s'",
        len(all_folders),
        account.name,
    )
    candidates: list[str] = []

    search_queries = [
        'SUBJECT "DMARC"',
        'SUBJECT "Report Domain"',
        'FROM "dmarc"',
        'FROM "report"',
    ]

    for folder in all_folders:
        try:
            quoted = f'"{folder}"' if " " in folder else folder
            status, _ = imap.select(quoted, readonly=True)
            if status != "OK":
                continue
            for query in search_queries:
                status, data = imap.search(None, query)
                if status == "OK" and data[0] and data[0].strip():
                    n = len(data[0].split())
                    logger.info("  Candidate: %s  (%d match(es) for %s)", folder, n, query)
                    candidates.append(folder)
                    break
        except Exception as exc:
            logger.debug("Error scanning folder '%s': %s", folder, exc)

    return candidates


def _quote_folder(name: str) -> str:
    """IMAP-quote a folder name that may contain spaces."""
    return f'"{name}"' if " " in name or name.startswith("\\") else name


def fetch_new_messages(
    imap: imaplib.IMAP4,
    folder: str,
    account_name: str,
    conn: sqlite3.Connection,
) -> list[dict]:
    """
    Scan `folder` for DMARC-looking messages not yet in the database.
    Returns a list of dicts with keys: msg, message_id.
    """
    status, _ = imap.select(_quote_folder(folder), readonly=True)
    if status != "OK":
        logger.warning("Cannot SELECT folder '%s'", folder)
        return []

    # Pre-filter with IMAP SEARCH to avoid fetching every message
    candidate_nums: set[bytes] = set()
    for query in (
        'SUBJECT "DMARC"',
        'SUBJECT "Report Domain"',
        'SUBJECT "Submitter"',
        'FROM "dmarc"',
        *[f'FROM "{s.split("@")[1]}"' for s in KNOWN_SENDERS],
    ):
        try:
            status, data = imap.search(None, query)
            if status == "OK" and data[0]:
                candidate_nums.update(data[0].split())
        except imaplib.IMAP4.error:
            pass  # not all servers support all SEARCH terms

    if not candidate_nums:
        logger.debug("No candidate messages in '%s'", folder)
        return []

    logger.info(
        "Checking %d candidate message(s) in %s/%s",
        len(candidate_nums),
        account_name,
        folder,
    )

    results: list[dict] = []
    for num in sorted(candidate_nums):
        try:
            # Fetch full RFC822 message
            status, raw_data = imap.fetch(num, "(RFC822)")
            if status != "OK" or not raw_data or raw_data[0] is None:
                continue

            raw_bytes = raw_data[0][1]
            if not isinstance(raw_bytes, bytes):
                continue

            msg = email.message_from_bytes(raw_bytes)

            if not classify_dmarc_message(msg):
                continue

            message_id = msg.get("Message-ID", "").strip() or f"<uid-{num.decode()}-{account_name}>"

            # Skip if already stored
            exists = conn.execute(
                "SELECT id FROM raw_reports WHERE message_id = ? AND account = ?",
                (message_id, account_name),
            ).fetchone()
            if exists:
                logger.debug("Already in DB: %s", message_id)
                continue

            results.append({"msg": msg, "message_id": message_id})

        except Exception as exc:
            logger.warning("Error processing message %s: %s", num, exc)

    return results


# ─── Collect Phase ─────────────────────────────────────────────────────────────


def collect(config: Config, conn: sqlite3.Connection) -> int:
    """
    Connect to every configured IMAP account, identify DMARC reports,
    parse and store them.  Returns the count of newly imported reports.
    """
    total_imported = 0

    for account in config.accounts:
        logger.info("Processing account: %s (%s)", account.name, account.username)
        try:
            imap = connect_imap(account)
        except Exception as exc:
            logger.error("Cannot connect to %s: %s", account.name, exc)
            continue

        try:
            if account.folders:
                folders = account.folders
            else:
                candidates = auto_discover_dmarc_folders(imap, account)
                if not candidates:
                    logger.info("No DMARC candidate folders found in %s", account.name)
                    continue
                print(
                    f"\nAuto-discovered candidate folders in '{account.name}':\n"
                    + "\n".join(f"  • {f}" for f in candidates)
                    + "\n\nAdd 'folders = [...]' to your [[account]] config to skip this prompt."
                    "\nProceeding with all candidates for this run.\n"
                )
                folders = candidates

            for folder in folders:
                try:
                    new_messages = fetch_new_messages(imap, folder, account.name, conn)
                    for item in new_messages:
                        msg = item["msg"]
                        message_id = item["message_id"]

                        # Find and extract XML from attachments
                        xml_content: Optional[str] = None
                        for part in msg.walk():
                            xml_content = extract_xml_from_part(part)
                            if xml_content:
                                break

                        if not xml_content:
                            logger.warning(
                                "No extractable XML in message %s — skipping", message_id
                            )
                            continue

                        parsed = parse_dmarc_xml(xml_content)
                        if not parsed:
                            logger.warning(
                                "XML parse failed for message %s — skipping", message_id
                            )
                            continue

                        sender = _decode_header(msg.get("From", ""))
                        subject = _decode_header(msg.get("Subject", ""))
                        date_received = msg.get("Date", "")

                        report_row_id = insert_report(
                            conn,
                            message_id=message_id,
                            account=account.name,
                            folder=folder,
                            sender=sender,
                            subject=subject,
                            date_received=date_received,
                            raw_xml=xml_content,
                            report_id=parsed["report_id"],
                            org_name=parsed["org_name"],
                            date_range_begin=parsed["date_range_begin"],
                            date_range_end=parsed["date_range_end"],
                        )

                        if report_row_id is None:
                            continue  # natural-key duplicate

                        for rec in parsed["records"]:
                            insert_record(
                                conn,
                                report_row_id,
                                source_ip=rec["source_ip"],
                                count=rec["count"],
                                disposition=rec["disposition"],
                                dkim_result=rec["dkim_result"],
                                spf_result=rec["spf_result"],
                                header_from=rec["header_from"],
                                envelope_from=rec["envelope_from"],
                            )

                        # Store selected email headers
                        stored_headers: dict[str, str] = {}
                        for h in ("From", "To", "Subject", "Date", "Message-ID"):
                            v = msg.get(h, "")
                            if v:
                                stored_headers[h] = _decode_header(v)
                        stored_headers["DKIM-Signature"] = (
                            "present" if msg.get("DKIM-Signature") else "absent"
                        )
                        insert_headers(conn, report_row_id, stored_headers)
                        conn.commit()

                        logger.info(
                            "Imported: %s / %s  (%d record(s))",
                            parsed["org_name"],
                            parsed["report_id"],
                            len(parsed["records"]),
                        )
                        total_imported += 1

                except Exception as exc:
                    logger.error("Error processing folder '%s': %s", folder, exc)

        finally:
            try:
                imap.logout()
            except Exception:
                pass

    return total_imported


# ─── Summarize Phase ───────────────────────────────────────────────────────────


def _ts_to_date(ts: int) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _he(s: str) -> str:
    """Minimal HTML-escape for untrusted strings."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def generate_html_summary(
    reports: list[dict],
    records_by_report: dict[int, list[dict]],
    headers_by_report: dict[int, dict[str, str]],
    db_path: Path,
    total_in_db: int,
) -> str:
    """Build an HTML email body summarising the provided reports."""

    rows: list[dict] = []
    for report in reports:
        rid = report["id"]
        recs = records_by_report.get(rid, [])

        total_vol = sum(r["count"] for r in recs)
        dkim_pass = sum(r["count"] for r in recs if r["dkim_result"] == "pass")
        spf_pass = sum(r["count"] for r in recs if r["spf_result"] == "pass")
        none_d = sum(r["count"] for r in recs if r["disposition"] == "none")
        quar_d = sum(r["count"] for r in recs if r["disposition"] == "quarantine")
        rej_d = sum(r["count"] for r in recs if r["disposition"] == "reject")

        begin = _ts_to_date(report["date_range_begin"] or 0)
        end = _ts_to_date(report["date_range_end"] or 0)
        period = f"{begin} – {end}" if begin and end else begin or end or "—"

        rows.append(
            {
                "id": rid,
                "org": report["org_name"] or "Unknown",
                "period": period,
                "total": total_vol,
                "dkim_pass": dkim_pass,
                "dkim_fail": total_vol - dkim_pass,
                "spf_pass": spf_pass,
                "spf_fail": total_vol - spf_pass,
                "none": none_d,
                "quarantine": quar_d,
                "reject": rej_d,
            }
        )

    # Sort: reject first, then quarantine, then by org name
    rows.sort(key=lambda r: (-(r["reject"] > 0), -(r["quarantine"] > 0), r["org"].lower()))

    def row_style(r: dict) -> str:
        if r["reject"] > 0:
            return ' style="background:#ffe0e0;"'
        if r["quarantine"] > 0:
            return ' style="background:#fff3cd;"'
        return ""

    def cell_style(val: int, warn_color: str) -> str:
        return f' style="color:{warn_color};font-weight:bold;"' if val > 0 else ""

    def result_style(result: str) -> str:
        if result == "pass":
            return ' style="color:green;"'
        if result in ("fail", ""):
            return ' style="color:crimson;font-weight:bold;"'
        return ""

    def disp_style(disp: str) -> str:
        if disp == "reject":
            return ' style="color:crimson;font-weight:bold;"'
        if disp == "quarantine":
            return ' style="color:darkorange;font-weight:bold;"'
        return ""

    table_body = ""
    for r in rows:
        table_body += f"""
        <tr{row_style(r)}>
          <td>{_he(r['org'])}</td>
          <td>{r['period']}</td>
          <td>{r['total']}</td>
          <td>{r['dkim_pass']} / {r['dkim_fail']}</td>
          <td>{r['spf_pass']} / {r['spf_fail']}</td>
          <td>{r['none']}</td>
          <td{cell_style(r['quarantine'], 'darkorange')}>{r['quarantine']}</td>
          <td{cell_style(r['reject'], 'crimson')}>{r['reject']}</td>
        </tr>"""

    if not table_body:
        table_body = (
            '<tr><td colspan="8" style="text-align:center;color:#888;">'
            "No records in this period</td></tr>"
        )

    # ── Failures detail section ───────────────────────────────────────────────
    # Show every record where DKIM or SPF did not pass, grouped by report.
    failure_rows = ""
    for report in reports:
        rid = report["id"]
        recs = records_by_report.get(rid, [])
        failures = [r for r in recs if r["dkim_result"] != "pass" or r["spf_result"] != "pass"]
        if not failures:
            continue

        hdrs = headers_by_report.get(rid, {})
        report_from    = _he(hdrs.get("From", report.get("sender", "") or ""))
        report_subject = _he(hdrs.get("Subject", report.get("subject", "") or ""))
        report_date    = _he(hdrs.get("Date", report.get("date_received", "") or ""))
        report_to      = _he(hdrs.get("To", ""))

        # Report email header summary
        failure_rows += f"""
        <tr style="background:#f8f0ff;">
          <td colspan="7" style="font-weight:bold;font-size:13px;">
            {_he(report['org_name'] or 'Unknown')}
            <span style="font-weight:normal;color:#666;margin-left:12px;">
              From: {report_from} &nbsp; Date: {report_date}
            </span>"""
        if report_to:
            failure_rows += f'<span style="font-weight:normal;color:#666;margin-left:8px;">To: {report_to}</span>'
        if report_subject:
            failure_rows += f'<br><span style="font-weight:normal;font-size:12px;color:#888;">Subject: {report_subject}</span>'
        failure_rows += "</td></tr>"

        for rec in failures:
            failure_rows += f"""
        <tr>
          <td style="padding-left:24px;">{_he(rec['source_ip'] or '—')}</td>
          <td>{rec['count']}</td>
          <td{disp_style(rec['disposition'])}>{_he(rec['disposition'] or '—')}</td>
          <td{result_style(rec['dkim_result'])}>{_he(rec['dkim_result'] or '—')}</td>
          <td{result_style(rec['spf_result'])}>{_he(rec['spf_result'] or '—')}</td>
          <td>{_he(rec['header_from'] or '—')}</td>
          <td>{_he(rec['envelope_from'] or '—')}</td>
        </tr>"""

    if failure_rows:
        failures_section = f"""
<h2 style="font-size:17px;margin-top:28px;">Authentication Failures</h2>
<p style="color:#555;">Records where DKIM or SPF did not pass, with the headers of the report email that contained them.</p>
<table>
  <thead>
    <tr>
      <th>Source IP</th><th>Count</th><th>Disposition</th>
      <th>DKIM</th><th>SPF</th><th>Header From</th><th>Envelope From</th>
    </tr>
  </thead>
  <tbody>
    {failure_rows}
  </tbody>
</table>"""
    else:
        failures_section = '<p style="color:green;margin-top:20px;">&#10003; No authentication failures in this period.</p>'

    total_records = sum(
        r["count"] for recs in records_by_report.values() for r in recs
    )
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>DMARC Summary</title>
<style>
  body  {{ font-family: Arial, sans-serif; font-size: 14px; color: #222; margin: 20px; }}
  h1   {{ font-size: 22px; margin-bottom: 4px; }}
  h2   {{ font-size: 17px; margin-bottom: 4px; }}
  p    {{ margin: 4px 0 12px; color: #555; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 8px; }}
  th, td {{ border: 1px solid #ccc; padding: 7px 10px; text-align: left; }}
  th   {{ background: #f4f4f4; font-weight: bold; }}
  tr:hover {{ filter: brightness(0.97); }}
  .footer {{ margin-top: 20px; font-size: 12px; color: #888; border-top: 1px solid #eee; padding-top: 8px; }}
  .legend span {{ display: inline-block; padding: 2px 6px; margin-right: 8px; border: 1px solid #ccc; }}
</style>
</head>
<body>
<h1>DMARC Aggregate Report Summary</h1>
<p>Generated: {generated_at} &nbsp;|&nbsp;
   Reports in period: <strong>{len(reports)}</strong> &nbsp;|&nbsp;
   Total message volume: <strong>{total_records:,}</strong></p>

<table>
  <thead>
    <tr>
      <th>Organization</th>
      <th>Reporting Period</th>
      <th>Total Volume</th>
      <th>DKIM Pass / Fail</th>
      <th>SPF Pass / Fail</th>
      <th>None</th>
      <th>Quarantine</th>
      <th>Reject</th>
    </tr>
  </thead>
  <tbody>
    {table_body}
  </tbody>
</table>

{failures_section}

<div class="footer">
  <p class="legend">
    Highlights:
    <span style="background:#ffe0e0;">red = reject disposition</span>
    <span style="background:#fff3cd;">yellow = quarantine disposition</span>
  </p>
  <p>Database: {db_path} &nbsp;|&nbsp; Total reports in DB: {total_in_db:,}
     &nbsp;|&nbsp; dmarc_report.py v{VERSION}</p>
</div>
</body>
</html>"""


def _smtp_send_once(smtp_cfg: SMTPConfig, msg: MIMEMultipart) -> None:
    if smtp_cfg.mode == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(
            smtp_cfg.host, smtp_cfg.port, timeout=smtp_cfg.timeout
        )
    else:
        server = smtplib.SMTP(smtp_cfg.host, smtp_cfg.port, timeout=smtp_cfg.timeout)
        if smtp_cfg.mode == "starttls":
            server.starttls()
    try:
        if smtp_cfg.username:
            server.login(smtp_cfg.username, smtp_cfg.password)
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


def smtp_send(smtp_cfg: SMTPConfig, msg: MIMEMultipart) -> None:
    """Send an email, retrying once on transient failure."""
    try:
        _smtp_send_once(smtp_cfg, msg)
    except (smtplib.SMTPException, OSError) as exc:
        logger.warning("SMTP send failed, retrying in 3 s: %s", exc)
        time.sleep(3)
        _smtp_send_once(smtp_cfg, msg)


def summarize(
    config: Config,
    conn: sqlite3.Connection,
    since: Optional[str] = None,
    dry_run: bool = False,
    preview: bool = False,
) -> bool:
    """
    Generate an HTML summary of unsummarized reports and either:
      - send it via SMTP (default)
      - print it to stdout (--dry-run)
      - write to a temp file and open in a browser (--preview)
    Returns True if a summary was generated.
    """
    reports = get_unsummarized_reports(conn, since)

    if not reports:
        logger.info("No new reports to summarize since last summary.")
        return False

    report_ids = [r["id"] for r in reports]
    all_records = get_records_for_reports(conn, report_ids)
    all_headers = get_headers_for_reports(conn, report_ids)

    records_by_report: dict[int, list[dict]] = defaultdict(list)
    for rec in all_records:
        records_by_report[rec["raw_report_id"]].append(dict(rec))

    total_in_db: int = conn.execute("SELECT COUNT(*) FROM raw_reports").fetchone()[0]

    period_start = min((r["processed_at"] for r in reports), default=None)
    period_end = max((r["processed_at"] for r in reports), default=None)

    html = generate_html_summary(
        [dict(r) for r in reports],
        records_by_report,
        all_headers,
        config.db_path,
        total_in_db,
    )

    if dry_run:
        print(html)
        return True

    if preview:
        fd, tmp_path = tempfile.mkstemp(suffix=".html", prefix="dmarc_summary_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(html)
            logger.info("Preview written to %s", tmp_path)
            subprocess.Popen(["xdg-open", tmp_path])
        except Exception as exc:
            logger.error("Failed to open preview: %s", exc)
        return True

    # Validate SMTP config minimally
    if not config.smtp.from_addr or not config.smtp.to_addrs:
        logger.error("SMTP from_addr and to_addrs must be set in config.")
        return False

    mime = MIMEMultipart("alternative")
    mime["Subject"] = (
        f"DMARC Summary — {len(reports)} report(s), "
        f"{sum(len(v) for v in records_by_report.values())} record(s)"
    )
    mime["From"] = config.smtp.from_addr
    mime["To"] = ", ".join(config.smtp.to_addrs)
    mime.attach(MIMEText(html, "html", "utf-8"))

    try:
        smtp_send(config.smtp, mime)
        logger.info("Summary email sent to: %s", config.smtp.to_addrs)
    except Exception as exc:
        logger.error("Failed to send summary email: %s", exc)
        return False

    log_summary(
        conn,
        report_count=len(reports),
        record_count=len(all_records),
        period_start=period_start,
        period_end=period_end,
    )
    return True


# ─── CLI Entry Point ───────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dmarc_report.py",
        description="DMARC Aggregate Report Collector & Summarizer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  dmarc_report                              # collect + summarize (default)
  dmarc_report --collect                    # fetch only
  dmarc_report --summarize                  # summarize only
  dmarc_report --summarize --dry-run        # dump HTML to stdout
  dmarc_report --summarize --preview        # open in browser
  dmarc_report --summarize --since 2024-01-01
  dmarc_report --summarize --since all         # everything from the beginning
  dmarc_report --config /etc/dmarc_report/config.toml -v
""",
    )
    p.add_argument(
        "--config",
        default=os.path.expanduser("~/.config/dmarc_report/config.toml"),
        metavar="FILE",
        help="Path to TOML config file (default: ~/.config/dmarc_report/config.toml)",
    )
    p.add_argument(
        "--collect",
        action="store_true",
        default=False,
        help="Fetch new reports from IMAP (default: on unless --summarize is given alone)",
    )
    p.add_argument(
        "--summarize",
        action="store_true",
        default=False,
        help="Send summary email (default: on unless --collect is given alone)",
    )
    p.add_argument(
        "--since",
        metavar="ISO_DATE",
        help=(
            "Override summary window start: ISO date (e.g. 2024-01-01) "
            "or 'all' to include everything from the beginning of time."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print summary HTML to stdout instead of sending email",
    )
    p.add_argument(
        "--preview",
        action="store_true",
        help="Write summary HTML to a temp file and open it with xdg-open",
    )
    p.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable DEBUG-level logging",
    )
    p.add_argument(
        "--version",
        action="version",
        version=f"dmarc_report {VERSION}",
    )
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )

    config_path = Path(args.config)
    if not config_path.exists():
        logger.error("Config file not found: %s", config_path)
        sys.exit(1)

    try:
        config = load_config(config_path)
    except (KeyError, ValueError, Exception) as exc:
        logger.error("Failed to load config: %s", exc)
        sys.exit(1)

    conn = init_db(config.db_path)

    try:
        do_collect = args.collect or not args.summarize
        do_summarize = args.summarize or not args.collect

        if do_collect:
            count = collect(config, conn)
            logger.info("Collection complete: %d new report(s) imported.", count)

        if do_summarize:
            since = args.since
            if since == "all":
                since = "1970-01-01T00:00:00+00:00"
            elif since and re.match(r"^\d{4}-\d{2}-\d{2}$", since):
                since = since + "T00:00:00+00:00"
            ok = summarize(config, conn, since=since, dry_run=args.dry_run, preview=args.preview)
            if not ok and not args.dry_run:
                logger.info("No summary generated (nothing new to report).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
