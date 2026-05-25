"""
database.py

SQLite schema for HunterJobs ATS. WAL mode, FTS5 full-text search,
idempotent init (no needless trigger-rebuild on every call).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "db" / "hunterjobs_ats.db"


def get_db_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


# ── schema ────────────────────────────────────────────────────────────────────
JOBS_TABLE = """
CREATE TABLE IF NOT EXISTS jobs (
    id               TEXT PRIMARY KEY,
    title            TEXT,
    company          TEXT,
    domain           TEXT,
    location         TEXT,
    job_type         TEXT,
    salary_min       INTEGER,
    salary_max       INTEGER,
    currency         TEXT,
    source           TEXT,
    url              TEXT,
    description      TEXT,
    date_posted      TEXT,
    date_scraped     TEXT,
    description_hash TEXT,

    -- Gemma #1 output
    verdict          TEXT,
    reject_reason    TEXT,
    gemma1_done      INTEGER DEFAULT 0,

    -- Gemma #2 output
    company_summary  TEXT,
    hiring_signal    TEXT,
    real_stack       TEXT,
    culture_flags    TEXT,
    company_size     TEXT,
    gemma2_done      INTEGER DEFAULT 0,

    -- Gemma #3 output
    contact_name     TEXT,
    contact_title    TEXT,
    contact_email    TEXT,
    email_confidence TEXT,
    email_source     TEXT,
    outreach_draft   TEXT,
    gemma3_done      INTEGER DEFAULT 0,

    -- user actions
    applied          INTEGER DEFAULT 0,
    applied_date     TEXT,

    -- user annotations
    notes            TEXT DEFAULT '',
    row_color        TEXT DEFAULT ''
);
"""

MARKET_TABLE = """
CREATE TABLE IF NOT EXISTS market_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    date                TEXT,
    total_jobs          INTEGER,
    good_count          INTEGER,
    maybe_count         INTEGER,
    bad_count           INTEGER,
    hard_reject_count   INTEGER,
    top_stacks          TEXT,
    salary_avg_min      INTEGER,
    salary_avg_max      INTEGER,
    analysis            TEXT,
    targeting_feedback  TEXT
);
"""

BRAIN2_CHAT_TABLE = """
CREATE TABLE IF NOT EXISTS brain2_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    role        TEXT NOT NULL,        -- 'user' | 'assistant' | 'system' | 'tool'
    content     TEXT NOT NULL,        -- plain text or JSON for tool calls/results
    backend     TEXT,                 -- which model produced this turn
    tool_calls  TEXT,                 -- JSON: list of tool calls if assistant called any
    tool_name   TEXT,                 -- if role='tool', which tool was called
    tool_args   TEXT,                 -- if role='tool', args JSON
    hidden      INTEGER DEFAULT 0     -- 1 = hide from UI (system/tool turns by default)
);
"""

FTS_TABLE = """
CREATE VIRTUAL TABLE IF NOT EXISTS jobs_fts USING fts5(
    title, company, description, real_stack,
    content=jobs, content_rowid=rowid
);
"""

TRIGGERS = [
    """
    CREATE TRIGGER IF NOT EXISTS jobs_ai AFTER INSERT ON jobs BEGIN
        INSERT INTO jobs_fts(rowid, title, company, description, real_stack)
        VALUES (new.rowid, new.title, new.company, new.description, new.real_stack);
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS jobs_ad AFTER DELETE ON jobs BEGIN
        INSERT INTO jobs_fts(jobs_fts, rowid, title, company, description, real_stack)
        VALUES ('delete', old.rowid, old.title, old.company, old.description, old.real_stack);
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS jobs_au AFTER UPDATE ON jobs BEGIN
        INSERT INTO jobs_fts(jobs_fts, rowid, title, company, description, real_stack)
        VALUES ('delete', old.rowid, old.title, old.company, old.description, old.real_stack);
        INSERT INTO jobs_fts(rowid, title, company, description, real_stack)
        VALUES (new.rowid, new.title, new.company, new.description, new.real_stack);
    END;
    """,
]


def init_db() -> None:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(JOBS_TABLE)
    c.execute(MARKET_TABLE)
    c.execute(BRAIN2_CHAT_TABLE)
    c.execute(FTS_TABLE)
    for trig in TRIGGERS:
        c.execute(trig)

    # ── lightweight migrations for older DBs missing newer columns ───────────
    c.execute("PRAGMA table_info(jobs)")
    existing_cols = {row[1] for row in c.fetchall()}
    for col, col_def in [
        ("notes",     "TEXT DEFAULT ''"),
        ("row_color", "TEXT DEFAULT ''"),
    ]:
        if col not in existing_cols:
            c.execute(f"ALTER TABLE jobs ADD COLUMN {col} {col_def}")

    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print(f"[*] Database ready: {DB_PATH}")
