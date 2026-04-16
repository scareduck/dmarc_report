# dmarc_report

A Python command-line tool that collects DMARC aggregate reports from IMAP
mailboxes, stores them in a local SQLite database, and delivers periodic HTML
summary emails via SMTP.

Designed to run on-demand or from cron — no daemon, no web framework, no
external database engine.

---

## Requirements

- Python 3.11 or newer
- Standard library only (no third-party packages required on Python 3.11+)

On Python < 3.11, install the `tomli` back-port for TOML config parsing:

```
pip install tomli
```

---

## Installation

```bash
# Clone or copy the repo
git clone https://github.com/yourorg/dmarc_report.git
cd dmarc_report

# Make the script executable
chmod +x dmarc_report.py

# Create your config
cp config.example.toml config.toml
$EDITOR config.toml
```

No `pip install` step is needed on Python 3.11+.

---

## Configuration Reference

All settings live in a single TOML file (default: `config.toml`).

### `[database]`

| Key    | Type   | Default                | Description                                    |
|--------|--------|------------------------|------------------------------------------------|
| `path` | string | `dmarc_reports.db`     | Path to the SQLite database file (created on first run). |

### `[[account]]` *(repeatable)*

| Key        | Type         | Default  | Description |
|------------|--------------|----------|-------------|
| `name`     | string       | host     | Friendly name for log output. |
| `host`     | string       | required | IMAP server hostname. |
| `port`     | integer      | `993`    | IMAP port. |
| `username` | string       | required | Login username. |
| `password` | string       | required | Password or app-specific password. |
| `folders`  | string array | `[]`     | Folders to scan. Empty triggers **auto-discovery** (see below). |
| `tls`      | bool         | `true`   | Use implicit TLS (IMAP4_SSL). |
| `starttls` | bool         | `false`  | Upgrade plain connection with STARTTLS. |
| `timeout`  | integer      | `30`     | IMAP operation timeout in seconds. |

**Auth note:** Password and app-password authentication are supported.
OAuth2/XOAUTH2 is planned as a future extension — see *Out of Scope* below.

### `[smtp]`

| Key          | Type         | Default       | Description |
|--------------|--------------|---------------|-------------|
| `host`       | string       | `localhost`   | SMTP server hostname. |
| `port`       | integer      | `587`         | SMTP port. |
| `mode`       | string       | `starttls`    | One of `starttls`, `ssl`, `plain`. |
| `from_addr`  | string       | required      | Envelope-From address. |
| `to_addrs`   | string array | required      | Recipient(s) for summary emails. |
| `username`   | string       | `""`          | SMTP login (leave blank for unauthenticated). |
| `password`   | string       | `""`          | SMTP password. |
| `timeout`    | integer      | `30`          | SMTP operation timeout in seconds. |

---

## First-Run Walkthrough

1.  **Copy and edit the config:**

    ```bash
    cp config.example.toml config.toml
    ```

    Fill in at minimum: `account.host`, `account.username`, `account.password`,
    `smtp.from_addr`, and `smtp.to_addrs`.

2.  **Run collection in collect-only mode to verify IMAP connectivity:**

    ```bash
    ./dmarc_report.py --mode collect --verbose
    ```

    If `folders` is empty in your config, the tool will scan every folder and
    print a list of candidates. Add the relevant folder names to your config to
    skip this prompt on subsequent runs:

    ```toml
    folders = ["INBOX", "DMARC"]
    ```

3.  **Preview the summary email without sending:**

    ```bash
    ./dmarc_report.py --mode summarize --dry-run | open -f -a Safari
    # or redirect to a file
    ./dmarc_report.py --mode summarize --dry-run > preview.html
    ```

4.  **Send the real summary:**

    ```bash
    ./dmarc_report.py --mode summarize
    ```

5.  **Run both phases together** (this is the default):

    ```bash
    ./dmarc_report.py
    ```

---

## Usage

```
dmarc_report.py [OPTIONS]

Options:
  --config FILE            Path to TOML config file  (default: config.toml)
  --mode {collect,summarize,both}
                           Operation mode             (default: both)
  --since ISO_DATE         Override summary window start (e.g. 2024-06-01).
                           Forces re-summarisation from that date forward.
  --dry-run                Print summary HTML to stdout; do not send email.
  --verbose, -v            Enable DEBUG-level logging.
  --version                Show version and exit.
  -h, --help               Show this help message and exit.
```

### Modes

| Mode        | Description |
|-------------|-------------|
| `collect`   | Connect to IMAP, download new DMARC reports, store in the database. No email sent. |
| `summarize` | Generate an HTML summary of reports collected since the last summary and email it. |
| `both`      | Run `collect` then `summarize` in sequence. This is the default. |

---

## Cron Examples

```crontab
# Collect DMARC reports daily at 02:00
0 2 * * *  /path/to/dmarc_report.py --mode collect \
           --config /etc/dmarc_report/config.toml >> /var/log/dmarc_collect.log 2>&1

# Send weekly summary every Monday at 08:00
0 8 * * 1  /path/to/dmarc_report.py --mode summarize \
           --config /etc/dmarc_report/config.toml >> /var/log/dmarc_summary.log 2>&1
```

Or collect and summarize in one daily run:

```crontab
# Collect + summarize every day at 06:00
0 6 * * *  /path/to/dmarc_report.py \
           --config /etc/dmarc_report/config.toml >> /var/log/dmarc_report.log 2>&1
```

---

## How DMARC Messages Are Identified

A message is classified as a DMARC aggregate report if it satisfies **two or
more** of the following signals:

1. Sender domain or address contains `dmarc` (case-insensitive)
2. Subject contains `dmarc`, `Report Domain`, or `Submitter`
3. Attachment filename matches `*dmarc*` or `*report*` and ends in `.xml`, `.gz`, or `.zip`
4. MIME type is `application/zip`, `application/gzip`, `application/xml`, or `text/xml`

Additionally, messages from the following addresses are treated as
**high-confidence** matches regardless of other signals:

- `reports@fastmaildmarc.com`
- `dmarcreport@microsoft.com`
- `postmaster@amazonses.com`
- `noreply-dmarc-support@google.com`
- `noreply@dmarc.yahoo.com`

---

## Database Schema

See [`schema.sql`](schema.sql) for annotated DDL.  Four tables:

| Table           | Contents |
|-----------------|----------|
| `raw_reports`   | One row per DMARC report email (report metadata + raw XML). |
| `report_records`| One row per `<record>` in each report (IP, counts, pass/fail). |
| `email_headers` | Selected email headers per report (From, Subject, Date, etc.). |
| `summary_log`   | Tracks when summary emails were sent and the window they covered. |

Deduplication uses `(report_id, org_name)` as a natural key — re-running
against the same mailbox is safe.

---

## Troubleshooting

**IMAP login fails**
: Check host/port/credentials. Many providers require an *app-specific
  password* rather than your account password. Ensure `tls = true` for
  port 993, or `starttls = true` for port 143.

**No reports found (auto-discovery)**
: Run with `--verbose` to see folder scan output. Some providers use
  non-standard folder names. Check your spam folder too.

**Summary email not delivered**
: Try `--dry-run` first to confirm HTML is generated. Then check SMTP
  credentials, `mode` (starttls vs ssl), and that `from_addr` is
  authorised on your mail server.

**`tomllib` not found**
: Upgrade to Python 3.11+, or install: `pip install tomli`

---

## Out of Scope (future work)

The following are explicitly **not** implemented in v1.0:

- **OAuth2 / XOAUTH2 IMAP authentication**
- **DMARC failure forensic reports (ruf)** — aggregate (rua) reports only
- **Web UI or dashboard**
- **Multi-tenant or hosted deployment**
