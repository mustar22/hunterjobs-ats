"""
scripts/migrate_v05.py

One-shot migration to the v0.5 ledger/metering schema. Idempotent — safe to
re-run. Backs up the DB first.

  1. init_db() — new tables (seen_jobs, scan_usage) + date_posted_estimated col
  2. recompute YC job ids (old ids embedded the unstable estimated date)
  3. backfill the seen_jobs ledger from existing history
  4. flag pre-migration YC dates as estimated (can't tell waas from ATS now)

Run from repo root: python scripts/migrate_v05.py
"""

import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.database import DB_PATH, get_db_connection, init_db
from core.ledger import backfill_from_jobs
from pipeline.brain1 import fallback_job_id


def recompute_yc_ids(conn) -> tuple[int, int]:
    """Old YC ids embedded date_posted; rebuild them without it. On collision
    (two old rows converging) the newest date_scraped wins."""
    rows = conn.execute(
        "SELECT id, company, title, url, date_posted, date_scraped "
        "FROM jobs WHERE source='yc'"
    ).fetchall()
    renamed = dropped = 0
    for r in rows:
        new_id = fallback_job_id(
            {"company": r["company"], "title": r["title"],
             "job_url": r["url"], "date_posted": r["date_posted"]}
        )
        if new_id == r["id"]:
            continue
        clash = conn.execute(
            "SELECT id, date_scraped FROM jobs WHERE id=?", (new_id,)
        ).fetchone()
        if clash:
            # keep whichever row is newer
            if (clash["date_scraped"] or "") >= (r["date_scraped"] or ""):
                conn.execute("DELETE FROM jobs WHERE id=?", (r["id"],))
            else:
                conn.execute("DELETE FROM jobs WHERE id=?", (new_id,))
                conn.execute("UPDATE jobs SET id=? WHERE id=?", (new_id, r["id"]))
            dropped += 1
        else:
            conn.execute("UPDATE jobs SET id=? WHERE id=?", (new_id, r["id"]))
            renamed += 1
        _rekey_embedding(conn, r["id"], new_id)
    conn.commit()
    return renamed, dropped


def _rekey_embedding(conn, old_id: str, new_id: str) -> None:
    """vec0 has no UPDATE; delete+reinsert. No-op when RAG table is absent."""
    try:
        row = conn.execute(
            "SELECT embedding FROM job_embeddings WHERE job_id=?", (old_id,)
        ).fetchone()
        if not row:
            return
        conn.execute("DELETE FROM job_embeddings WHERE job_id IN (?, ?)",
                     (old_id, new_id))
        conn.execute("INSERT INTO job_embeddings (job_id, embedding) VALUES (?, ?)",
                     (new_id, row["embedding"]))
    except Exception:
        pass  # sqlite-vec not loaded / table missing — RAG rebuilds on demand


def main():
    if not DB_PATH.exists():
        print(f"[*] No DB at {DB_PATH} — fresh install, init_db is enough.")
        init_db()
        return

    backup = DB_PATH.with_suffix(f".pre-v05.{datetime.now(timezone.utc):%Y%m%d%H%M%S}.bak")
    conn = get_db_connection()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    conn.close()
    shutil.copy2(DB_PATH, backup)
    print(f"[*] Backup: {backup}")

    init_db()
    conn = get_db_connection()
    try:
        renamed, dropped = recompute_yc_ids(conn)
        print(f"[*] YC ids recomputed: {renamed} renamed, {dropped} duplicate rows merged")

        seeded = backfill_from_jobs(conn)
        print(f"[*] Ledger backfilled: {seeded} rows")

        flagged = conn.execute(
            "UPDATE jobs SET date_posted_estimated=1 "
            "WHERE source='yc' AND date_posted_estimated=0"
        ).rowcount
        conn.commit()
        print(f"[*] Flagged {flagged} pre-migration YC dates as estimated")
    finally:
        conn.close()
    print("[*] Migration done.")


if __name__ == "__main__":
    main()
