"""
pipeline/sources/linkedin.py

LinkedIn as an enumerable source, not a keyword lottery.

The guest job-search endpoint answers without a keyword, so we ask for
everything posted in a time window and page through it in date order rather
than firing search terms and hoping. What comes back is describable: a window,
an order, and a stated boundary when LinkedIn stops serving.

Emits the same row shape as the YC and HN sources so Stage 1 doesn't care where
a listing came from.
"""

from __future__ import annotations

import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone

import requests

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
VIEW_URL = "https://www.linkedin.com/jobs/view/{}"

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0.0.0 Safari/537.36")

PAGE = 10                  # what the endpoint actually returns, not the 25 you'd assume
DEPTH_LIMIT = 1000         # LinkedIn's own: start >= 1000 returns HTTP 400
EMPTY_RETRIES = 2          # an empty page mid-run is a hiccup, not the end
EMPTY_STOP = 3             # this many empty pages in a row and we believe it
DELAY = (3.0, 7.0)         # same politeness window jobspy uses, and it works
BACKOFF = (30, 60, 120, 240)   # 429 waits; exhaust these and the run is partial

# The guest endpoint accepts every f_* filter and ignores all of them: f_WT
# (work mode), f_F (function), f_I (industry), f_E (experience), f_JT (type)
# all return byte-identical results. Only keywords, f_TPR, location and sortBy
# actually do anything — tested, not assumed. So there are no categories to
# pick, and work mode stays a downstream text decision like YC and HN.

_REMOTE_RE = re.compile(r"\bremote\b", re.I)
_ONSITE_RE = re.compile(r"\bon[\s-]?site\b", re.I)


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"user-agent": UA, "accept": "text/html", "accept-language": "en-US,en;q=0.9"})
    return s


def _get_page(session, start: int, hours: int, location: str,
              keywords: str = "", timeout: int = 15):
    """One search page. Returns (status, cards) where status is "ok",
    "ratelimited" or "error" — never 'there is nothing here'."""
    params = {
        "location": location,
        "f_TPR": f"r{int(hours * 3600)}",
        "sortBy": "DD",          # date descending: makes paging chronological
        "start": start,
    }
    if keywords:
        params["keywords"] = keywords
    try:
        r = session.get(SEARCH_URL, params=params, timeout=timeout)
    except Exception as e:
        log.warning(f"[li] page start={start} failed: {e}")
        return "error", []
    if r.status_code == 429:
        return "ratelimited", []
    if r.status_code >= 400:
        return "error", []
    if BeautifulSoup is None:
        raise RuntimeError("beautifulsoup4 is required for the LinkedIn source")
    soup = BeautifulSoup(r.text, "html.parser")
    return "ok", soup.find_all("div", class_="base-search-card")


def _card_date(card) -> str:
    t = card.find("time")
    return (t.get("datetime") or "") if t else ""


def parse_card(card) -> dict | None:
    """One search-result card into a row. None when there's no usable id."""
    link = card.find("a", class_="base-card__full-link")
    href = (link.get("href") or "").split("?")[0] if link else ""
    job_id = href.rstrip("/").split("-")[-1] if href else ""
    if not job_id.isdigit():
        return None

    title_el = card.find("span", class_="sr-only")
    title = title_el.get_text(strip=True) if title_el else ""

    sub = card.find("h4", class_="base-search-card__subtitle")
    company_a = sub.find("a") if sub else None
    company = company_a.get_text(strip=True) if company_a else (
        sub.get_text(strip=True) if sub else "")
    company_url = (company_a.get("href") or "").split("?")[0] if company_a else ""

    meta = card.find("div", class_="base-search-card__metadata")
    loc_el = meta.find("span", class_="job-search-card__location") if meta else None
    location = loc_el.get_text(strip=True) if loc_el else ""

    blob = f"{title} {location}"
    if _REMOTE_RE.search(blob):
        is_remote = True
    elif _ONSITE_RE.search(blob):
        is_remote = False
    else:
        is_remote = None

    return {
        "id": f"li-{job_id}",            # matches what's already in the pool
        "title": title,
        "company": company,
        "company_url_direct": company_url.replace("https://www.linkedin.com/company/", ""),
        "location": location,
        "job_type": "",
        "min_amount": None,
        "max_amount": None,
        "currency": "",
        "site": "linkedin",
        "job_url": VIEW_URL.format(job_id),
        "description": "",
        "date_posted": _card_date(card),
        "is_remote": is_remote,
    }


def fetch_description(job_id: str, session, timeout: int = 15) -> str:
    """The card carries no description — that's a second request per job."""
    try:
        r = session.get(VIEW_URL.format(job_id), timeout=timeout)
        r.raise_for_status()
    except Exception:
        return ""
    soup = BeautifulSoup(r.text, "html.parser")
    box = (soup.find("div", class_="show-more-less-html__markup")
           or soup.find("section", class_="description"))
    return box.get_text("\n", strip=True) if box else ""


def scrape_linkedin_jobs(hours: int = 72, location: str = "Worldwide",
                         keywords: str = "",
                         limit: int | None = None,
                         depth_limit: int = DEPTH_LIMIT,
                         with_descriptions: bool = True,
                         stats: dict | None = None) -> list[dict]:
    """Everything posted in the last `hours`, newest first.

    `keywords` empty = the whole firehose, which is what the research pool
    wants; pass a term to narrow it for an actual job hunt.
    `limit` None = the time window decides how much comes back.
    Stops for exactly one of three reasons and says which: the window ran out
    (complete), LinkedIn's depth limit (partial — the boundary date is logged),
    or the results genuinely ended."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).date().isoformat()
    session = _session()
    rows: dict[str, dict] = {}
    start = pages = empty_streak = failed = throttled = 0
    reason = "end of results"
    oldest = ""

    while start < depth_limit:
        status, cards = _get_page(session, start, hours, location, keywords)
        pages += 1
        if status == "ratelimited":
            # wait it out and retry the SAME offset — skipping ahead here is how
            # you silently lose a page
            if throttled >= len(BACKOFF):
                reason = "rate limited — LinkedIn stopped answering"
                break
            wait = BACKOFF[throttled] + random.uniform(0, 10)
            throttled += 1
            log.warning(f"[li] 429 at start={start} — backing off {wait:.0f}s "
                        f"({throttled}/{len(BACKOFF)})")
            time.sleep(wait)
            continue
        if status == "error":
            failed += 1
            if empty_streak >= EMPTY_RETRIES:
                reason = "request failures"
                break
            empty_streak += 1
            time.sleep(random.uniform(*DELAY))
            continue
        if not cards:
            # start=500 returns 200 with zero cards while 900 returns ten, so an
            # empty page proves nothing on its own
            empty_streak += 1
            if empty_streak >= EMPTY_STOP:
                break
            time.sleep(random.uniform(*DELAY))
            start += PAGE
            continue
        empty_streak = 0
        throttled = 0        # a good page means the throttle lifted

        stop = False
        for card in cards:
            row = parse_card(card)
            if not row:
                continue
            if row["date_posted"]:
                oldest = row["date_posted"]
                if row["date_posted"] < cutoff:
                    reason, stop = "reached the window edge", True
                    break
            rows.setdefault(row["id"], row)
            if limit and len(rows) >= limit:
                reason, stop = f"hit limit={limit}", True
                break
        if stop:
            break
        start += PAGE
        time.sleep(random.uniform(*DELAY))
    else:
        reason = f"LinkedIn depth limit ({depth_limit}) — older listings unreachable"

    out = list(rows.values())
    if with_descriptions:
        for row in out:
            row["description"] = fetch_description(row["id"][3:], session)
            time.sleep(random.uniform(0.5, 1.5))
    session.close()

    log.info(f"[li] {pages} pages -> {len(out)} listings, back to {oldest or 'n/a'} "
             f"(window {hours}h, cutoff {cutoff}, "
             f"keywords={keywords or 'none'}) — stopped: {reason}")
    if failed:
        log.warning(f"[li] {failed} page requests failed — coverage is incomplete")
    if stats is not None:
        stats.update(pages=pages, listings=len(out), oldest=oldest,
                     cutoff=cutoff, reason=reason, failed=failed,
                     throttled=throttled,
                     complete=reason == "reached the window edge")
    return out
