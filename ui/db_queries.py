"""
ui/db_queries.py

Dashboard-side DB helpers (the read/write queries the UI issues against the
jobs table). Named db_queries to avoid colliding with core.database, which owns
the schema/connection. These all go through core.database.get_db_connection.
"""

from __future__ import annotations

from datetime import datetime, timezone

from core.database import get_db_connection


def fetch_jobs(verdicts: list[str], query: str = "", limit: int = 300) -> list[dict]:
    if not verdicts:
        return []
    conn = get_db_connection()
    try:
        params: list = []
        if query.strip():
            sql = "SELECT j.* FROM jobs j JOIN jobs_fts f ON j.rowid = f.rowid WHERE "
            safe_q = query.replace('"', '""')
            sql += "jobs_fts MATCH ? AND "
            params.append(f'"{safe_q}"*')
        else:
            sql = "SELECT * FROM jobs WHERE "
        placeholders = ",".join("?" for _ in verdicts)
        sql += f"verdict IN ({placeholders}) AND (applied IS NULL OR applied = 0) "
        sql += "ORDER BY date_scraped DESC LIMIT ?"
        params.extend(verdicts)
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def fetch_applied() -> list[dict]:
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE applied=1 ORDER BY applied_date DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_applied(job_id: str) -> None:
    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE jobs SET applied=1, applied_date=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), job_id),
        )
        conn.commit()
    finally:
        conn.close()


def unmark_applied(job_id: str) -> None:
    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE jobs SET applied=0, applied_date=NULL WHERE id=?", (job_id,)
        )
        conn.commit()
    finally:
        conn.close()


def update_notes(job_id: str, notes: str) -> None:
    conn = get_db_connection()
    try:
        conn.execute("UPDATE jobs SET notes=? WHERE id=?", (notes or "", job_id))
        conn.commit()
    finally:
        conn.close()


def update_row_color(job_id: str, color: str) -> None:
    """Set the user's color label for a job. color: '' (none) or one of
    purple/green/amber/red/blue/gray."""
    if color and color not in ("purple", "green", "amber", "red", "blue", "gray"):
        return
    conn = get_db_connection()
    try:
        conn.execute("UPDATE jobs SET row_color=? WHERE id=?", (color or "", job_id))
        conn.commit()
    finally:
        conn.close()


def update_verdict(job_id: str, verdict: str, reason: str = "") -> None:
    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE jobs SET verdict=?, reject_reason=? WHERE id=?",
            (verdict, reason, job_id),
        )
        conn.commit()
    finally:
        conn.close()
