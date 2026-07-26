"""
Enrich only: research companies, scrape nothing, judge nothing.

    python -m pipeline.run_enrich

Walks the companies behind your recent listings and fills the cache - one pass
per company, cache-first, so re-runs only pay for what's genuinely new. Ordered
by how many of your listings ride on each company, so if you stop it early
you've already got the ones that matter most.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from core.config import load_config, load_keys
from core.database import get_db_connection, init_db
import pipeline.brain1 as b1
from pipeline import enrich

log = logging.getLogger(__name__)


def run_enrich(days: int = 30, limit: int = 0) -> dict:
    cfg = load_config()
    keys = load_keys()
    client, model, backend = b1.get_gemma_client_for_stage(cfg, keys, "stage23")
    init_db()
    conn = get_db_connection()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """SELECT company, domain, MAX(COALESCE(yc_slug,'')) AS yc_slug,
                  COUNT(*) AS n
           FROM jobs WHERE date_scraped >= ? AND COALESCE(company,'') != ''
           GROUP BY company, domain ORDER BY n DESC""", (cutoff,)).fetchall()
    if limit:
        rows = rows[:limit]
    log.info(f"[enrich] {len(rows)} companies seen in the last {days} days")

    done = cached = failed = 0
    t0 = time.time()
    for i, r in enumerate(rows, 1):
        try:
            e = enrich.enrich_company(conn, cfg, r["company"], r["domain"] or "",
                                      client=client, model=model,
                                      backend=backend,
                                      yc_slug=r["yc_slug"] or "")
            if e.get("from_cache"):
                cached += 1
            else:
                done += 1
                log.info(f"[enrich] [{i}/{len(rows)}] {r['company']} "
                         f"({r['n']} listings ride on this)")
        except Exception as ex:
            failed += 1
            log.warning(f"[enrich] {r['company']} failed: {ex}")
    conn.close()
    mins = (time.time() - t0) / 60
    log.info(f"[enrich] done in {mins:.1f} min: {done} researched, "
             f"{cached} already cached, {failed} failed")
    return {"researched": done, "cached": cached, "failed": failed}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    print(run_enrich())
