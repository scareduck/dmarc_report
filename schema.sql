-- dmarc_report SQLite schema
-- Standalone DDL — also applied automatically on first run by dmarc_report.py.
--
-- Apply manually:
--   sqlite3 reports.db < schema.sql

PRAGMA foreign_keys = ON;

-- One row per DMARC aggregate report email received.
-- Deduplicated on (report_id, org_name) — the natural key from the XML.
CREATE TABLE IF NOT EXISTS raw_reports (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Email provenance
    message_id        TEXT    NOT NULL,          -- RFC 5322 Message-ID header
    account           TEXT    NOT NULL,          -- account.name from config
    folder            TEXT    NOT NULL,          -- IMAP folder name
    sender            TEXT,                      -- decoded From: header
    subject           TEXT,                      -- decoded Subject: header
    date_received     TEXT,                      -- raw Date: header value

    -- Report content
    raw_xml           TEXT,                      -- full report XML (decompressed)
    report_id         TEXT,                      -- <report_metadata><report_id>
    org_name          TEXT,                      -- <report_metadata><org_name>
    date_range_begin  INTEGER,                   -- Unix timestamp (begin)
    date_range_end    INTEGER,                   -- Unix timestamp (end)

    processed_at      TEXT    NOT NULL,          -- ISO 8601 UTC timestamp of import

    UNIQUE(report_id, org_name)                  -- deduplication key
);

-- One row per <record> element inside a DMARC aggregate report.
CREATE TABLE IF NOT EXISTS report_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_report_id   INTEGER NOT NULL
                        REFERENCES raw_reports(id) ON DELETE CASCADE,

    source_ip       TEXT,       -- <row><source_ip>
    count           INTEGER,    -- <row><count>

    -- From <row><policy_evaluated>
    disposition     TEXT,       -- none | quarantine | reject
    dkim_result     TEXT,       -- pass | fail
    spf_result      TEXT,       -- pass | fail

    -- From <identifiers>
    header_from     TEXT,
    envelope_from   TEXT
);

-- Selected email headers stored for auditing / future querying.
CREATE TABLE IF NOT EXISTS email_headers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_report_id   INTEGER NOT NULL
                        REFERENCES raw_reports(id) ON DELETE CASCADE,
    header_name     TEXT    NOT NULL,   -- e.g. "From", "Subject", "DKIM-Signature"
    header_value    TEXT                -- decoded value, or "present"/"absent" for flags
);

-- One row per summary email sent.  Used to determine the window for the
-- next summary (reports with processed_at > last sent_at).
CREATE TABLE IF NOT EXISTS summary_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at         TEXT    NOT NULL,   -- ISO 8601 UTC timestamp
    report_count    INTEGER NOT NULL,   -- number of raw_reports included
    record_count    INTEGER NOT NULL,   -- total report_records included
    period_start    TEXT,               -- processed_at of earliest report in batch
    period_end      TEXT                -- processed_at of latest report in batch
);

-- Useful indexes
CREATE INDEX IF NOT EXISTS idx_raw_reports_processed_at
    ON raw_reports(processed_at);

CREATE INDEX IF NOT EXISTS idx_raw_reports_org_domain
    ON raw_reports(org_name, date_range_begin);

CREATE INDEX IF NOT EXISTS idx_report_records_report_id
    ON report_records(raw_report_id);

CREATE INDEX IF NOT EXISTS idx_report_records_disposition
    ON report_records(disposition);
