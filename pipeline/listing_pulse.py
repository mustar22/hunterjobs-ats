"""
pipeline/listing_pulse.py

Ask listings whether they still exist, instead of guessing from a timer.

Never runs on its own — you call it, for the sources you pick. No LLM, no API
keys, one cheap HTTP request per listing.

Why not a timer: the ten oldest LinkedIn listings in the pool were five weeks
old and every one was still live. Any expiry clock would have been wrong about
all of them.

Why not scrape-diff for LinkedIn: search stops serving past a fixed depth, so
an old listing can never reappear in a scrape. Absence there proves nothing.
YC is different — its scrape IS a census, so liveness comes free during
scraping and this module deliberately doesn't duplicate it.
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone

import requests

from core import ledger

log = logging.getLogger(__name__)

LI_VIEW = "https://www.linkedin.com/jobs/view/{}"
HN_ITEM = "https://hacker-news.firebaseio.com/v0/item/{}.json"

# pacing is correctness, not manners: bursts made LinkedIn 404 listings that
# were provably live minutes later
LI_DELAY = (4.0, 7.0)
CONFIRM_MISSES = 2          # one 404 is a rumour, two on separate passes is news
PROGRESS_EVERY = 25         # heartbeat so a 25-minute run doesn't look dead

SOURCES = ("linkedin", "hn")   # yc liveness arrives free with its census scrape


def _stalest(conn, source: str, limit: int) -> list[str]:
    """Listings we've verified least recently, oldest first."""
    return [r[0] for r in conn.execute(
        "SELECT job_key FROM seen_jobs WHERE source = ? AND expired_at IS NULL "
        "ORDER BY COALESCE(checked_at, '') ASC, last_seen_at ASC LIMIT ?",
        (source, limit))]


def _record(conn, job_key: str, alive: bool | None) -> bool:
    """alive True/False, or None for 'could not tell'. Returns True if this
    check buried the listing."""
    now = datetime.now(timezone.utc).isoformat()
    if alive is None:                       # blocked, timed out, server error
        conn.execute("UPDATE seen_jobs SET checked_at = ? WHERE job_key = ?",
                     (now, job_key))
        conn.commit()
        return False
    if alive:
        conn.execute("UPDATE seen_jobs SET checked_at = ?, miss_count = 0 "
                     "WHERE job_key = ?", (now, job_key))
        conn.commit()
        return False
    row = conn.execute("SELECT COALESCE(miss_count,0) m FROM seen_jobs "
                       "WHERE job_key = ?", (job_key,)).fetchone()
    misses = (row["m"] if row else 0) + 1
    conn.execute("UPDATE seen_jobs SET checked_at = ?, miss_count = ? "
                 "WHERE job_key = ?", (now, misses, job_key))
    conn.commit()
    if misses >= CONFIRM_MISSES:
        ledger.mark_dead(conn, [job_key])
        return True
    return False


def _check_linkedin(session, job_key: str) -> bool | None:
    """404 = gone. Anything else that isn't 200 means we couldn't tell."""
    try:
        r = session.get(LI_VIEW.format(job_key[3:]), timeout=15,
                        allow_redirects=False)
    except Exception:
        return None
    if r.status_code == 404:
        return False
    if r.status_code == 200:
        return True
    return None                              # 429 and friends are not deaths


def _check_hn(session, job_key: str) -> bool | None:
    """HN reports death: deleted/dead comments come back empty but present."""
    try:
        r = session.get(HN_ITEM.format(job_key[3:]), timeout=15)
        r.raise_for_status()
        item = r.json()
    except Exception:
        return None
    if not item:
        return False
    return not (item.get("deleted") or item.get("dead"))


def run_pulse(conn, sources=("linkedin",), limit: int = 250,
              progress=None, should_stop=None) -> dict:
    """Verify the stalest `limit` listings per source. Returns per-source counts.

    Nothing is buried on a single failure, and nothing is buried because a
    server was rude — only a repeated, explicit 'not found'."""
    out: dict[str, dict] = {}
    session = requests.Session()
    session.headers.update({"user-agent": "Mozilla/5.0 (X11; Linux x86_64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0.0.0 Safari/537.36"})
    try:
        for source in sources:
            if source not in SOURCES:
                log.warning(f"[pulse] {source} has no direct check — skipping")
                continue
            keys = _stalest(conn, source, limit)
            log.info(f"[pulse] {source}: checking {len(keys)} stalest listings"
                     + (" (~%d min at 6s each)" % round(len(keys) * 5.5 / 60)
                        if source == "linkedin" and keys else ""))
            alive = gone = unknown = 0
            for i, key in enumerate(keys, 1):
                if should_stop and should_stop():
                    log.info("[pulse] stopped by user")
                    break
                verdict = (_check_linkedin(session, key) if source == "linkedin"
                           else _check_hn(session, key))
                if verdict is None:
                    unknown += 1
                elif verdict:
                    alive += 1
                else:
                    gone += 1
                _record(conn, key, verdict)
                if progress:
                    progress(source, i, len(keys), alive, gone, unknown)
                # paced runs take ~25 min for 250 listings; say something or it
                # reads as hung
                if i % PROGRESS_EVERY == 0 and i != len(keys):
                    log.info(f"[pulse] {source} {i}/{len(keys)} — {alive} alive, "
                             f"{gone} not found, {unknown} unreadable")
                if source == "linkedin":
                    time.sleep(random.uniform(*LI_DELAY))
            out[source] = {"checked": len(keys), "alive": alive,
                           "gone": gone, "unknown": unknown}
            log.info(f"[pulse] {source}: {len(keys)} checked — {alive} alive, "
                     f"{gone} not found, {unknown} couldn't tell")
            if unknown > len(keys) * 0.2 and keys:
                log.error(f"[pulse] {source}: {unknown}/{len(keys)} unreadable — "
                          f"likely rate-limited, treat this run as incomplete")
    finally:
        session.close()
    return out
