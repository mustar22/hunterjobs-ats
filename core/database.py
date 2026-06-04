"""
database.py

SQLite schema for HunterJobs ATS. WAL mode, FTS5 full-text search,
idempotent init (no needless trigger-rebuild on every call).
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent / "db" / "hunterjobs_ats.db"

# Gates the RAG feature: True once sqlite-vec loads on any connection. When the
# extension can't load, RAG is disabled and the rest of the app runs normally.
RAG_AVAILABLE = False
_rag_load_warned = False


def _load_vec_extension(conn: sqlite3.Connection) -> bool:
    """Best-effort load of the sqlite-vec loadable extension onto `conn`.
    Each connection that touches the job_embeddings vec0 table must load it.
    On any failure, log once and leave RAG disabled — never raise."""
    global RAG_AVAILABLE, _rag_load_warned
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        RAG_AVAILABLE = True
        return True
    except Exception as e:
        if not _rag_load_warned:
            log.warning(
                "sqlite-vec extension unavailable; RAG (similar past "
                "applications) disabled. Reason: %s", e
            )
            _rag_load_warned = True
        RAG_AVAILABLE = False
        return False


def get_db_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    _load_vec_extension(conn)
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
    contacts         TEXT DEFAULT '',   -- JSON list of {name,title,email,source,confidence}
    contact_name     TEXT,              -- legacy single-contact cols (unused, kept for back-compat)
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

# RAG embeddings: vec0 virtual table, one 768-dim Gemini vector per job.
# job_id is a conceptual FK to jobs.id (vec0 doesn't enforce it).
EMBEDDINGS_TABLE = """
CREATE VIRTUAL TABLE IF NOT EXISTS job_embeddings USING vec0(
    job_id    TEXT PRIMARY KEY,
    embedding float[768] distance_metric=cosine
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

    # Skip the embeddings table when sqlite-vec didn't load (RAG disabled).
    if RAG_AVAILABLE:
        try:
            c.execute(EMBEDDINGS_TABLE)
        except Exception as e:
            log.warning("Could not create job_embeddings table: %s", e)

    # ── lightweight migrations for older DBs missing newer columns ───────────
    c.execute("PRAGMA table_info(jobs)")
    existing_cols = {row[1] for row in c.fetchall()}
    for col, col_def in [
        ("notes",     "TEXT DEFAULT ''"),
        ("row_color", "TEXT DEFAULT ''"),
        ("contacts",  "TEXT DEFAULT ''"),
    ]:
        if col not in existing_cols:
            c.execute(f"ALTER TABLE jobs ADD COLUMN {col} {col_def}")

    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print(f"[*] Database ready: {DB_PATH}")
