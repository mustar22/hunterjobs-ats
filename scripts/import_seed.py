"""
scripts/import_seed.py

Import the free company-research seed from hunterjobsats.com into your local DB.

Someone (me) already paid the LLM calls to research a few thousand companies -
what they build, their real stack, whether they're a staffing agency wearing a
company costume. This pulls that down so your first scans don't re-research
companies that are already known.

It also brings YC and Hacker News listings, unjudged, so you have something to
judge on day one. LinkedIn and Indeed listings are NOT included - redistributing
those is the line between analysing public postings and running a listings
database. Scrape those yourself, it takes twenty minutes.

What it does NOT bring: contacts, or my verdicts. Contacts are personal data and
yours to hunt on your own keys; verdicts depend on your profile, not mine.

It lands in its own `companies_seed` table and never touches `companies`, so
your own research is never overwritten - and a job can show both reads next to
each other. If mine and yours disagree, that's worth seeing.

Usage:  python scripts/import_seed.py [--url URL] [--file PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

sys.path.insert(0, str(Path.cwd()))

from core.database import get_db_connection, init_db  # noqa: E402

SEED_URL = "https://hunterjobsats.com/seed/companies_seed.db"

# what we copy over; contacts/hunted deliberately absent
_FIELDS = ("company_key", "name", "domain", "yc_slug", "company_summary",
           "hiring_signal", "real_stack", "culture_flags", "company_size",
           "sources", "researched_at")


def _fetch(url: str) -> Path:
    print(f"[*] downloading {url}")
    tmp = Path(tempfile.gettempdir()) / "hj_companies_seed.db"
    with urlopen(url, timeout=120) as r, open(tmp, "wb") as f:
        f.write(r.read())
    print(f"[*] got {tmp.stat().st_size // 1024} KB")
    return tmp


_JOB_COLS = ("id", "title", "company", "domain", "location", "job_type",
             "salary_min", "salary_max", "currency", "source", "url",
             "description", "date_posted", "date_posted_estimated", "yc_slug",
             "date_scraped", "description_hash")


def _import_jobs(seed, conn, dry_run: bool) -> int:
    """YC + HN listings, unjudged. They arrive as QUEUED so YOUR profile
    decides on them - I'm not shipping my verdicts, only the listings and the
    ledger date they were first seen. LinkedIn/Indeed are never in here."""
    try:
        rows = seed.execute(
            f"SELECT {', '.join(_JOB_COLS)}, first_seen_at "
            f"FROM pool_jobs_seed").fetchall()
    except Exception:
        return 0                       # older seed file without listings
    new = 0
    for r in rows:
        if dry_run:
            new += 1
            continue
        cur = conn.execute(
            f"""INSERT OR IGNORE INTO jobs ({', '.join(_JOB_COLS)},
                  verdict, reject_reason, gemma1_done, gemma2_done,
                  gemma3_done, applied)
                VALUES ({', '.join('?' for _ in _JOB_COLS)},
                        'QUEUED', '', 0, 0, 0, 0)""",
            tuple(r[c] for c in _JOB_COLS))
        if cur.rowcount:
            new += 1
            seen = r["first_seen_at"] or r["date_scraped"]
            conn.execute(
                """INSERT OR IGNORE INTO seen_jobs
                   (job_key, source, first_seen_at, last_seen_at)
                   VALUES (?, ?, ?, ?)""",
                (r["id"], r["source"], seen, seen))
    return new


def import_seed(path: Path, dry_run: bool = False) -> dict:
    """Load a seed .db into companies_seed. Returns counts (also used by the
    Setup button, which is why this is a function and not just __main__)."""
    seed_conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    seed_conn.row_factory = sqlite3.Row
    rows = seed_conn.execute(
        f"SELECT {', '.join(_FIELDS)} FROM companies_seed").fetchall()

    init_db()
    conn = get_db_connection()
    have = {r[0]: (r[1] or "") for r in
            conn.execute("SELECT company_key, researched_at FROM companies_seed")}
    mine = {r[0] for r in conn.execute("SELECT company_key FROM companies")}

    new = updated = same = 0
    now = datetime.now(timezone.utc).isoformat()
    for r in rows:
        key = r["company_key"]
        if key not in have:
            new += 1
        elif (r["researched_at"] or "") > have[key]:
            updated += 1
        else:
            same += 1
            continue
        if dry_run:
            continue
        conn.execute(
            f"""INSERT INTO companies_seed ({', '.join(_FIELDS)}, imported_at)
                VALUES ({', '.join('?' for _ in _FIELDS)}, ?)
                ON CONFLICT(company_key) DO UPDATE SET
                  {', '.join(f'{f}=excluded.{f}' for f in _FIELDS[1:])},
                  imported_at=excluded.imported_at""",
            (*(r[f] for f in _FIELDS), now))
    jobs_new = _import_jobs(seed_conn, conn, dry_run)
    if not dry_run:
        conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM companies_seed").fetchone()[0]
    conn.close()
    seed_conn.close()
    return {"in_seed": len(rows), "new": new, "updated": updated,
            "unchanged": same, "total": total, "jobs_new": jobs_new,
            "also_mine": len({r["company_key"] for r in rows} & mine)}


def main(url: str, file: str | None, dry_run: bool) -> None:
    path = Path(file) if file else _fetch(url)
    c = import_seed(path, dry_run)
    verb = "would import" if dry_run else "imported"
    print(f"[*] seed holds {c['in_seed']} researched companies")
    print(f"[*] {verb}: {c['new']} new, {c['updated']} updated, "
          f"{c['unchanged']} already current")
    if c.get("jobs_new"):
        print(f"[*] {verb} {c['jobs_new']} YC/HN listings too, unjudged - your "
              f"own profile decides on them next run")
    if c["also_mine"]:
        print(f"[*] {c['also_mine']} of them you have researched yourself too "
              f"- both reads are kept, you can compare on the job")
    if not dry_run:
        print("[*] done - no contacts came along, those are yours to hunt")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=SEED_URL)
    ap.add_argument("--file", help="import a local .db instead of downloading")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change, write nothing")
    a = ap.parse_args()
    main(a.url, a.file, a.dry_run)
