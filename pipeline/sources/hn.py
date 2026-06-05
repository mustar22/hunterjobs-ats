"""
pipeline/sources/hn.py

Hacker News "Ask HN: Who is hiring?" as a job source. No auth, no LLM:

  - Algolia HN Search API finds the newest monthly @whoishiring thread.
  - Firebase HN API fetches the thread's top-level comments; each top-level
    comment is one job posting.
  - Deterministic regex pulls the easy fields (company, apply URL, remote flag);
    the cleaned comment text becomes the job description that Stage 1 judges.
    Blank fields when unsure — nothing is fabricated.

Emits JobSpy-shaped row dicts (source="hn") so HN flows through the exact same
Stage 1 path as LinkedIn/Indeed/YC. Every network call is contained → any
failure returns [] / skips the item and never blocks the rest of the scrape.
"""

from __future__ import annotations

import html
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

try:
    from bs4 import BeautifulSoup
except Exception:  # bs4 optional — fall back to regex tag-strip
    BeautifulSoup = None

log = logging.getLogger(__name__)

ALGOLIA_SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
FIREBASE_ITEM = "https://hacker-news.firebaseio.com/v0/item/{}.json"
HN_COMMENT_URL = "https://news.ycombinator.com/item?id={}"

_URL_RE = re.compile(r"https?://[^\s<>\"')]+")
_REMOTE_RE = re.compile(r"\bremote\b", re.I)
_ONSITE_RE = re.compile(r"\bon[\s-]?site\b", re.I)
# Pipe is the dominant delimiter in HN "Who is hiring" posts; dash is a fallback.
_DASH_SPLIT_RE = re.compile(r"\s[-–—]\s")


def find_latest_hiring_thread(timeout: int = 10) -> dict | None:
    """Locate the newest 'Ask HN: Who is hiring?' story via Algolia (sorted
    newest-first), skipping the sibling 'freelancer' / 'wants to be hired'
    threads. Returns {id, title, date} or None on any failure."""
    params = {
        "query": "Ask HN: Who is hiring?",
        "tags": "story,author_whoishiring",
        "hitsPerPage": 20,
    }
    try:
        r = requests.get(ALGOLIA_SEARCH, params=params, timeout=timeout)
        r.raise_for_status()
        hits = r.json().get("hits") or []
    except Exception as e:
        log.warning(f"[hn] thread search failed (skipping): {e}")
        return None
    for h in hits:  # search_by_date → newest first
        title = (h.get("title") or "").lower()
        if ("who is hiring" in title
                and "freelancer" not in title
                and "wants to be hired" not in title):
            oid = h.get("objectID")
            if not oid:
                continue
            return {
                "id": int(oid),
                "title": h.get("title") or "",
                "date": (h.get("created_at") or "")[:10],
            }
    log.warning("[hn] no matching 'Who is hiring?' story in search results")
    return None


def _fetch_item(item_id: int, session: requests.Session, timeout: int = 10):
    try:
        r = session.get(FIREBASE_ITEM.format(item_id), timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def fetch_thread_comments(story_id: int, timeout: int = 10) -> list[int]:
    """Top-level comment ids of the thread, in HN display order (the story's
    `kids`). Empty list on failure."""
    try:
        r = requests.get(FIREBASE_ITEM.format(story_id), timeout=timeout)
        r.raise_for_status()
        return r.json().get("kids") or []
    except Exception as e:
        log.warning(f"[hn] could not fetch thread {story_id} (skipping): {e}")
        return []


def _clean_html(raw: str) -> tuple[str, list[str]]:
    """Return (plain_text, hrefs) from an HN comment's HTML. HN uses <p> for
    paragraph breaks and HTML entities throughout."""
    if not raw:
        return "", []
    if BeautifulSoup is not None:
        soup = BeautifulSoup(raw, "html.parser")
        hrefs = [a.get("href") for a in soup.find_all("a") if a.get("href")]
        for p in soup.find_all("p"):
            p.insert_before("\n")
        text = soup.get_text(" ", strip=True)
    else:
        hrefs = _URL_RE.findall(raw)
        text = re.sub(r"<p>", "\n", raw, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text, hrefs


def parse_comment(item: dict | None, thread_date: str = "") -> dict | None:
    """Parse one top-level comment into a JobSpy-shaped row, or None if it's
    not a usable posting (deleted/dead, or too short for Stage 1)."""
    if not item or item.get("type") != "comment":
        return None
    if item.get("deleted") or item.get("dead"):
        return None
    text, hrefs = _clean_html(item.get("text") or "")
    # Stage 1 skips anything under 100 chars; drop here so it never counts.
    if len(text) < 100:
        return None

    first_line = text.split("\n", 1)[0].strip()

    # Company + role: the leading "Company | Role | Location | ..." convention.
    company, title = "", ""
    if "|" in first_line:
        segs = [s.strip() for s in first_line.split("|")]
        company = segs[0]
        if len(segs) > 1:
            title = segs[1]
    else:
        parts = _DASH_SPLIT_RE.split(first_line, maxsplit=1)
        if len(parts) > 1:
            company, title = parts[0].strip(), parts[1].strip()
    if len(company) > 60:
        company = ""
    if not title or len(title) > 100:
        title = first_line[:100]

    url = ""
    for h in hrefs:
        if h and "news.ycombinator.com" not in h:
            url = h
            break
    if not url:
        m = _URL_RE.search(text)
        if m:
            url = m.group(0)
    domain = url.split("//", 1)[-1].split("/", 1)[0] if url else ""

    # Remote flag: tri-state (True/False/None) so the shared remote filter works.
    if _REMOTE_RE.search(text):
        is_remote = True
    elif _ONSITE_RE.search(text):
        is_remote = False
    else:
        is_remote = None

    cid = item.get("id")
    # Comment time, not the monthly thread date, is the posting's real age.
    t = item.get("time")
    if t:
        date_posted = datetime.fromtimestamp(t, tz=timezone.utc).date().isoformat()
    else:
        date_posted = thread_date

    return {
        "id": f"hn_{cid}",
        "title": title,
        "company": company,
        "company_url_direct": domain,
        "location": "",
        "job_type": "",
        "min_amount": None,
        "max_amount": None,
        "currency": "",
        "site": "hn",
        "job_url": HN_COMMENT_URL.format(cid),
        "description": text,
        "date_posted": date_posted,
        "is_remote": is_remote,
    }


def scrape_hn_jobs(cfg: dict) -> list[dict]:
    """Scrape the current 'Who is hiring?' thread into JobSpy-shaped rows.
    Bounded by hn_max_jobs. Any failure is non-fatal → returns [] (mirrors YC)."""
    max_jobs = int(cfg.get("hn_max_jobs", 200))
    thread = find_latest_hiring_thread()
    if not thread:
        return []
    log.info(f"[hn] thread: {thread['title']} (id={thread['id']}, {thread['date']})")
    kids = fetch_thread_comments(thread["id"])
    if not kids:
        log.warning("[hn] thread has no comments (skipping)")
        return []
    kids = kids[:max_jobs]
    thread_date = thread.get("date", "")

    rows: list[dict] = []
    try:
        with requests.Session() as session:
            with ThreadPoolExecutor(max_workers=8) as pool:
                items = pool.map(lambda c: _fetch_item(c, session), kids)
        for item in items:
            row = parse_comment(item, thread_date)
            if row:
                rows.append(row)
    except Exception as e:
        log.warning(f"[hn] comment fetch failed (returning what we have): {e}")
    return rows
