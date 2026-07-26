"""
scripts/import_seed.py

Import the free company-research seed from hunterjobsats.com into your local DB.

Someone (me) already paid the LLM calls to research a few thousand companies -
what they build, their real stack, whether they're a staffing agency wearing a
company costume. This pulls that down so your first scans don't re-research
companies that are already known.

What it does NOT bring: contacts. Those are personal data and they're yours to
hunt, on your keys, from your machine - seeded rows come in with the contact
hunt still pending, so enrichment will look for people the normal way.

Your own research always wins: a local row is only replaced when the seed's
copy is genuinely newer.

Usage:  python scripts/import_seed.py [--url URL] [--file PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
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


def main(url: str, file: str | None, dry_run: bool) -> None:
    path = Path(file) if file else _fetch(url)
    seed = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    seed.row_factory = sqlite3.Row
    rows = seed.execute(
        f"SELECT {', '.join(_FIELDS)} FROM companies_seed").fetchall()
    seed.close()
    print(f"[*] seed holds {len(rows)} researched companies")

    init_db()
    conn = get_db_connection()
    mine = {r[0]: (r[1] or "") for r in
            conn.execute("SELECT company_key, researched_at FROM companies")}

    new = refreshed = kept = 0
    for r in rows:
        key = r["company_key"]
        if key not in mine:
            new += 1
        elif (r["researched_at"] or "") > mine[key]:
            refreshed += 1        # seed knows something newer than we do
        else:
            kept += 1             # ours is newer or equal: leave it alone
            continue
        if dry_run:
            continue
        conn.execute(
            f"""INSERT INTO companies ({', '.join(_FIELDS)}, contacts, hunted)
                VALUES ({', '.join('?' for _ in _FIELDS)}, '[]', 0)
                ON CONFLICT(company_key) DO UPDATE SET
                  {', '.join(f'{f}=excluded.{f}' for f in _FIELDS[1:])}""",
            tuple(r[f] for f in _FIELDS))
    if not dry_run:
        conn.commit()
    conn.close()

    verb = "would import" if dry_run else "imported"
    print(f"[*] {verb}: {new} new, {refreshed} refreshed, {kept} left alone "
          f"(yours were newer)")
    if not dry_run:
        print("[*] done - contacts stay unhunted, your next scan finds those "
              "on your own keys")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=SEED_URL)
    ap.add_argument("--file", help="import a local .db instead of downloading")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change, write nothing")
    a = ap.parse_args()
    main(a.url, a.file, a.dry_run)
