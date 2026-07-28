"""
pipeline/enrich.py

One enrichment pass per company: research + contacts in a single LLM call,
cached in the companies table. Replaces the separate Stage 2 / Stage 3 calls.

Input gathering order (cheap and deterministic first):
  posting emails (free) → YC profile props (founders + descriptions, one fetch)
  → company site (homepage → /about → ddgs snippets) → team-page crawl →
  GitHub org → one LLM call over everything gathered.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from html import unescape

import requests

import core.companies as companies
import core.domains as domains
from core.schemas import CompanyEnrichment
import pipeline.brain1 as b1

log = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
# HN-style obfuscation: "jane at acme dot com", "jane (at) acme (dot) com"
_OBFUSCATED_RE = re.compile(
    r"\b([a-zA-Z0-9._%+\-]+)\s*[\(\[]?\s*at\s*[\)\]]?\s*"
    r"([a-zA-Z0-9\-]+)\s*[\(\[]?\s*dot\s*[\)\]]?\s*([a-zA-Z]{2,})\b", re.I)
_JUNK_EMAIL = ("noreply", "no-reply", "example.com", "yourdomain", "email.com",
               "sentry", ".png", ".jpg", ".gif", ".webp")

_DATA_PAGE_RE = re.compile(r'data-page="([^"]+)"')
_TEAMISH_HREF_RE = re.compile(r"(team|about|people|leadership|founders|company)", re.I)

_NULLISH = ("null", "none", "n/a", "na", "undefined", "-", "—")


def _clean_str(s, max_len: int = 100) -> str:
    """Kill LLM string junk: literal 'null'/'none', overlong values."""
    s = (s or "").strip()
    if s.lower() in _NULLISH:
        return ""
    return s[:max_len]


def posting_emails(text: str) -> list[dict]:
    """Contact emails baked into the job posting itself — the company telling
    you where to apply. Highest confidence there is, zero cost."""
    text = text or ""
    found: list[str] = []
    for e in _EMAIL_RE.findall(text):
        found.append(e)
    for m in _OBFUSCATED_RE.finditer(text):
        found.append(f"{m.group(1)}@{m.group(2)}.{m.group(3)}")
    out, seen = [], set()
    for e in found:
        el = e.lower().strip(".")
        if el in seen or any(j in el for j in _JUNK_EMAIL):
            continue
        seen.add(el)
        out.append({"name": "", "title": "listed in posting", "email": el,
                    "source": "listing", "confidence": "verified"})
    return out


def fetch_yc_company(slug: str, timeout: int = 15) -> dict | None:
    """Founders + structured descriptions from the public YC company page
    (same react_on_rails props the WaaS scraper parses). None on any failure."""
    if not slug:
        return None
    try:
        r = requests.get(
            f"https://www.ycombinator.com/companies/{slug}",
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (compatible; HunterJobsBot/1.0)",
                     "Accept": "text/html"},
        )
        r.raise_for_status()
        m = _DATA_PAGE_RE.search(r.text)
        if not m:
            return None
        comp = (json.loads(unescape(m.group(1))).get("props") or {}).get("company") or {}
    except Exception as e:
        log.warning(f"[enrich] YC page fetch failed for '{slug}' (skipping): {e}")
        return None
    if not comp:
        return None
    founders = []
    for f in comp.get("founders") or []:
        name = _clean_str(f.get("full_name"))
        if not name:
            continue
        founders.append({
            "name": name,
            "title": _clean_str(f.get("title")) or "Founder",
            "email": "",
            "source": "yc",
            "confidence": "verified",
            "bio": _clean_str(f.get("founder_bio"), 300),
        })
    return {
        "founders": founders,
        "one_liner": _clean_str(comp.get("one_liner"), 300),
        "long_description": _clean_str(comp.get("long_description"), 2000),
        "hiring_description": _clean_str(comp.get("hiring_description"), 1000),
        "year_founded": comp.get("year_founded"),
        "location": _clean_str(comp.get("location")),
        "team_size": comp.get("team_size"),
        "linkedin_url": _clean_str(comp.get("linkedin_url"), 200),
    }


def _fetch_ok(text: str) -> bool:
    return bool(text) and not text.startswith("(fetch failed") and len(text) >= 400


def gather_site_content(company: str, domain: str,
                        client=None, model=None, backend=None) -> tuple[str, str, list[str]]:
    """Best available company text: homepage → /about → search snippets.
    Returns (text, tag, source_urls). Never feeds a raw '(fetch failed: ...)'
    string to the model."""
    cdomain = b1.clean_domain(domain)
    if cdomain:
        text = b1.scrape_markdown(cdomain)
        if _fetch_ok(text):
            return text, "website", [f"https://{cdomain}"]
        text = b1.scrape_markdown(f"{cdomain}/about")
        if _fetch_ok(text):
            return text, "website", [f"https://{cdomain}/about"]
    # last resort: search snippets, so the model judges SOMETHING real
    from core import websearch
    results = websearch.search(f'"{company}" company')
    snippets = websearch.snippets_text(results)
    if snippets:
        return snippets, "search", [r["url"] for r in results if r.get("url")][:3]
    return "", "none", []


def crawl_team_contacts(domain: str, company: str, limit: int = 8) -> list[dict]:
    """Fixed team paths first (existing behavior); when they find nothing,
    discover team-ish links from the homepage nav and follow same-domain."""
    contacts = b1.scrape_team_contacts(domain, company, limit=limit)
    if contacts:
        return contacts
    cdomain = b1.clean_domain(domain)
    if not cdomain:
        return []
    try:
        from bs4 import BeautifulSoup
        r = requests.get(f"https://{cdomain}", timeout=5,
                         headers={"User-Agent": "Mozilla/5.0 (compatible; HunterJobsBot/1.0)"})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception:
        return []
    seen_paths: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        blob = f"{href} {a.get_text(' ', strip=True)}"
        if not _TEAMISH_HREF_RE.search(blob):
            continue
        if href.startswith("http") and cdomain not in href:
            continue
        path = href if href.startswith("/") else href.split(cdomain, 1)[-1]
        path = path.split("#")[0].split("?")[0]
        if not path.startswith("/") or path in seen_paths:
            continue
        seen_paths.add(path)
        if len(seen_paths) >= 4:
            break
    out: list[dict] = []
    seen_names: set[str] = set()
    for path in seen_paths:
        try:
            r = requests.get(f"https://{cdomain}{path}", timeout=5,
                             headers={"User-Agent": "Mozilla/5.0 (compatible; HunterJobsBot/1.0)"})
            r.raise_for_status()
        except Exception:
            continue
        for c in b1._extract_team_from_html(r.text, company):
            if c["name"].lower() in seen_names:
                continue
            seen_names.add(c["name"].lower())
            out.append(c)
            if len(out) >= limit:
                return out
    return out


def _build_prompt(company: str, domain: str, yc: dict | None,
                  site_text: str, site_tag: str) -> str:
    parts = [f"Company: {company}", f"Domain: {domain or 'unknown'}"]
    if yc:
        yc_bits = []
        if yc.get("one_liner"):
            yc_bits.append(f"One-liner: {yc['one_liner']}")
        if yc.get("long_description"):
            yc_bits.append(f"Description: {yc['long_description']}")
        if yc.get("hiring_description"):
            yc_bits.append(f"Hiring pitch: {yc['hiring_description']}")
        if yc.get("year_founded"):
            yc_bits.append(f"Founded: {yc['year_founded']}")
        if yc.get("team_size"):
            yc_bits.append(f"Team size: {yc['team_size']}")
        if yc.get("founders"):
            yc_bits.append("Founders: " + "; ".join(
                f"{f['name']} ({f['title']})" for f in yc["founders"]))
        parts.append("=== YC PROFILE (verified) ===\n" + "\n".join(yc_bits))
    if site_text:
        label = "WEBSITE CONTENT" if site_tag == "website" else "WEB SEARCH SNIPPETS"
        parts.append(f"=== {label} ===\n{site_text}")
    else:
        parts.append("=== NO COMPANY CONTENT AVAILABLE ===")
    return "\n\n".join(parts)


_SYSTEM = (
    "You are a company OSINT analyst. Be brief and factual.\n"
    "hiring_signal: looks_real if active hiring signs, ghost if posts old/empty/"
    "evasive, uncertain if unclear.\n"
    "company_size: tiny (<50), mid (50-500), enterprise (500+).\n"
    "culture_flags: MUST include the literal string 'staffing_agency' if the company "
    "is a staffing firm, recruiting agency, gig platform, body shop, data labeling "
    "service, or any business that hires people to place them at other companies. "
    "Also include 'data_labeling' for AI training/labeling/RLHF services. "
    "Other red flags as plain strings. Empty list if none.\n"
    "real_stack: confirmed tech only, empty list if nothing found.\n"
    "people: real people explicitly named in the content as founders/leadership, "
    "with their titles. NEVER invent anyone. Empty list if no one is clearly named."
)


def _seed_research(conn, key: str) -> dict | None:
    """Research imported from hunterjobsats.com, in the cache-row shape.
    contacts/hunted stay empty so the local contact hunt still happens."""
    if not key:
        return None
    try:
        r = conn.execute(
            """SELECT company_key, name, domain, yc_slug, company_summary,
                      hiring_signal, real_stack, culture_flags, company_size,
                      sources, researched_at
               FROM companies_seed WHERE company_key = ?""", (key,)).fetchone()
    except Exception:
        return None                      # no seed table = nothing imported yet
    if not r:
        return None
    d = dict(r)
    for f in ("real_stack", "culture_flags", "sources"):
        try:
            d[f] = json.loads(d.get(f) or "[]")
        except (json.JSONDecodeError, TypeError):
            d[f] = []
    d["contacts"], d["hunted"] = [], 0
    return d


def enrich_company(conn, cfg: dict, company: str, domain: str,
                   client=None, model=None, backend=None,
                   yc_slug: str = "", skip_hunt: bool = False,
                   force: bool = False, meter=None) -> dict:
    """Research + contacts for one company, cache-first. Returns the cache-row
    dict shape (research fields + contacts list + hunted flag + from_cache)."""
    key = companies.company_key(company, b1.clean_domain(domain))
    ttl = int(cfg.get("company_ttl_days", 30))
    cached = None if force else companies.get_cached(conn, key, ttl)
    if cached is None and not force:
        # nothing of our own, but the imported seed may already know this
        # company — reuse that research instead of paying for it again. The
        # contact hunt still runs below; contacts are never seeded.
        cached = _seed_research(conn, key)
    if cached and (cached.get("hunted") or skip_hunt):
        cached["from_cache"] = True
        return cached

    yc = fetch_yc_company(yc_slug) if yc_slug else None

    # LinkedIn hands us a name and nothing else, and without a domain every
    # step below (site read, team crawl, github) is blind and falls through to
    # a paid search. Resolve one for free first; unverified stays empty.
    resolved_text = ""
    if not b1.clean_domain(domain):
        if yc and (yc.get("website") or ""):
            domain = yc["website"]
        else:
            found, resolved_text = domains.resolve(company, b1.scrape_markdown)
            if found:
                domain = found

    # contact hunt (network-only, no LLM): skipped when the posting already
    # carries an email — founders from YC still ride along for free
    team, gh = [], []
    hunted = False
    if not skip_hunt:
        hunted = True
        # founders known = skip the team-page crawl; at tiny YC startups the
        # founders ARE the targets, and generic pages surface customer
        # testimonials as "team" (ShortLoop case)
        if not (yc or {}).get("founders"):
            team = crawl_team_contacts(domain, company)
        keys = b1.load_keys()
        gh = [{
            "name": g.get("name") or "", "title": g.get("title") or "",
            "email": g.get("email") or "", "source": "github",
            "confidence": ("verified" if g.get("email") else "reported"),
        } for g in b1.github_contacts(company, keys.get("github", ""), domain)]

    # trust receipts: every URL the research actually read
    sources: list[dict] = []
    if yc:
        sources.append({"label": "YC profile",
                        "url": f"https://www.ycombinator.com/companies/{yc_slug}"})

    # research inputs + the one LLM call
    if cached and not force:
        # fresh research already cached; we only owed the hunt
        result = dict(cached)
        sources = (cached.get("sources") or []) or sources
    else:
        if _fetch_ok(resolved_text):
            # verify() already downloaded this page — don't pay for it twice
            site_text, site_tag = resolved_text, "website"
            site_urls = [f"https://{b1.clean_domain(domain)}"]
        else:
            site_text, site_tag, site_urls = gather_site_content(
                company, domain, client, model, backend)
        label = "Company site" if site_tag == "website" else "Web search"
        sources += [{"label": label, "url": u} for u in site_urls]
        prompt = _build_prompt(company, domain, yc, site_text, site_tag)
        llm_people: list[dict] = []
        try:
            r = b1.call_gemma(client, model, backend, _SYSTEM, prompt,
                              CompanyEnrichment, stage="stage2",
                              max_output_tokens=1536)
            if meter is not None:
                meter.count("stage2_runs")
            summary = r.company_summary
            if b1._is_degenerate(summary):
                summary = "Info unavailable."
                r.hiring_signal = "uncertain"
            else:
                summary = b1._sanitize_summary(summary)
            for p in r.people:
                nm = _clean_str(p.name)
                # strict person-name guard: kills placeholders like
                # "name_not_found" and marketing headings, not just loops
                if nm and b1._plausible_name(nm) and b1._is_real_person_name(nm, company):
                    llm_people.append({"name": nm, "title": _clean_str(p.title),
                                       "email": "", "source": "web",
                                       "confidence": "reported"})
            result = {
                "company_summary": summary,
                "hiring_signal": r.hiring_signal,
                "real_stack": r.real_stack,
                "culture_flags": r.culture_flags,
                "company_size": r.company_size,
            }
        except Exception as e:
            log.warning(f"[enrich] LLM enrichment failed for '{company}': {e}")
            result = {"company_summary": "Info unavailable.",
                      "hiring_signal": "uncertain", "real_stack": [],
                      "culture_flags": [], "company_size": "tiny"}
        result["_people"] = llm_people

    yc_founders = [dict(f) for f in (yc or {}).get("founders") or []]
    for f in yc_founders:
        f.pop("bio", None)
    prev_contacts = (cached or {}).get("contacts") or []
    merged = b1._merge_contacts([yc_founders, team, gh,
                                 result.pop("_people", []), prev_contacts])
    cdomain = b1.clean_domain(domain)
    if cdomain:
        for c in merged:
            if c.get("name") and not c.get("email"):
                cands = b1.permutation_emails(c["name"], cdomain)
                if cands:
                    c["email"] = cands[0]
                    c["confidence"] = "pattern"
                    c["source"] = f"{c['source']}+permutation"
        if hunted and not any(c.get("source") == "permutation" for c in merged):
            for local in ("founder", "hello"):
                merged.append({"name": "", "title": "Founder / Eng lead — unverified",
                               "email": f"{local}@{cdomain}",
                               "source": "permutation", "confidence": "pattern"})
    for c in merged:
        c.setdefault("name", ""); c.setdefault("title", ""); c.setdefault("email", "")
        c["title"] = _clean_str(c["title"])
    merged.sort(key=lambda c: (b1._CONF_TIER.get(c.get("confidence"), 3),
                               b1._title_rank(c.get("title", ""))))

    out = {
        "name": company, "domain": cdomain, "yc_slug": yc_slug or "",
        "contacts": merged,
        "sources": sources,
        "hunted": bool(hunted or (cached or {}).get("hunted")),
        "researched_at": datetime.now(timezone.utc).isoformat(),
        "from_cache": False,
        **{k: result[k] for k in ("company_summary", "hiring_signal",
                                  "real_stack", "culture_flags", "company_size")},
    }
    companies.save(conn, key, out)
    if meter is not None and hunted:
        meter.count("stage3_runs")
    return out
