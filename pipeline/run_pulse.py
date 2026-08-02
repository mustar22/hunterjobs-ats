"""
Listing pulse: ask stored listings whether they still exist.

    python -m pipeline.run_pulse linkedin,hn 250

Never runs on its own - the Setup button or this command starts it. No LLM, no
API keys, one cheap request per listing. LinkedIn is paced deliberately: bursts
make live listings return 404, so rushing it manufactures deaths.

Y Combinator isn't a valid source here. Its scrape reads every company board in
full, so vanished listings are already caught during a normal run.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from core.database import get_db_connection, init_db
from pipeline.listing_pulse import SOURCES, run_pulse

log = logging.getLogger(__name__)


def main(argv: list[str]) -> int:
    # the Logs tab tails hunterjobs.log; a detached process that only writes
    # to stdout is invisible from the dashboard
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(
                Path(__file__).resolve().parent.parent / "hunterjobs.log",
                encoding="utf-8"),
        ],
    )
    sources = tuple(s.strip() for s in (argv[0] if argv else "linkedin").split(",")
                    if s.strip())
    limit = int(argv[1]) if len(argv) > 1 else 250
    bad = [s for s in sources if s not in SOURCES]
    if bad:
        log.error(f"no direct check for {bad}; valid sources: {list(SOURCES)}")
        return 2

    init_db()
    conn = get_db_connection()
    try:
        res = run_pulse(conn, sources=sources, limit=limit)
    finally:
        conn.close()
    return 0 if res else 2      # run_pulse already logged the per-source result


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
