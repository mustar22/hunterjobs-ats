"""
pipeline/sources/hh.py

HeadHunter (hh.uz / hh.kz / hh.by) as a job source. Covers Russia, Uzbekistan,
Kazakhstan, Belarus and the rest of the CIS, which nothing else here reaches.

No API key. The documented REST API at api.hh.ru now answers 403 to anonymous
reads, but every search page embeds its whole state as escaped JSON in a
<template id="HH-Lux-InitialState">, so the structured data is right there.

The regional domains are interchangeable front doors over one index: hh.uz,
hh.kz and hh.by return identical counts AND identical vacancy ids for the same
query. `area` is what actually selects a market, not the hostname. hh.ru itself
answers 451 from some countries, so it's deliberately not the default.

Emits the same row shape as the LinkedIn/YC/HN sources.
"""

from __future__ import annotations

import html
import json
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger(__name__)

HOST = "hh.uz"                 # any regional door works; this one isn't geo-walled
SEARCH_URL = "https://{host}/search/vacancy"
_STATE_RE = re.compile(r'id="HH-Lux-InitialState"[^>]*>(.*?)</template>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0.0.0 Safari/537.36")

PAGE = 50                      # what a search page actually returns
DEPTH_LIMIT = 40               # page 40 is HTTP 404: 2000 results, hard stop
DELAY = (4.0, 8.0)
RETRIES = 2                    # timeouts here are throttling, not a wall

# a few useful area ids; the full tree is at /areas on the public site
AREAS = {"russia": 113, "uzbekistan": 97, "kazakhstan": 40, "belarus": 16,
         "moscow": 1, "tashkent": 2759}

_REMOTE_RE = re.compile(r"удал[её]нн|remote|дистанцион", re.I)


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"user-agent": UA, "accept": "text/html",
                      "accept-language": "ru,en;q=0.9"})
    return s


def _state(session, url: str, params: dict, timeout: int = 25):
    """One search page as parsed state. Returns (status, data).

    status "ok" | "empty" | "error" — a timeout is throttling, never an ending.
    """
    try:
        r = session.get(url, params=params, timeout=timeout)
    except Exception as e:
        log.debug(f"[hh] {params.get('page')}: {type(e).__name__}")
        return "error", None
    if r.status_code == 404:
        return "empty", None                 # past the depth cap
    if r.status_code != 200:
        return "error", None
    m = _STATE_RE.search(r.text)
    if not m:
        log.warning("[hh] page state blob missing — layout changed?")
        return "error", None
    try:
        return "ok", json.loads(html.unescape(m.group(1)))
    except Exception:
        return "error", None


def _text(raw: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", html.unescape(raw or ""))).strip()


def work_formats(v: dict) -> set[str]:
    """hh ships this as workFormats, PLURAL, a list of dicts each wrapping a
    list. Values are REMOTE / ON_SITE / HYBRID. Singular workFormat is always
    None, which quietly made every listing look non-remote."""
    out: set[str] = set()
    for f in v.get("workFormats") or []:
        out.update(f.get("workFormatsElement") or [])
    return out


def parse_vacancy(v: dict) -> dict | None:
    """One search-result vacancy into a row. None when there's no id."""
    vid = v.get("vacancyId")
    if not vid:
        return None
    comp = v.get("company") or {}
    money = v.get("compensation") or {}
    area = (v.get("area") or {}).get("name") or ""
    title = v.get("name") or ""
    # hh states the work format outright, so trust it and keep the regex as a
    # fallback for the rows that don't. None stays None: unknown, not "no".
    fmts = work_formats(v)
    if fmts:
        remote = "REMOTE" in fmts
    else:
        remote = True if _REMOTE_RE.search(f"{title} {area}") else None
    return {
        "id": f"hh-{vid}",
        "title": title,
        "company": comp.get("visibleName") or comp.get("name") or "",
        "company_url_direct": "",
        "company_url": (comp.get("@url") or ""),
        "location": area,
        "job_type": "",
        # hh actually publishes salary, unlike most boards. from/to are already
        # normalised to a month - perModeFrom/perModeTo hold the raw per-shift
        # figure, so a 8000/shift job reads as 120000 here, which is what the
        # monthly floor wants.
        "min_amount": money.get("from"),
        "max_amount": money.get("to"),
        "currency": money.get("currencyCode") or "",
        # gross vs net is a ~13% swing; a number without it isn't defensible
        "salary_gross": "" if money.get("gross") is None else
                        ("gross" if money["gross"] else "net"),
        "site": "hh",
        "job_url": ((v.get("links") or {}).get("desktop")
                    or f"https://{HOST}/vacancy/{vid}"),
        "description": "",
        # exact timestamps, not the day-granular dates most boards give
        "date_posted": v.get("creationTime") or "",
        "is_remote": remote,
    }


def fetch_description(job_url: str, session, timeout: int = 25) -> str:
    """Descriptions aren't on the search page — one fetch per vacancy."""
    try:
        r = session.get(job_url, timeout=timeout)
        r.raise_for_status()
    except Exception:
        return ""
    m = _STATE_RE.search(r.text)
    if not m:
        return ""
    try:
        d = json.loads(html.unescape(m.group(1)))
    except Exception:
        return ""
    return _text((d.get("vacancyView") or {}).get("description") or "")


def scrape_hh_jobs(hours: int = 168, area: int | str = "uzbekistan",
                   text: str = "", host: str = HOST,
                   limit: int | None = None,
                   with_descriptions: bool = True,
                   stats: dict | None = None) -> list[dict]:
    """Vacancies posted in the last `hours` for one area, newest first.

    `area` takes an id or a name from AREAS. `text` empty = everything in that
    area. Stops for one stated reason: the window edge (complete), the depth
    cap at 2000 (partial), or the results ending."""
    area_id = AREAS.get(str(area).lower(), area)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    session = _session()
    rows: dict[str, dict] = {}
    page = failed = stale = 0
    reason, oldest = "end of results", ""

    while page < DEPTH_LIMIT:
        # search_period filters server-side (916k -> 127k for one day). No
        # order_by: hh ignores it in practice - "publication_time" returned
        # April before July, so position says nothing about age and an early
        # stop would throw away everything after the first old row.
        params = {"area": area_id, "page": page,
                  "search_period": max(1, round(hours / 24))}
        if text:
            params["text"] = text
        status = data = None
        for attempt in range(RETRIES + 1):
            status, data = _state(session, SEARCH_URL.format(host=host), params)
            if status != "error":
                break
            time.sleep(random.uniform(*DELAY) * (attempt + 1))
        if status == "error":
            failed += 1
            reason = "request failures"
            break
        if status == "empty":
            reason = f"depth cap ({DEPTH_LIMIT} pages)"
            break

        items = ((data.get("vacancySearchResult") or {}).get("vacancies") or [])
        if not items:
            break
        stop = False
        for v in items:
            row = parse_vacancy(v)
            if not row:
                continue
            if row["date_posted"]:
                try:
                    when = datetime.fromisoformat(row["date_posted"])
                except ValueError:
                    when = None
                if when and when < cutoff:
                    stale += 1           # skip it, keep going: not sorted
                    continue
                if when:
                    oldest = min(oldest or row["date_posted"], row["date_posted"])
            rows.setdefault(row["id"], row)
            if limit and len(rows) >= limit:
                reason, stop = f"hit limit={limit}", True
                break
        if stop:
            break
        page += 1
        time.sleep(random.uniform(*DELAY))
    else:
        reason = f"depth cap ({DEPTH_LIMIT} pages = {DEPTH_LIMIT * PAGE} results)"

    out = list(rows.values())
    if with_descriptions:
        for row in out:
            row["description"] = fetch_description(row["job_url"], session)
            time.sleep(random.uniform(1.0, 2.5))
    session.close()

    log.info(f"[hh] {page + 1} pages -> {len(out)} vacancies, back to "
             f"{oldest[:10] or 'n/a'} (area={area_id}, window {hours}h, "
             f"text={text or 'none'}) — {stale} outside window — stopped: {reason}")
    if failed:
        log.warning(f"[hh] gave up on {failed} page(s) — coverage is incomplete")
    if stats is not None:
        stats.update(pages=page + 1, vacancies=len(out), oldest=oldest,
                     area=area_id, reason=reason, failed=failed, stale=stale,
                     complete=reason.startswith(("end of results", "hit limit")))
    return out
