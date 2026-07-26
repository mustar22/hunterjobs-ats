"""
scripts/import_seed.py

Import the free company-research seed from hunterjobsats.com into your local DB.

Someone (me) already paid the LLM calls to research a few thousand companies -
what they build, their real stack, whether they're a staffing agency wearing a
company costume. This pulls that down so your first scans don't re-research
companies that are already known.

What it does NOT bring: contacts. Those are personal data and they're yours to
hunt, on your keys, from your machine.

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


def import_seed(path: Path, dry_run: bool = False) -> dict:
    """Load a seed .db into companies_seed. Returns counts (also used by the
    Setup button, which is why this is a function and not just __main__)."""
    seed = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    seed.row_factory = sqlite3.Row
    rows = seed.execute(
        f"SELECT {', '.join(_FIELDS)} FROM companies_seed").fetchall()
    seed.close()

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
    if not dry_run:
        conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM companies_seed").fetchone()[0]
    conn.close()
    return {"in_seed": len(rows), "new": new, "updated": updated,
            "unchanged": same, "total": total,
            "also_mine": len({r["company_key"] for r in rows} & mine)}


def main(url: str, file: str | None, dry_run: bool) -> None:
    path = Path(file) if file else _fetch(url)
    c = import_seed(path, dry_run)
    verb = "would import" if dry_run else "imported"
    print(f"[*] seed holds {c['in_seed']} researched companies")
    print(f"[*] {verb}: {c['new']} new, {c['updated']} updated, "
          f"{c['unchanged']} already current")
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
