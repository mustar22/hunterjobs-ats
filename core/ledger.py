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


def census_pass(conn: sqlite3.Connection, source: str, run_started: str,
                max_misses: int = 2, now: str | None = None) -> tuple[int, int]:
    """Call ONLY after a COMPLETE pass of `source`. Rows not sighted since
    `run_started` take a miss; `max_misses` in a row means gone.

    Never call this on a partial run — with `--no-yc`, or a pass that died
    halfway, every listing looks absent and you would bury the whole source.
    Two misses is what makes one flaky run cost nothing.

    Returns (newly_missed, newly_expired)."""
    ts = now or _now_iso()
    conn.execute("UPDATE seen_jobs SET miss_count = 0 "
                 "WHERE source = ? AND last_seen_at >= ?", (source, run_started))
    missed = conn.execute(
        "UPDATE seen_jobs SET miss_count = COALESCE(miss_count, 0) + 1 "
        "WHERE source = ? AND last_seen_at < ? AND expired_at IS NULL",
        (source, run_started)).rowcount
    expired = conn.execute(
        "UPDATE seen_jobs SET expired_at = ? "
        "WHERE source = ? AND COALESCE(miss_count, 0) >= ? AND expired_at IS NULL",
        (ts, source, max_misses)).rowcount
    conn.commit()
    return missed, expired


def mark_dead(conn: sqlite3.Connection, job_keys: list[str],
              now: str | None = None) -> int:
    """Expire listings we KNOW are gone — no inference. HN deletes come back
    as empty comments, so death is reported rather than guessed."""
    if not job_keys:
        return 0
    ts = now or _now_iso()
    ph = ",".join("?" for _ in job_keys)
    n = conn.execute(
        f"UPDATE seen_jobs SET expired_at = ? "
        f"WHERE job_key IN ({ph}) AND expired_at IS NULL",
        (ts, *job_keys)).rowcount
    conn.commit()
    return n
