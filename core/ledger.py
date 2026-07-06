"""
core/ledger.py

First-seen ledger (seen_jobs table): the source of truth for "new since last
scan" — upstream dates are display data. Helpers take an open conn.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def upsert_seen(conn: sqlite3.Connection, job_key: str, source: str,
                now_iso: str | None = None) -> bool:
    """Record a sighting; True if it's the first. Re-sightings bump
    last_seen_at and un-expire (a listing that reappears is live again)."""
    now_iso = now_iso or _now_iso()
    cur = conn.execute(
        """
        INSERT INTO seen_jobs (job_key, source, first_seen_at, last_seen_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(job_key) DO UPDATE SET
            last_seen_at = excluded.last_seen_at,
            expired_at   = NULL
        """,
        (job_key, source, now_iso, now_iso),
    )
    conn.commit()
    # lastrowid is unreliable for upserts; detect "new" via first_seen == now.
    row = conn.execute(
        "SELECT first_seen_at FROM seen_jobs WHERE job_key = ?", (job_key,)
    ).fetchone()
    return bool(row) and row["first_seen_at"] == now_iso


def mark_judged(conn: sqlite3.Connection, job_key: str,
                now_iso: str | None = None) -> None:
    conn.execute(
        "UPDATE seen_jobs SET judged_at = ? WHERE job_key = ? AND judged_at IS NULL",
        (now_iso or _now_iso(), job_key),
    )
    conn.commit()


def is_judged(conn: sqlite3.Connection, job_key: str) -> bool:
    row = conn.execute(
        "SELECT judged_at FROM seen_jobs WHERE job_key = ?", (job_key,)
    ).fetchone()
    return bool(row) and row["judged_at"] is not None


def prune_expired(conn: sqlite3.Connection, not_seen_days: int,
                  now: datetime | None = None) -> int:
    """Mark rows unseen for N days as expired (vanished = filled/closed).
    Never deletes. Returns newly-expired count; <=0 disables."""
    if not_seen_days <= 0:
        return 0
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=not_seen_days)).isoformat()
    cur = conn.execute(
        "UPDATE seen_jobs SET expired_at = ? "
        "WHERE expired_at IS NULL AND last_seen_at < ?",
        (now.isoformat(), cutoff),
    )
    conn.commit()
    return cur.rowcount


def backfill_from_jobs(conn: sqlite3.Connection,
                       now_iso: str | None = None) -> int:
    """Seed the ledger from existing jobs rows (migration). Idempotent;
    returns rows inserted."""
    now_iso = now_iso or _now_iso()
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO seen_jobs
            (job_key, source, first_seen_at, last_seen_at, judged_at)
        SELECT id, source,
               COALESCE(NULLIF(date_scraped, ''), ?),
               COALESCE(NULLIF(date_scraped, ''), ?),
               CASE WHEN gemma1_done = 1
                    THEN COALESCE(NULLIF(date_scraped, ''), ?) END
        FROM jobs
        """,
        (now_iso, now_iso, now_iso),
    )
    conn.commit()
    return cur.rowcount
