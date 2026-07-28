"""
core/domains.py

Turn a company name into a domain we can actually read, without a search key.

Why this exists: listings from LinkedIn arrive with a company NAME and nothing
else. In one overnight run, 570 of 575 newly-seen companies had no domain on
file - so the site-fetch path in enrich.py could never fire, and 98% of them
fell straight through to a paid search call. We were renting a search engine to
find a homepage.

Clearbit's autocomplete endpoint is free and needs no key, but it fuzzy-matches
hard: "MrBeast" comes back as mrbeastburger.com and "Kira" as kirammo.com.
So nothing it returns is trusted until the page says the company's name back to
us. A wrong domain is worse than no domain - it produces confident research
about the wrong company, which is the one failure this project refuses to ship.
"""

from __future__ import annotations

import logging
import re
import threading

import requests

log = logging.getLogger(__name__)

SUGGEST_URL = "https://autocomplete.clearbit.com/v1/companies/suggest"
TIMEOUT = 8

# words that carry no identity - matching on these would let "Global Tech Inc"
# verify against any consultancy on earth
_NOISE = {
    "inc", "llc", "ltd", "limited", "gmbh", "bv", "nv", "sa", "ag", "plc",
    "co", "corp", "corporation", "company", "group", "holdings", "holding",
    "technologies", "technology", "tech", "solutions", "systems", "services",
    "consulting", "consultancy", "partners", "labs", "lab", "studio", "studios",
    "software", "digital", "global", "international", "worldwide", "the", "and",
    "staffing", "recruitment", "recruiting", "talent", "agency",
}

_cache: dict[str, str | None] = {}
_lock = threading.Lock()


def _tokens(name: str) -> set[str]:
    """Identity-carrying lowercase words from a company name."""
    words = re.split(r"[^a-z0-9]+", (name or "").lower())
    return {w for w in words if len(w) > 2 and w not in _NOISE}


def suggest(name: str) -> list[dict]:
    """Free, keyless name -> candidate domains. Never raises."""
    if not (name or "").strip():
        return []
    try:
        r = requests.get(SUGGEST_URL, params={"query": name}, timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        return [c for c in (r.json() or []) if isinstance(c, dict)]
    except Exception as e:                       # network, JSON, anything
        log.debug(f"[domains] suggest failed for {name!r}: {e}")
        return []


# a one-word company can legitimately own <word>hq.com or <word>app.com, but
# "Prima" does NOT own primagames.com. Anything past this list is someone else.
_OK_SUFFIX = ("", "hq", "app", "io", "ai", "hr", "inc", "co", "corp", "group",
              "labs", "software", "tech", "team", "jobs", "careers", "get",
              "join", "try", "use", "the")


def looks_like(candidate_name: str, candidate_domain: str, wanted: str) -> bool:
    """Does this suggestion plausibly belong to the company we asked about?

    Cheap, no network. Multi-word names are specific enough that one shared
    identity word is convincing ("Emerging Travel Group"). Single-word names
    are not - "Prima" shares a word with primagames.com and with any other
    company on earth whose name starts the same way - so those must own the
    domain label outright, give or take a boring suffix."""
    want = _tokens(wanted)
    if not want:
        return False
    label = candidate_domain.split(".")[0].lower()
    if len(want) == 1:
        tok = next(iter(want))
        return any(label == tok + s or label == s + tok for s in _OK_SUFFIX)
    have = _tokens(candidate_name) | _tokens(label)
    return bool(want & have)


def verify(domain: str, name: str, fetch) -> tuple[bool, str]:
    """Read the site and check it says the company's name back to us.

    `fetch` is injected (enrich passes brain1.scrape_markdown) so this module
    stays free of pipeline imports and is trivially testable.
    Returns (ok, page_text) - the text is handed back so the caller doesn't
    pay for the same fetch twice."""
    want = _tokens(name)
    if not want:
        return False, ""
    text = fetch(domain) or ""
    if text.startswith("(fetch failed"):
        return False, ""
    lowered = text.lower()
    # one identity word is enough: "Emerging Travel Group" won't print its
    # full legal name on the homepage, but "emerging" or "travel" will show
    return (any(w in lowered for w in want), text)


def resolve(name: str, fetch) -> tuple[str, str]:
    """Company name -> (verified domain, page text). ('', '') when unsure.

    Deliberately conservative: we would rather fall back to a search call than
    attach the wrong company's website to a listing."""
    key = (name or "").strip().lower()
    if not key:
        return "", ""
    with _lock:
        if key in _cache:
            cached = _cache[key]
            return (cached or ""), ""      # text not cached, only the verdict

    found, text, how = "", "", ""
    for cand in suggest(name)[:3]:
        dom = (cand.get("domain") or "").strip().lower()
        if not dom:
            continue
        if not looks_like(cand.get("name") or "", dom, name):
            continue
        ok, page = verify(dom, name, fetch)
        if ok:
            found, text, how = dom, page, "suggest"
            break

    if not found:
        found, text = _guess_dotcom(name, fetch)
        how = "guess"

    with _lock:
        _cache[key] = found or None
    if found:
        log.info(f"[domains] {name!r} -> {found} ({how}, verified)")
    return found, text


def _guess_dotcom(name: str, fetch) -> tuple[str, str]:
    """Vendor-free floor: a one-word company usually owns <word>.com.

    .com ONLY, and the reason is the whole trick: .com is the most contested
    namespace there is, so a one-word company that owns <word>.com is almost
    certainly the principal holder of that name. Ownership IS the disambiguator.

    That property transfers to no other TLD. Trying .io/.ai/.co resolved more
    names and resolved them wrong - not to squatters, which is what I assumed,
    but to OTHER REAL COMPANIES sharing the name: prima.ai is a manufacturing
    firm, kira.io sells a phone agent, neither is the employer we were asked
    about. No page-quality check can separate those, because nothing is wrong
    with the page. Don't extend this list without a second signal tying the
    site to the actual listing - and the listings don't carry one (checked:
    2 of 1,694).

    This exists so the resolver keeps working when the suggest endpoint doesn't:
    Clearbit's name-to-domain API was sunset in 2025 and autocomplete is now an
    unsupported HubSpot leftover that could vanish without notice."""
    toks = _tokens(name)
    if len(toks) != 1:
        return "", ""              # multi-word names are too varied to guess
    ok, page = verify(f"{next(iter(toks))}.com", name, fetch)
    return (f"{next(iter(toks))}.com", page) if ok else ("", "")
