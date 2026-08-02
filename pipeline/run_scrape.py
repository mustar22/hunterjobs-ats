"""
Scrape only: fill the pool, judge nothing.

    python -m pipeline.run_scrape

Every listing lands as QUEUED, so the next normal run drains them through
Stage 1 oldest-first. Useful when you want the listings banked now and the
LLM spend later (or on a different model).
"""

from __future__ import annotations

import logging

from core import ledger
from core.config import load_config
from core.database import get_db_connection, init_db
import pipeline.brain1 as b1
from pipeline.sources import hn

log = logging.getLogger(__name__)


def _store(conn, rows, label: str) -> int:
    """INSERT OR IGNORE so anything already judged keeps its verdict."""
    new = 0
    for row in rows:
        job = b1.scraped_row_to_job(row)
        if job is None:
            continue
        ledger.upsert_seen(conn, job["id"], job["source"])
        cur = conn.execute(
            """INSERT OR IGNORE INTO jobs (
                 id, title, company, domain, location, job_type,
                 salary_min, salary_max, currency, source, url,
                 description, date_posted, date_scraped, description_hash,
                 date_posted_estimated, yc_slug, verdict, reject_reason,
                 gemma1_done, gemma2_done, gemma3_done, applied
               ) VALUES (
                 :id, :title, :company, :domain, :location, :job_type,
                 :salary_min, :salary_max, :currency, :source, :url,
                 :description, :date_posted, :date_scraped, :description_hash,
                 :date_posted_estimated, :yc_slug, 'QUEUED', '',
                 0, 0, 0, 0
               )""", job)
        new += cur.rowcount
    conn.commit()
    log.info(f"[scrape] {label}: {len(rows)} scraped, {new} new")
    return new


def run_scrape(only: set[str] | None = None) -> dict:
    """`only` limits which sources run; None = whatever Setup has enabled."""
    cfg = load_config()
    want = (lambda name: (only is None or name in only))
    init_db()
    conn = get_db_connection()
    counts = {"linkedin_indeed": 0, "hn": 0, "yc": 0}
    try:
        sources = [s for s in (cfg.get("sources") or [])
                   if s in ("linkedin", "indeed") and want(s)]
        terms = [t.strip() for t in (cfg.get("search_terms") or "").split(",")
                 if t.strip()]
        if sources and terms:
            for term in terms:
                df = b1.safe_scrape(term, sources,
                                    int(cfg.get("results_wanted", 50)),
                                    int(cfg.get("hours_old", 720)))
                if df is not None and len(df):
                    counts["linkedin_indeed"] += _store(
                        conn, [r for _, r in df.iterrows()], f"jobspy:{term}")
        if cfg.get("use_hn") and want("hn"):
            rows = b1.apply_yc_date_filter(hn.scrape_hn_jobs(cfg),
                                           int(cfg.get("hours_old", 720)))
            counts["hn"] = _store(conn, rows, "HN")
        if cfg.get("use_yc") and want("yc"):
            rows = b1.apply_yc_date_filter(b1.safe_scrape_yc(cfg),
                                           int(cfg.get("yc_hours_old", 720)))
            counts["yc"] = _store(conn, rows, "YC")
    finally:
        conn.close()
    total = sum(counts.values())
    log.info(f"[scrape] done: {total} new listings queued for the next run")
    return counts


if __name__ == "__main__":
    import sys
    from pathlib import Path
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(Path(__file__).resolve().parent.parent
                                      / "hunterjobs.log", encoding="utf-8")])
    only = ({s.strip() for s in sys.argv[1].split(",") if s.strip()}
            if len(sys.argv) > 1 else None)
    print(run_scrape(only))
