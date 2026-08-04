"""
brain1.py

Sequential three-stage job intelligence pipeline.

  Stage 1 (scrape + filter):
      scrape LinkedIn -> hard-reject pass -> Gemma 1 filter -> DB write.
      GOOD jobs collected into in-memory list. MAYBE/BAD stop here.

  Stage 2 (research, runs AFTER Stage 1):
      iterate GOODs sequentially -> scrape company site -> Gemma 2 research
      -> update DB. Demote to BAD if staffing/labeling agency detected.
      Survivors continue to Stage 3.

  Stage 3 (outreach, runs AFTER Stage 2):
      iterate survivors sequentially -> GitHub OSINT + email permutation +
      Gemma 3 outreach -> update DB.

Status heartbeat written to runner_status.json after every job so the
dashboard can show live progress. A watchdog thread hard-kills the process
if the dashboard heartbeat dies for >90s.

Module also exposes two single-job entry points for the dashboard's MAYBE
manual buttons:
    enrich_company_for_job(job_id)   -> runs Gemma 2 for one job
    find_contact_for_job(job_id)     -> runs Gemma 3 for one job
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as md
from google import genai
from google.genai import types
from openai import OpenAI

from core.config import OPENROUTER_URL
from core.database import get_db_connection, init_db
from core.schemas import JobFilter, CompanyResearch
from pipeline.sources import hn
from pipeline.sources import linkedin as li
from pipeline.metering import ScanMeter
import core.embeddings as embeddings
import core.ledger as ledger
import core.runner_status as runner_status


# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("hunterjobs.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

from core.config import CONFIG_PATH


# ── config / keys ─────────────────────────────────────────────────────────────
def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {}


def load_keys() -> dict:
    try:
        import keys
        return {
            "google": getattr(keys, "GOOGLE_API_KEY", ""),
            "github": getattr(keys, "GITHUB_PAT", ""),
            "openrouter": getattr(keys, "OPENROUTER_API_KEY", ""),
        }
    except ImportError:
        return {"google": "", "github": "", "openrouter": ""}


# ── LLM client factory ────────────────────────────────────────────────────────
DEFAULT_GEMMA_MODEL = "gemma-4-26b-a4b-it"


def get_gemma_client_for_stage(cfg: dict, keys: dict, stage_group: str):
    """stage_group: 'stage1', 'stage2', 'stage3' (or legacy 'stage23'). Stage 1
    has its own backend; stages 2/3 share the 'stage23' backend but each picks
    its own Gemma model."""
    backend_group = "stage1" if stage_group == "stage1" else "stage23"
    backend = cfg.get(f"brain1_{backend_group}_backend") or cfg.get("brain1_backend", "gemma")

    # per-stage model, falling back to the old shared key
    def _model(name: str, default: str = "") -> str:
        return ((cfg.get(f"brain1_{backend_group}_{name}_model")
                 or cfg.get(f"brain1_{name}_model") or default).strip())

    if backend == "lmstudio":
        base_url = cfg.get("brain1_lmstudio_url", "http://localhost:1234/v1")
        model_name = _model("lmstudio")
        if not model_name:
            try:
                r = requests.get(f"{base_url.rstrip('/')}/models", timeout=5)
                r.raise_for_status()
                models = r.json().get("data", [])
                if models:
                    model_name = models[0]["id"]
                    log.info(f"LM Studio auto-detected model: {model_name}")
                else:
                    log.error("LM Studio returned no loaded models.")
                    model_name = "no-model-loaded"
            except Exception as e:
                log.error(f"Could not query LM Studio at {base_url}/models: {e}")
                model_name = "lmstudio-unavailable"
        return (
            OpenAI(base_url=base_url, api_key="lm-studio"),
            model_name,
            "lmstudio",
        )

    if backend == "openrouter":
        model_name = _model("openrouter", "openrouter/free")
        return (
            OpenAI(base_url=OPENROUTER_URL, api_key=keys.get("openrouter", "")),
            model_name,
            "openrouter",
        )

    if backend == "anthropic":
        import anthropic  # lazy, same as brain2 — optional dep
        model_name = _model("anthropic", "claude-haiku-4-5")
        return anthropic.Anthropic(api_key=keys.get("anthropic", "")), model_name, "anthropic"

    # legacy 'stage23' resolves to the stage-2 model field.
    model_key = "stage2" if stage_group == "stage23" else stage_group
    model_name = (cfg.get(f"brain1_{model_key}_gemma_model") or DEFAULT_GEMMA_MODEL).strip()
    return genai.Client(api_key=keys["google"]), model_name, "gemma"


# Single-stage helper for the manual MAYBE buttons (stage2=research, stage3=contact).
def get_gemma_client(cfg: dict, keys: dict, stage_group: str = "stage2"):
    return get_gemma_client_for_stage(cfg, keys, stage_group)


# ── helpers ───────────────────────────────────────────────────────────────────
def description_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8", errors="ignore")).hexdigest()


def hard_reject_check(text: str, rejects: list[str]) -> str | None:
    # Whole-token match; re.escape guards regex-special keywords (TS/SCI, W-2).
    for kw in rejects:
        if re.search(r"\b" + re.escape(kw) + r"\b", text, re.IGNORECASE):
            return kw
    return None


def scrape_markdown(url: str, timeout: int = 5, max_chars: int = 10_000) -> str:
    if not url:
        return ""
    if not url.startswith("http"):
        url = f"https://{url}"
    try:
        r = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; HunterJobsBot/1.0)"},
            timeout=timeout,
        )
        r.raise_for_status()
        clean = md(r.text, strip=["script", "style", "nav", "footer"])
        clean = re.sub(r"\n\s*\n", "\n\n", clean).strip()
        return clean[:max_chars]
    except Exception as e:
        return f"(fetch failed: {e})"


# ── contact OSINT: team-page scrape ───────────────────────────────────────────
_NAME_RE = re.compile(r"^[A-Z][a-zA-Z'’.\-]+(?: [A-Z][a-zA-Z'’.\-]+){1,2}$")
# Word-boundary matching so short tokens don't match inside words
# (e.g. "coo" must not match "cookie", "vp" must not match "vpn").
_ROLE_RE = re.compile(
    r"\b(ceo|cto|coo|cfo|chief|co-?founder|founder|president|vice president|vp|"
    r"head of|director|lead|engineer|manager|officer|partner|principal)\b",
    re.I,
)
_DM_RE = re.compile(r"\b(ceo|cto|coo|cfo|chief|co-?founder|founder)\b", re.I)
_LEAD_RE = re.compile(
    r"\b(president|vice president|vp|head of|director|lead|principal)\b", re.I
)


def _looks_like_title(s: str) -> bool:
    s2 = (s or "").strip()
    return 0 < len(s2) <= 60 and bool(_ROLE_RE.search(s2))


def _title_rank(title: str) -> int:
    """0 = decision-maker, 1 = other leadership, 2 = everyone else."""
    t = title or ""
    if _DM_RE.search(t):
        return 0
    if _LEAD_RE.search(t):
        return 1
    return 2


# Tokens that mark a string as a headline/section/brand rather than a person.
_NON_NAME_WORDS = {
    "introducing", "meet", "welcome", "announcing", "presenting", "discover",
    "explore", "learn", "read", "more", "get", "start", "started", "join",
    "our", "your", "the", "we", "us", "about", "contact", "team", "careers",
    "career", "jobs", "home", "pricing", "privacy", "policy", "terms", "blog",
    "news", "login", "signin", "signup", "sign", "new", "now", "today", "free",
    "demo", "book", "request", "company", "mission", "vision", "values",
    "product", "products", "platform", "solutions", "services", "features",
    "ai", "app", "inc", "llc", "ltd", "co", "corp", "gmbh", "io", "hq",
    "labs", "lab", "tech", "world", "first", "best",
    # capability/marketing headings that pass the caps-name regex
    "technical", "expertise", "engineering", "operations", "success",
    "revenue", "insights", "excellence", "innovation", "security",
    "unknown", "found", "name",
}


def _is_real_person_name(name: str, company: str = "") -> bool:
    """Precision-first guard: accept only strings that look like an actual
    person's name (2-3 capitalized tokens), rejecting marketing headlines
    ('Introducing Finn AI'), all-caps brands/acronyms ('Northeast OBGYN'),
    and strings echoing the company/product name."""
    s = (name or "").strip()
    if not _NAME_RE.match(s):
        return False
    toks = s.split()
    if not (2 <= len(toks) <= 3):
        return False
    company_toks = {
        t for t in re.split(r"[^a-z0-9]+", (company or "").lower()) if len(t) > 2
    }
    for t in toks:
        low = t.strip(".'’-").lower()
        if not low or low in _NON_NAME_WORDS:
            return False
        # all-caps acronym/brand token (AI, OBGYN, LLC) — not a given/sur-name
        if len(t) >= 2 and t.isupper():
            return False
        if low in company_toks:  # echoes the company/product brand
            return False
    return True


def _extract_team_from_html(html: str, company: str = "") -> list[dict]:
    """Heuristic team-card extraction: a person-name element with a role string
    nearby. Best-effort — returns only confidently paired (name, title)."""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return []
    found: list[dict] = []
    seen: set[str] = set()
    for el in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6",
                             "strong", "b", "span", "div", "p", "a"]):
        name = el.get_text(" ", strip=True)
        if not _is_real_person_name(name, company) or name.lower() in seen:
            continue
        title = ""
        nearby = []
        sib = el.find_next_sibling()
        if sib:
            nearby.append(sib.get_text(" ", strip=True))
        if el.parent:
            nearby.append(el.parent.get_text(" ", strip=True))
        for chunk in nearby:
            for frag in re.split(r"[\n|•·,/]| - | — ", chunk):
                if _looks_like_title(frag) and name.lower() not in frag.lower():
                    title = frag.strip()
                    break
            if title:
                break
        if title:
            seen.add(name.lower())
            found.append({
                "name": name, "title": title, "email": "",
                "source": "team_page", "confidence": "verified",
            })
    return found


def scrape_team_contacts(domain: str, company: str = "", timeout: int = 5,
                         limit: int = 8) -> list[dict]:
    """Fetch homepage + common team pages, extract real (name, title) pairs.
    Every fetch is isolated in try/except → skips on failure, never raises."""
    cdomain = clean_domain(domain)
    if not cdomain:
        return []
    headers = {"User-Agent": "Mozilla/5.0 (compatible; HunterJobsBot/1.0)"}
    contacts: list[dict] = []
    seen: set[str] = set()
    company_toks = {
        t for t in re.split(r"[^a-z0-9]+", (company or "").lower()) if len(t) > 2
    }
    for path in ("", "/team", "/about", "/about-us", "/company", "/people"):
        try:
            r = requests.get(f"https://{cdomain}{path}", headers=headers, timeout=timeout)
            r.raise_for_status()
            html = r.text
        except Exception:
            continue
        page = _extract_team_from_html(html, company)
        # Wrong-site guard: names found but the page title never references the
        # company → the fetch likely landed on an unrelated site (Prosper case).
        if page and company_toks:
            try:
                title = (BeautifulSoup(html, "html.parser").title.string or "").lower()
            except Exception:
                title = ""
            if title and not any(t in title for t in company_toks):
                log.warning(
                    f"[stage3] team page '{cdomain}{path}' title "
                    f"'{title.strip()[:60]}' doesn't reference '{company}' — "
                    f"names may be from an unrelated site"
                )
        for c in page:
            if c["name"].lower() in seen:
                continue
            seen.add(c["name"].lower())
            contacts.append(c)
            if len(contacts) >= limit:
                return contacts
        if len(contacts) >= 3:  # found a real team page; stop probing further paths
            break
    return contacts


# Web searches go through core.websearch (Tavily/Serper when keyed, ddgs fallback).


# Detection-only repeat regex: a 3+ char chunk immediately repeated. Lower bar
# than _REPEAT_RE (which strips 5+ loops); paired with the coverage test in
# _is_degenerate so incidental doublings ("couscous") don't trip it.
_DEGEN_REPEAT_RE = re.compile(r"(.{3,}?)\1+", re.DOTALL)


_DEGEN_NORM_RE = re.compile(r"[^a-z0-9]+")


def _stutters(text: str, min_unit: int = 9, times: int = 3,
              window: int = 140) -> bool:
    """A long chunk repeating several times close together is a loop.

    Local on purpose: a company name repeating across a paragraph is normal
    writing; the same 9+ characters three times inside 140 is a model stuck in
    a groove ("...m-test,-test-automation, oote-test-automation...")."""
    flat = _DEGEN_NORM_RE.sub("", (text or "").lower())
    for i in range(0, max(0, len(flat) - min_unit)):
        unit = flat[i:i + min_unit]
        nxt, count = i + min_unit, 1
        while count < times:
            j = flat.find(unit, nxt, i + window)
            if j < 0:
                break
            count += 1
            nxt = j + min_unit
        if count >= times:
            return True
    return False


def _is_degenerate(text: str, *, min_tokens: int = 6,
                   distinct_ratio: float = 0.5,
                   repeat_coverage: float = 0.5) -> bool:
    """Detect LLM degeneration — looped words/phrases/substrings — for both short
    names and longer prose. Catches 'stage stage stage…' and
    'unidentifiableifiable'; leaves normal (even mildly repetitive) text alone."""
    s = (text or "").strip()
    if len(s) < 8:
        return False
    toks = s.split()
    # 3+ identical tokens in a row — the clearest loop signal.
    run = 1
    for a, b in zip(toks, toks[1:]):
        run = run + 1 if a.lower() == b.lower() else 1
        if run >= 3:
            return True
    # Enough tokens but few distinct ones → looping.
    if len(toks) >= min_tokens:
        distinct = len(set(t.lower() for t in toks))
        if distinct / len(toks) < distinct_ratio:
            return True
    # A tight (non-spaced) immediate substring repeat that dominates the text;
    # spaced word-loops are left to the token checks above ("very very good").
    for m in _DEGEN_REPEAT_RE.finditer(s):
        unit = m.group(1)
        if (len(unit) >= 4 and not any(c.isspace() for c in unit)
                and len(m.group(0)) / len(s) >= repeat_coverage):
            return True
    # Same word modulo punctuation: "multi--platform-based multi-platform--based
    # multi-platform-based" is three of one word to a reader, three different
    # tokens to str.split().
    norm = [_DEGEN_NORM_RE.sub("", t.lower()) for t in toks]
    run = 1
    for a, b in zip(norm, norm[1:]):
        if not a:
            run = 1
            continue
        run = run + 1 if a == b else 1
        if run >= 3:
            return True
    if _stutters(s):
        return True
    # One stuttered word inside otherwise-fine prose ("...are
    # unidentifiableifiable from the"): whole-text ratios get diluted by the
    # normal words around it, so score long tokens on their own.
    return any(_token_loops(t) for t in toks)


def _token_loops(tok: str, window: int = 6, min_len: int = 14) -> bool:
    """A single word that eats its own tail. Any repeated 6-char window inside
    one long word is degeneration - real words don't do that (checked against
    'internationalization', 'indistinguishable', 'telecommunications', ...)."""
    t = "".join(c for c in tok.lower() if c.isalpha())
    if len(t) < min_len:
        return False
    seen = set()
    for i in range(len(t) - window + 1):
        w = t[i:i + window]
        if w in seen:
            return True
        seen.add(w)
    return False


def _plausible_name(nm: str) -> bool:
    """Reject LLM-degenerate output (over-long, or a looped token/phrase)."""
    return len(nm) <= 100 and not _is_degenerate(nm)


_CONF_TIER = {"verified": 0, "reported": 1, "pattern": 2}


def _merge_contacts(groups: list[list[dict]]) -> list[dict]:
    """Dedupe across sources by email and by name; enrich a kept entry with a
    missing email/title from a later duplicate. Group order = source priority."""
    out: list[dict] = []
    by_name: dict[str, dict] = {}
    seen_emails: set[str] = set()
    for group in groups:
        for c in group:
            name = (c.get("name") or "").strip()
            email = (c.get("email") or "").strip().lower()
            nk = name.lower()
            if email and email in seen_emails:
                continue
            if nk and nk in by_name:
                ex = by_name[nk]
                if email and not ex.get("email"):
                    ex["email"] = c.get("email")
                if not ex.get("title") and c.get("title"):
                    ex["title"] = c["title"]
                if email:
                    seen_emails.add(email)
                continue
            out.append(c)
            if nk:
                by_name[nk] = c
            if email:
                seen_emails.add(email)
    return out


def _clean_company_name(company: str) -> str:
    """Reduce a raw company field to a bare org name for slugifying/search:
    drop parentheticals ("(YC S18, non-profit)"), any trailing descriptor after
    a comma or dash, and common legal suffixes (Inc/Ltd/LLC/...). Returns '' when
    nothing usable remains. So "Enveritas (YC S18, non-profit)" -> "Enveritas"."""
    s = (company or "").strip()
    if not s:
        return ""
    s = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", s)              # strip parentheticals
    s = re.split(r"\s[-–—]\s|,", s)[0]                       # drop trailing descriptor
    s = re.sub(                                             # strip legal suffixes
        r"\b(inc|llc|l\.l\.c|ltd|limited|corp|corporation|co|gmbh|plc|llp|pty|ag)\b\.?",
        " ", s, flags=re.I,
    )
    return re.sub(r"\s+", " ", s).strip(" .,&-")


def github_contacts(company: str, github_pat: str, domain: str = "",
                    limit: int = 5) -> list[dict]:
    """Resolve the company to a GitHub ORG, then return its PUBLIC members as
    real contacts {name, title, email, login, source:"github"}.

    Org membership is public-only here. Many orgs hide their members, so this
    is often sparse or empty — that's fine: the path's only job is "real names
    of people at the org", and Part-2 permutation turns those names into emails.
    No user-search fallback — empty-but-honest beats unrelated strangers. Real
    data only; every request is contained → empty list on any failure.
    """
    if not github_pat:
        return []
    # Company name only, never the job title; '' after cleaning = skip, don't guess.
    company = _clean_company_name(company)
    if not company:
        return []

    headers = {
        "Authorization": f"token {github_pat}",
        "Accept": "application/vnd.github.v3+json",
    }

    def _get(url, quiet_404=False):
        try:
            r = requests.get(url, headers=headers, timeout=5)
            if r.status_code != 200:
                if not (quiet_404 and r.status_code == 404):
                    log.warning(f"[stage3] github {r.status_code} for {url}")
                return None
            return r.json()
        except Exception as e:
            log.warning(f"[stage3] github request failed ({url}): {e}")
            return None

    # Candidate org logins, most-precise first: domain slug, company slug(s).
    candidates: list[str] = []
    cdomain = clean_domain(domain)
    if cdomain:
        candidates.append(cdomain.split(".")[0])
    name_slug = re.sub(r"[^a-z0-9]", "", company.lower())
    name_hyphen = re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-")
    for s in (name_slug, name_hyphen):
        if s and s not in candidates:
            candidates.append(s)

    # Exact org lookups only. No fuzzy name search: a name-match alone surfaces
    # unrelated orgs and their members' personal emails as "company contacts".
    confirmed: list[str] = []
    seen_login: set[str] = set()
    for slug in candidates:
        org = _get(f"https://api.github.com/orgs/{slug}", quiet_404=True)
        if org and org.get("login") and org["login"].lower() not in seen_login:
            seen_login.add(org["login"].lower())
            confirmed.append(org["login"])

    if not confirmed:
        log.info(f"[stage3] github: no org resolved for '{company}'")
        return []

    contacts: list[dict] = []
    seen_member: set[str] = set()
    for org_login in confirmed:
        members = _get(
            f"https://api.github.com/orgs/{org_login}/public_members?per_page=10"
        ) or []
        for m in members:
            mlogin = m.get("login")
            if not mlogin or mlogin in seen_member:
                continue
            seen_member.add(mlogin)
            prof = _get(f"https://api.github.com/users/{mlogin}") or {}
            name = (prof.get("name") or "").strip()
            email = (prof.get("email") or "").strip()
            # Trust the email only if it's on the company domain; a personal
            # gmail/hotmail (or GitHub's noreply alias) proves no affiliation.
            if email and not (cdomain and email.lower().endswith("@" + cdomain)):
                email = ""
            if not name and not email:
                continue  # anonymous handle, nothing usable
            contacts.append({
                "name": name, "title": "", "email": email,
                "login": mlogin, "source": "github",
            })
            if len(contacts) >= limit:
                return contacts
    if not contacts:
        log.info(f"[stage3] github: org(s) {confirmed} expose no usable public members")
    return contacts


def permutation_emails(name: str, domain: str) -> list[str]:
    parts = (name or "").lower().split()
    if not parts:
        return [f"hello@{domain}"]
    if len(parts) == 1:
        return [f"{parts[0]}@{domain}"]
    first, last = parts[0], parts[-1]
    return [
        f"{first}@{domain}",
        f"{first}.{last}@{domain}",
        f"{first[0]}{last}@{domain}",
        f"{first[0]}.{last}@{domain}",
        f"{first}_{last}@{domain}",
    ]


def clean_domain(domain: str) -> str:
    """Extract the bare domain. Returns empty string if the domain is a job board
    or social network rather than the company's own site."""
    d = (
        (domain or "")
        .replace("https://", "")
        .replace("http://", "")
        .split("/")[0]
        .split("?")[0]
        .split("#")[0]
        .strip()
        .lower()
    )
    # scraped company URLs are sometimes junk strings
    if d in ("", "nan", "none", "null", "n/a"):
        return ""
    # Strip www./uk./es./etc subdomain
    if d.startswith("www."):
        d = d[4:]
    # Drop job boards, socials, and ATS/apply hosts — not the real company domain
    bad_hosts = (
        "linkedin.com", "indeed.com", "glassdoor.com", "google.com",
        "ziprecruiter.com", "monster.com", "wellfound.com", "ycombinator.com",
        "facebook.com", "twitter.com", "x.com",
        "grnh.se", "greenhouse.io", "lever.co", "ashbyhq.com",
        "workable.com", "myworkdayjobs.com", "smartrecruiters.com",
        "jobvite.com", "icims.com", "breezy.hr", "recruitee.com",
        "applytojob.com", "teamtailor.com", "bamboohr.com",
        "workatastartup.com",
    )
    if any(d.endswith(host) for host in bad_hosts):
        return ""
    # Sanity check: must contain a dot
    if "." not in d:
        return ""
    return d


def _company_domain(row: dict) -> str:
    """Pick the first scraped URL that resolves to a real company domain,
    preferring the direct site over an ATS/board apply link. '' → no perm emails."""
    for c in (row.get("company_url_direct"), row.get("company_url")):
        c = str(c or "")
        if c and clean_domain(c):
            return c
    return ""


# ── rate limiter ──────────────────────────────────────────────────────────────
class TokenBucket:
    """Simple token bucket. Used to keep Gemma calls under the 16k input-tokens/min
    paid tier limit. We budget conservatively (12k/min) since the actual count
    depends on prompt size + system prompt + schema overhead."""

    def __init__(self, tokens_per_minute: int = 12_000):
        self.capacity = tokens_per_minute
        self.tokens = float(tokens_per_minute)
        self.rate_per_sec = tokens_per_minute / 60.0
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def consume(self, tokens: int, on_wait=None) -> None:
        """Block until `tokens` are available, then deduct. If `tokens` exceeds
        capacity, the request is capped to capacity (otherwise we'd loop forever).
        If `on_wait` callback is provided, it's called between sleeps so the
        caller can refresh a heartbeat or update status."""
        if tokens > self.capacity:
            tokens = self.capacity
        while True:
            with self.lock:
                now = time.monotonic()
                elapsed = now - self.last
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate_per_sec)
                self.last = now
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return
                shortfall = tokens - self.tokens
                wait = shortfall / self.rate_per_sec
            time.sleep(min(wait, 5.0))
            if on_wait is not None:
                try:
                    on_wait()
                except Exception:
                    pass


# One stage active at a time → one shared bucket. 14k of the Gemini Tier 1
# 16k input TPM, leaving headroom for token-estimation error.
_SHARED_BUCKET = TokenBucket(tokens_per_minute=14_000)
_BUCKETS = {
    "stage1": _SHARED_BUCKET,
    "stage2": _SHARED_BUCKET,
    "stage3": _SHARED_BUCKET,
    "manual": _SHARED_BUCKET,
}


def _estimate_tokens(text: str) -> int:
    """Rough estimate: 1 token ≈ 3.5 characters for English text. Conservative side."""
    return max(1, len(text) // 3)


def _strip_json_fence(text: str) -> str:
    """Strip leading/trailing markdown code fences from LLM JSON output.
    Some models (especially Gemma) return ```json {...} ``` despite being
    told response_mime_type='application/json'."""
    if not text:
        return text
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
    if t.endswith("```"):
        t = t[:-3].rstrip()
    return t.strip()


def _run_with_timeout(fn, timeout_s: float):
    """Run `fn()` in a background thread; raise TimeoutError if it doesn't
    return within `timeout_s`. The thread is daemon, so if it never returns
    we leak it but it dies with the process. Necessary because google-genai
    SDK's own timeout doesn't actually work (it passes timeout=None to
    httpx). See googleapis/python-genai#911."""
    result = {"value": None, "exc": None}
    done = threading.Event()

    def _runner():
        try:
            result["value"] = fn()
        except Exception as e:
            result["exc"] = e
        finally:
            done.set()

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    if not done.wait(timeout=timeout_s):
        raise TimeoutError(f"Gemma call exceeded {timeout_s}s")
    if result["exc"] is not None:
        raise result["exc"]
    return result["value"]


class GemmaParseError(Exception):
    """Model returned unparseable JSON — usually degenerate repetition that the
    output-token cap truncated mid-structure. Distinct from transient transport
    errors so callers can fall back instead of retrying a deterministic loop."""


def _collapse_repetition(text: str) -> str:
    """Collapse degenerate repeated runs ("agentic agentic agentic…") to a single
    instance so a looping response stops eating the token buffer."""
    prev = None
    while prev != text:
        prev = text
        text = _REPEAT_RE.sub(r"\1", text)
    return text


def _close_truncated_json(text: str) -> str:
    """Best-effort repair of JSON the token cap cut off mid-stream: terminate a
    dangling string, drop a trailing comma, then balance open braces/brackets."""
    s = text.rstrip()
    stack, in_str, esc = [], False, False
    for ch in s:
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in "{[":
            stack.append(ch)
        elif ch == "}" and stack and stack[-1] == "{":
            stack.pop()
        elif ch == "]" and stack and stack[-1] == "[":
            stack.pop()
    if in_str:
        s += '"'
    s = s.rstrip().rstrip(",")
    for opener in reversed(stack):
        s += "}" if opener == "{" else "]"
    return s


def _parse_or_salvage(raw: str, schema):
    """Validate model JSON; on a truncated/degenerate response, collapse the
    repetition and close dangling structures, then retry. Whatever fields
    survived are kept; missing ones fall back to the schema defaults. Raises
    GemmaParseError only when nothing parseable remains."""
    text = _strip_json_fence(raw or "")
    try:
        return schema.model_validate_json(text)
    except Exception:
        pass
    repaired = _close_truncated_json(_collapse_repetition(text))
    try:
        return schema.model_validate_json(repaired)
    except Exception as e:
        raise GemmaParseError(str(e)) from e


# ── Gemma call wrapper ────────────────────────────────────────────────────────
def call_gemma(
    client, model: str, backend: str,
    system: str, prompt: str, schema,
    stage: str = "stage1", max_output_tokens: int = 1024,
):
    # Rate-limit only the paid Gemma API; LM Studio is local and free.
    if backend == "gemma":
        bucket = _BUCKETS.get(stage, _BUCKETS["stage1"])
        est_tokens = _estimate_tokens(system) + _estimate_tokens(prompt)

        def _heartbeat_while_waiting():
            try:
                runner_status.patch("brain1")
            except Exception:
                pass

        bucket.consume(est_tokens, on_wait=_heartbeat_while_waiting)

    max_attempts = 4
    per_call_timeout = 60.0  # hard external timeout per attempt

    for attempt in range(1, max_attempts + 1):
        try:
            if backend == "anthropic":
                def _call_anthropic():
                    # structured outputs: parse() validates against the schema
                    response = client.messages.parse(
                        model=model,
                        max_tokens=max_output_tokens,
                        system=system,
                        messages=[{"role": "user", "content": prompt}],
                        output_format=schema,
                        timeout=per_call_timeout,
                    )
                    if response.parsed_output is not None:
                        return response.parsed_output
                    raw = next((b.text for b in response.content
                                if b.type == "text"), "")
                    return _parse_or_salvage(raw, schema)
                return _run_with_timeout(_call_anthropic, per_call_timeout + 5)

            if backend in ("lmstudio", "openrouter"):
                def _call_lmstudio():
                    response = client.chat.completions.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": system},
                            {"role": "user", "content": prompt},
                        ],
                        temperature=0.1,
                        max_tokens=max_output_tokens,
                        response_format={
                            "type": "json_schema",
                            "json_schema": {
                                "name": schema.__name__,
                                "schema": schema.model_json_schema(),
                                "strict": True,
                            },
                        },
                        timeout=per_call_timeout,
                    )
                    raw = response.choices[0].message.content or ""
                    return _parse_or_salvage(raw, schema)
                return _run_with_timeout(_call_lmstudio, per_call_timeout + 5)

            def _call_gemma_api():
                config = types.GenerateContentConfig(
                    system_instruction=system,
                    temperature=0.1,
                    max_output_tokens=max_output_tokens,
                    response_mime_type="application/json",
                    response_schema=schema,
                )
                response = client.models.generate_content(
                    model=model, contents=prompt, config=config
                )
                return _parse_or_salvage(response.text or "", schema)
            return _run_with_timeout(_call_gemma_api, per_call_timeout)
        except Exception as e:
            msg = str(e)
            # 429 rate limit — respect retryDelay if present, else exponential backoff
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg or "rate limit" in msg.lower():
                wait_s = 0
                m = re.search(r"retry in ([\d.]+)s", msg)
                if m:
                    wait_s = float(m.group(1)) + 1
                else:
                    m = re.search(r"'retryDelay':\s*'([\d.]+)s'", msg)
                    if m:
                        wait_s = float(m.group(1)) + 1
                if wait_s == 0:
                    wait_s = min(60, 5 * (2 ** (attempt - 1)))
                if attempt < max_attempts:
                    log.warning(f"429 rate limit, sleeping {wait_s:.1f}s (attempt {attempt}/{max_attempts})")
                    time.sleep(wait_s)
                    continue
                raise
            # 500 INTERNAL — transient; quick retry
            if "500" in msg and "INTERNAL" in msg.upper():
                if attempt < max_attempts:
                    backoff = 2 * attempt
                    log.warning(f"500 INTERNAL, retrying in {backoff}s (attempt {attempt}/{max_attempts})")
                    time.sleep(backoff)
                    continue
                raise
            # Timeout — transient, retry
            if "timeout" in msg.lower() or "timed out" in msg.lower():
                if attempt < max_attempts:
                    backoff = 3 * attempt
                    log.warning(f"Timeout, retrying in {backoff}s (attempt {attempt}/{max_attempts})")
                    time.sleep(backoff)
                    continue
                raise
            # Any other error — fail fast
            raise


# ── Gemma #1 / #2 / #3 ────────────────────────────────────────────────────────
# The rewritable brief: what the evaluator is actually hunting for. Users may
# replace it wholesale — HJ is a listing analyzer, job filtering is just the
# default mission. Kept as a constant so "Restore default" has a source of truth.
DEFAULT_JUDGE_PROMPT = (
    "You are a strict job filter working for the candidate described in the "
    "CANDIDATE PROFILE. GOOD if the listing is a strong match for them, MAYBE "
    "if uncertain but possible, BAD if it clearly doesn't fit."
)

# Not user-editable — goal-agnostic frame + the fields the schema depends on.
_JUDGE_CONTRACT = (
    "You evaluate listings one at a time. Your evaluation goal and criteria "
    "are defined in the EVALUATION BRIEF below — follow it exactly.\n\n"
    "OUTPUT CONTRACT (always applies, regardless of the brief): verdict must "
    "be GOOD, MAYBE or BAD. For BAD, give a brief reject_reason (under 15 "
    "words); for GOOD/MAYBE leave it empty. Also report two facts read from "
    "the LISTING text alone: work_mode — remote/hybrid/onsite as the listing "
    "states it, unknown if it doesn't say; us_auth_required — yes if it "
    "demands US work authorization, citizenship, security clearance or W-2, "
    "no if explicitly open to anyone, unclear otherwise."
)


def gemma1_filter(client, model, backend, description: str, profile: str,
                  location: str = "", is_remote=None, source: str = "",
                  geo_eligibility: str = "", visa: str = "",
                  judge_prompt: str = "") -> JobFilter:
    system = (
        f"{_JUDGE_CONTRACT}\n\n"
        f"EVALUATION BRIEF:\n{(judge_prompt or '').strip() or DEFAULT_JUDGE_PROMPT}"
    )
    if (profile or "").strip():
        system += f"\n\nCANDIDATE PROFILE:\n{profile.strip()}"
    # Geo rule is a no-op unless the candidate has declared eligibility constraints.
    if (geo_eligibility or "").strip():
        system += (
            f"\n\nCANDIDATE GEO-ELIGIBILITY:\n{geo_eligibility.strip()}\n\n"
            "GEO RULE: Given the candidate's geo-eligibility above, if the role is "
            "region-locked, requires work authorization/sponsorship/relocation the "
            "candidate lacks, or is 'remote' only within a region the candidate cannot "
            "legally work in, return BAD with reject_reason prefixed 'geo: '. Treat "
            "'remote' as ambiguous — count it eligible only if remote-global or within "
            "a region the candidate can work. A visa/citizenship requirement (e.g. "
            "'US citizen/visa only', 'must be authorized to work in X') is a region-lock "
            "→ geo: BAD unless the candidate is authorized there. A region-qualified "
            "remote ('Remote (US)', 'Remote (CA)', 'Remote — EU only', etc.) is "
            "remote-within-that-region only, NOT remote-global → geo: BAD unless the "
            "candidate can work in that region."
        )
    remote_str = {True: "true", False: "false"}.get(is_remote, "unknown")
    meta = (f"Location: {location or 'unspecified'} | "
            f"Remote: {remote_str} | Source: {source or 'unspecified'}")
    if (visa or "").strip():
        meta += f" | Visa: {visa.strip()}"
    prompt = f"Job listing:\n{meta}\n\n{description[:6000]}"
    # hard token lock: a rewritten brief can't make stage 1 write essays
    return call_gemma(client, model, backend, system, prompt, JobFilter,
                      stage="stage1", max_output_tokens=256)


# A substring of 2-40 chars repeated 5+ times = a degenerate model loop
# ("truth-truth-truth…"); cut it before it reaches the UI.
_REPEAT_RE = re.compile(r"(.{2,40}?)\1{4,}", re.DOTALL)


def _sanitize_summary(text: str, max_chars: int = 600) -> str:
    """Defensive guard against runaway company_summary output: strip degenerate
    repetition, then hard-cap length. Model-agnostic — protects the UI from any
    backend that loops."""
    s = (text or "").strip()
    if not s:
        return s
    m = _REPEAT_RE.search(s)
    if m:
        s = (s[:m.start()] + m.group(1)).strip()
    if len(s) > max_chars:
        s = s[:max_chars].rstrip() + "…"
    return s


# Agency-specific signals only. Bare "data labeling"/"annotation" are deliberately
# NOT here: those describe a legit product (e.g. Trace Labs) as often as an agency.
_AGENCY_MARKERS = (
    "staffing_agency", "staffing agency", "staffing firm",
    "recruiting agency", "recruitment agency", "recruiting partner",
    "staff augmentation", "we place candidates", "place candidates at",
    "on behalf of our client", "on behalf of clients", "on behalf of our clients",
    "body shop", "gig platform", "talent scaling", "network of talent",
    "leverage a network", "managed it capabilities", "it consulting",
)


def _known_agency(conn, company: str, domain: str, dismissed: set[str]) -> str:
    """Company already researched and flagged as a staffing agency? Returns the
    name to cite, or "". Dismissed suspects are never blocked — a wrong flag
    must stay one dismissal away from undone, not silently eat every listing."""
    if not (company or "").strip():
        return ""
    if company.strip().lower() in dismissed:
        return ""
    try:
        import core.companies as _companies
        key = _companies.company_key(company, clean_domain(domain or ""))
        row = conn.execute(
            "SELECT culture_flags, company_summary FROM companies WHERE company_key = ?",
            (key,)).fetchone()
    except Exception:
        return ""
    if not row:
        return ""
    if _is_staffing_agency(row["culture_flags"], row["company_summary"] or ""):
        return company
    return ""


_EMPTY_RESEARCH_RE = re.compile(
    r"no (company )?(content|information|data)|unknown|unavailable|not available",
    re.I)


def _research_is_empty(summary: str) -> bool:
    """A summary that says it found nothing cost a call and taught nothing.
    Counting these separately keeps 'researched=12' from implying 12 useful
    answers when several are 'No information available for X'."""
    text = (summary or "").strip()
    # only the opening: a real summary may legitimately admit a gap later
    # ("...founded in 2019. Funding is unknown.")
    return len(text) < 40 or bool(_EMPTY_RESEARCH_RE.search(text[:60]))


def _is_staffing_agency(culture_flags, summary: str) -> bool:
    """True only on agency-specific signals (places people at other companies),
    not on bare product-labeling terms."""
    flags = " ".join((f or "").lower() for f in (culture_flags or []))
    blob = f"{flags} {(summary or '').lower()}"
    return any(m in blob for m in _AGENCY_MARKERS)


# Company research + contact discovery live in pipeline/enrich.py (one cached
# enrichment pass per company); the OSINT helpers above are its building blocks.


# ── DB write helpers ──────────────────────────────────────────────────────────
# Also used to trim DB rows back into insertable dicts (py3.12 sqlite3
# complains about unused named params).
JOB_INSERT_COLS = (
    "id", "title", "company", "domain", "location", "job_type",
    "salary_min", "salary_max", "currency", "source", "url",
    "description", "date_posted", "date_scraped", "description_hash",
    "date_posted_estimated", "yc_slug", "company_url",
)


def scraped_row_to_job(row, job_id: str = "", desc: str = "",
                       dhash: str = "") -> dict | None:
    """Scraped row (LinkedIn/YC/HN) → insertable job dict. None = unusable
    (no/short description). Also the pool-donor entry point (scrape_pool.py)."""
    desc = desc or str(row.get("description") or "")
    if not desc or len(desc) < 100:
        return None
    return {
        "id": job_id or str(row.get("id") or fallback_job_id(row)),
        "title": str(row.get("title") or ""),
        "company": str(row.get("company") or ""),
        "domain": _company_domain(row),
        # keeps the LinkedIn slug alive: a listing has no real domain, the
        # company page does
        "company_url": str(row.get("company_url") or ""),
        "location": str(row.get("location") or ""),
        "job_type": str(row.get("job_type") or ""),
        "salary_min": row.get("min_amount"),
        "salary_max": row.get("max_amount"),
        "currency": str(row.get("currency") or ""),
        "source": str(row.get("site") or ""),
        "url": str(row.get("job_url") or ""),
        "description": desc,
        "date_posted": str(row.get("date_posted") or ""),
        "date_scraped": datetime.now(timezone.utc).isoformat(),
        "description_hash": dhash or description_hash(desc),
        "date_posted_estimated": int(row.get("date_posted_estimated") or 0),
        "yc_slug": str(row.get("yc_slug") or ""),
    }


def insert_job_with_verdict(conn, job: dict, verdict: str, reject_reason: str,
                            judged: bool = True, work_mode: str = "unknown",
                            us_auth_required: str = "unclear") -> None:
    """judged=False = QUEUED path: stored in full but gemma1_done stays 0."""
    # contract says <15 words; the brief is user-writable, so enforce it here
    if reject_reason and len(reject_reason) > 160:
        reject_reason = reject_reason[:160].rstrip() + "…"
    params = {c: job.get(c) for c in JOB_INSERT_COLS}
    params["date_posted_estimated"] = int(params.get("date_posted_estimated") or 0)
    params["yc_slug"] = params.get("yc_slug") or ""
    params["company_url"] = params.get("company_url") or ""
    conn.execute(
        """
        INSERT OR REPLACE INTO jobs (
            id, title, company, domain, location, job_type,
            salary_min, salary_max, currency, source, url,
            description, date_posted, date_scraped, description_hash,
            date_posted_estimated, yc_slug, company_url,
            verdict, reject_reason, gemma1_done,
            work_mode, us_auth_required,
            company_summary, hiring_signal, real_stack, culture_flags, company_size,
            gemma2_done, gemma3_done,
            applied, applied_date
        ) VALUES (
            :id, :title, :company, :domain, :location, :job_type,
            :salary_min, :salary_max, :currency, :source, :url,
            :description, :date_posted, :date_scraped, :description_hash,
            :date_posted_estimated, :yc_slug, :company_url,
            :verdict, :reject_reason, :gemma1_done,
            :work_mode, :us_auth_required,
            NULL, 'uncertain', '[]', '[]', 'tiny',
            0, 0,
            0, NULL
        )
        """,
        {**params, "verdict": verdict, "reject_reason": reject_reason,
         "gemma1_done": 1 if judged else 0,
         "work_mode": work_mode, "us_auth_required": us_auth_required},
    )
    conn.commit()


def update_job_research(conn, job_id: str, r: CompanyResearch,
                        sources: list[dict] | None = None) -> None:
    conn.execute(
        """
        UPDATE jobs SET
            company_summary = ?,
            hiring_signal   = ?,
            real_stack      = ?,
            culture_flags   = ?,
            company_size    = ?,
            intel_sources   = ?,
            gemma2_done     = 1
        WHERE id = ?
        """,
        (
            r.company_summary,
            r.hiring_signal,
            json.dumps(r.real_stack),
            json.dumps(r.culture_flags),
            r.company_size,
            json.dumps(sources or []),
            job_id,
        ),
    )
    conn.commit()


def update_job_outreach(conn, job_id: str, contacts: list[dict]) -> None:
    conn.execute(
        """
        UPDATE jobs SET
            contacts    = ?,
            gemma3_done = 1
        WHERE id = ?
        """,
        (json.dumps(contacts), job_id),
    )
    conn.commit()


def should_process(conn, job_id: str, new_hash: str) -> tuple[bool, bool]:
    """Returns (should_process, is_new). Known ids never re-enter Stage 1 —
    listing edits just refresh the stored hash, the verdict stands."""
    row = conn.execute(
        "SELECT description_hash FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if not row:
        return True, True
    if row["description_hash"] != new_hash:
        conn.execute(
            "UPDATE jobs SET description_hash=? WHERE id=?", (new_hash, job_id)
        )
        conn.commit()
    return False, False


def load_job(conn, job_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def fallback_job_id(row) -> str:
    """Build a stable, collision-free id for scraped rows that lack a native id
    (YC listings — LinkedIn rows already carry a numeric id).

    No date_posted in the id: WaaS dates are scrape-time estimates that shift
    daily, which would mint a fresh id (= fresh LLM call) every scan. The url
    hash alone is unique and stable; date is a last resort for URL-less rows."""
    base = f"{row.get('company')}_{row.get('title')}"
    url = str(row.get("job_url") or "")
    if url:
        suffix = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
        return f"{base}_{suffix}"
    return f"{base}_{row.get('date_posted')}"


# a seed-stage startup does not have 2,500 open roles — beyond this the ATS
# slug almost certainly resolved to some unrelated giant (see: YC's "Pulse"
# vs the UK healthcare staffing agency squatting greenhouse.io/pulse)
YC_MAX_JOBS_PER_COMPANY = 150


# ── LinkedIn term scrape ──────────────────────────────────────────────────────
def safe_scrape(term: str, sources: list[str], results_wanted: int,
                hours_old: int) -> list[dict]:
    """LinkedIn listings for one term. Returns rows, not a DataFrame.

    Was JobSpy; it advanced its offset by the cumulative result count (skipping
    whole pages), stopped on the first empty page, ordered by relevance rather
    than date, and crashed on unrecognised country strings. On the same term and
    window our scraper returned 976 listings to its 119."""
    if "linkedin" not in sources:
        return []
    stats: dict = {}
    rows = li.scrape_linkedin_jobs(hours=hours_old, keywords=term,
                                   limit=results_wanted or None,
                                   stats=stats)
    if not stats.get("complete"):
        log.warning(f"[stage1] '{term}': incomplete — {stats.get('reason')}")
    return rows


def yc_jobs_to_rows(yc_jobs: list[dict]) -> list[dict]:
    """Convert ycombinator_jobs_scraper output into JobSpy-style row dicts so YC
    listings flow through the exact same Stage 1 path as LinkedIn. YC has
    no salary or numeric id; we leave id=None so the downstream fallback builds a
    stable one from company/title/date."""
    per_company: dict[str, int] = {}
    rows = []
    for j in yc_jobs:
        comp = (j.get("company") or "").strip().lower()
        per_company[comp] = per_company.get(comp, 0) + 1
        if per_company[comp] > YC_MAX_JOBS_PER_COMPANY:
            if per_company[comp] == YC_MAX_JOBS_PER_COMPANY + 1:
                print(f"[yc] '{j.get('company')}' exceeds "
                      f"{YC_MAX_JOBS_PER_COMPANY} listings — likely a wrong "
                      f"ATS board, skipping the overflow")
            continue
        rows.append({
            "id": None,
            "title": j.get("title") or "",
            "company": j.get("company") or "",
            "company_url_direct": j.get("company_website") or "",
            "location": j.get("location") or "",
            "job_type": j.get("job_type") or "",
            "min_amount": None,
            "max_amount": None,
            "currency": "",
            "site": "yc",
            "job_url": j.get("job_url") or "",
            "description": j.get("description") or "",
            "date_posted": str(j.get("date_posted") or ""),
            # WaaS dates are estimates from rounded relative ages; flag them
            # so filtering/UI never treat them as real.
            "date_posted_estimated": 1 if j.get("ats") == "waas" else 0,
            # YC slug unlocks the profile-page founders fetch at enrichment
            "yc_slug": j.get("company_yc_slug") or "",
            # Keep YC's tri-state remote flag (True/False/None=unknown) for the pre-Stage-1 filter.
            "is_remote": j.get("is_remote"),
            # WaaS-only structured visa requirement; feeds the Stage 1 geo check.
            "visa": j.get("visa"),
        })
    return rows


def apply_yc_remote_filter(rows: list[dict], remote_only: bool) -> list[dict]:
    """Drop YC rows explicitly marked non-remote. Unknown (is_remote None/missing)
    is kept — we don't want to lose genuinely-remote jobs that just didn't set the
    flag. No-op when remote_only is False."""
    if not remote_only:
        return rows
    return [r for r in rows if r.get("is_remote") is not False]


def _parse_yc_date(s: str):
    """Parse a row's date_posted to an aware UTC datetime. Day-granular strings
    ('YYYY-MM-DD', from ATS boards) become midnight UTC; full ISO timestamps
    (HN comments) keep their exact time. Returns (dt, day_granular) —
    (None, True) when empty or unparseable."""
    s = (s or "").strip()
    if not s:
        return None, True
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt, len(s) <= 10
    except ValueError:
        try:
            return (datetime.strptime(s[:10], "%Y-%m-%d")
                    .replace(tzinfo=timezone.utc)), True
        except ValueError:
            return None, True


def apply_yc_date_filter(rows: list[dict], hours_old: int,
                         now: datetime | None = None,
                         return_stats: bool = False):
    """Freshness window over REAL posting dates (JobSpy does this server-side;
    YC/HN have no param for it). Estimated dates (WaaS) pass through — they're
    fabrications, the ledger + cap govern them instead. Day-granular dates
    compare by date (inclusive), full timestamps (HN) compare exactly.
    Undated rows are dropped: can't confirm fresh = the stale-leak bug.
    hours_old<=0 disables. return_stats adds (stale, undated) drop counts."""
    if not hours_old or hours_old <= 0:
        return (rows, 0, 0) if return_stats else rows
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours_old)
    kept = []
    stale = undated = 0  # diagnostic split of the dropped rows; does not affect kept
    for r in rows:
        if r.get("date_posted_estimated"):
            kept.append(r)
            continue
        dt, day_granular = _parse_yc_date(str(r.get("date_posted") or ""))
        if dt is None:
            undated += 1
        elif (dt.date() >= cutoff.date()) if day_granular else (dt >= cutoff):
            kept.append(r)
        else:
            stale += 1
    return (kept, stale, undated) if return_stats else kept


def safe_scrape_yc(cfg: dict):
    """Scrape small early-stage YC startups once (company-based, not per-term).
    Any failure is non-fatal and returns [] — a YC error must never kill the
    LinkedIn scrape."""
    try:
        from ycombinator_jobs_scraper import scrape_yc_jobs
    except Exception as e:
        log.error(f"YC scraper unavailable (skipping): {e}")
        return []
    try:
        # keyword=None on purpose: YC's keyword is a crude single-substring title
        # filter; Stage 1's LLM does the real filtering on the description instead.
        jobs = scrape_yc_jobs(
            # 0 = uncapped
            max_companies=int(cfg.get("yc_max_companies", 100)) or None,
            max_team_size=int(cfg.get("yc_max_team_size", 50)) or None,
            waas_descriptions=True,
            years_back=int(cfg.get("yc_years_back", 3)),
            keyword=None,
        )
        return yc_jobs_to_rows(jobs)
    except Exception as e:
        log.error(f"YC scrape failed (skipping): {e}")
        return []


# ── Source selection helpers ──────────────────────────────────────────────────
def linkedin_enabled(sources: list[str]) -> bool:
    """True when LinkedIn is selected. An empty list is legitimate (YC/HN-only
    run) and means skip the term loop entirely."""
    return "linkedin" in (sources or [])


def has_scrape_source(sources: list[str], use_yc: bool, use_hn: bool = False) -> bool:
    """True when there is anything to scrape at all — LinkedIn, YC, or HN.
    When all are off there is genuinely nothing to do (vs. silently forcing LinkedIn)."""
    return bool(sources) or bool(use_yc) or bool(use_hn)


# ── Main entry: sequential Stage 1 → Stage 2 → Stage 3 ────────────────────────
def run_brain1() -> None:
    cfg = load_config()
    keys = load_keys()
    profile_text = cfg.get("profile", "")
    geo_text = cfg.get("geo_eligibility", "")

    search_terms = [
        t.strip() for t in cfg.get("search_terms", "").splitlines() if t.strip()
    ] or ["machine learning engineer remote"]
    hard_rejects = [
        t.strip() for t in cfg.get("hard_rejects", "").splitlines() if t.strip()
    ]
    # companies the user has said are NOT agencies — never auto-blocked
    dismissed = {c.strip().lower() for c in cfg.get("dismissed_suspects", [])
                 if isinstance(c, str) and c.strip()}
    sources = cfg.get("sources", ["linkedin"])
    use_yc = bool(cfg.get("use_yc"))
    use_hn = bool(cfg.get("use_hn"))
    results_wanted = int(cfg.get("results_wanted", 100))
    hours_old = int(cfg.get("hours_old", 72))
    yc_hours_old = int(cfg.get("yc_hours_old", 720))
    max_llm_jobs = int(cfg.get("max_llm_jobs_per_scan", 100))
    ledger_expire_days = int(cfg.get("ledger_expire_days", 60))
    use_rag = bool(cfg.get("use_rag", True))
    github_pat = keys.get("github", "")

    # Two separate clients allow Stage 1 (filter, high volume) and Stage 2/3
    # (research+outreach, needs intelligence) to use different backends.
    s1_client, s1_model, s1_backend = get_gemma_client_for_stage(cfg, keys, "stage1")
    s2_client, s2_model, s2_backend = get_gemma_client_for_stage(cfg, keys, "stage2")
    s3_client, s3_model, s3_backend = get_gemma_client_for_stage(cfg, keys, "stage3")

    log.info("=" * 60)
    log.info(f"Brain 1 started")
    log.info(f"Stage 1 (filter):   backend={s1_backend} model={s1_model}")
    log.info(f"Stage 2 (research): backend={s2_backend} model={s2_model}")
    log.info(f"Stage 3 (outreach): backend={s3_backend} model={s3_model}")
    log.info(f"Terms: {len(search_terms)} | Rejects: {len(hard_rejects)} | Sources: {sources} | YC: {use_yc} | HN: {use_hn}")
    log.info("=" * 60)

    # All sources off + empty queue = nothing to do. With QUEUED backlog it's a
    # legit drain-only run: judge leftovers without re-scraping anything.
    if not has_scrape_source(sources, use_yc, use_hn):
        init_db()
        _c = get_db_connection()
        try:
            pending = _c.execute(
                "SELECT COUNT(*) FROM jobs WHERE verdict='QUEUED'"
            ).fetchone()[0]
        finally:
            _c.close()
        if not pending:
            log.warning(
                "No scrape sources enabled and no queued jobs. Nothing to do — "
                "enable LinkedIn, Y Combinator, or Hacker News in Setup."
            )
            runner_status.start("brain1")
            runner_status.finish("brain1", error="no sources enabled")
            return
        log.info(f"[stage1] no scrape sources; drain-only run ({pending} queued)")

    init_db()
    runner_status.start("brain1")
    runner_status.patch(
        "brain1",
        pid=os.getpid(),
        stage1="initializing", stage2="idle", stage3="idle",
        scraped=0, good=0, maybe=0, bad=0, hard_rej=0,
    )

    # Watchdog: hard-kills us if the dashboard dies while we're stuck inside
    # synchronous scrape code that can't reach the cooperative heartbeat checks.
    def _watchdog():
        while True:
            time.sleep(15)
            if not runner_status.dashboard_is_alive(max_age_seconds=90):
                log.warning("Watchdog: dashboard heartbeat dead for >90s; hard-killing brain1.")
                for h in logging.getLogger().handlers:
                    try:
                        h.flush()
                    except Exception:
                        pass
                try:
                    runner_status.finish("brain1", error="killed by watchdog (dashboard gone)")
                except Exception:
                    pass
                os._exit(1)

    threading.Thread(target=_watchdog, daemon=True, name="brain1-watchdog").start()

    counts = {"scraped": 0, "good": 0, "maybe": 0, "bad": 0, "hard_rej": 0,
              "no_desc": 0, "judged": 0, "queued": 0,
              # enrichment is the expensive half and had nothing to show for it
              "researched": 0, "cached": 0, "contacts": 0,
              "research_failed": 0, "agencies": 0}

    good_jobs: list[dict] = []
    # Cross-source dedup by job_url (LinkedIn/YC/HN can overlap within a run).
    seen_urls: set[str] = set()

    conn = get_db_connection()
    meter = ScanMeter(conn, cap=max_llm_jobs)
    last_heartbeat = time.monotonic()
    aborted = False
    scan_error: str | None = None

    def _judge_job(job: dict, is_remote=None, visa: str = "",
                   progress_label: str = "") -> None:
        """Stage 1 verdict + persist. Caller checks the cap; a failed call
        still counts (the attempt was spent)."""
        runner_status.patch(
            "brain1",
            stage1=f"filter {counts['scraped']} {progress_label}",
        )
        try:
            g1 = gemma1_filter(s1_client, s1_model, s1_backend,
                               job["description"], profile_text,
                               location=job["location"], is_remote=is_remote,
                               source=job["source"], geo_eligibility=geo_text,
                               visa=visa,
                               judge_prompt=cfg.get("judge_prompt", ""))
            insert_job_with_verdict(conn, job, g1.verdict, g1.reject_reason,
                                    work_mode=g1.work_mode,
                                    us_auth_required=g1.us_auth_required)
            log.info(f"[stage1] {g1.verdict:5s} {job['title']} @ {job['company']}")
            # Best-effort embed-on-scrape for RAG; a failed embed must never fail the scrape.
            if use_rag:
                embeddings.embed_and_store(conn, job)
            if g1.verdict == "GOOD":
                counts["good"] += 1
                job["verdict"] = "GOOD"  # stage 2 reads this
                good_jobs.append(job)
            elif g1.verdict == "MAYBE":
                counts["maybe"] += 1
            else:
                counts["bad"] += 1
        except Exception as e:
            log.error(f"[stage1] Gemma1 failed for {job['id']}: {e}")
            insert_job_with_verdict(conn, job, "BAD", f"gemma1_error: {e}")
            counts["bad"] += 1
            time.sleep(1)
        counts["judged"] += 1
        ledger.mark_judged(conn, job["id"])
        meter.count("judged")
        runner_status.patch("brain1", **counts)

    def _process_row(row, progress_label: str) -> bool:
        """The Stage 1 choke point: ledger → dedup → hard-reject (free) →
        cap gate (overflow QUEUED) → LLM judge. Returns False only when the
        dashboard heartbeat died and the scrape should abort."""
        nonlocal last_heartbeat
        if not runner_status.dashboard_is_alive(max_age_seconds=90):
            log.warning("Dashboard heartbeat lost (>90s). Self-terminating.")
            return False
        if time.monotonic() - last_heartbeat > 20:
            runner_status.patch("brain1")
            last_heartbeat = time.monotonic()
        job_id = str(row.get("id") or fallback_job_id(row))
        desc = str(row.get("description") or "")
        if not desc or len(desc) < 100:
            counts["no_desc"] += 1
            log.info(f"[stage1] skip (no/short description) "
                     f"{row.get('title')} @ {row.get('company')}")
            return True
        url = str(row.get("job_url") or "")
        if url and url in seen_urls:
            return True
        if url:
            seen_urls.add(url)
        # every sighting lands in the ledger, judged or not
        ledger.upsert_seen(conn, job_id, str(row.get("site") or ""))
        dhash = description_hash(desc)
        process, is_new = should_process(conn, job_id, dhash)
        if not process:
            return True

        job = scraped_row_to_job(row, job_id=job_id, desc=desc, dhash=dhash)

        counts["scraped"] += 1
        meter.count("scraped")

        # ── hard reject (free: no LLM call, never counts against the cap) ──
        # Include company name: many staffing firms have giveaway names but normal job text.
        reject_text = f"{job['title']} {job['company']} {desc}"
        reject_kw = hard_reject_check(reject_text, hard_rejects)
        if reject_kw:
            insert_job_with_verdict(conn, job, "BAD", f"hard_reject: {reject_kw}")
            counts["hard_rej"] += 1
            meter.count("hard_rejected")
            runner_status.patch("brain1", **counts)
            return True

        # ── known agency (free): we already paid to learn this once ──
        # Staffing detection used to run AFTER enrichment, so every listing from
        # a known agency cost a judge call and a research call before being
        # demoted. Now the first one pays and the rest are free.
        agency = _known_agency(conn, job["company"], job["domain"], dismissed)
        if agency:
            insert_job_with_verdict(conn, job, "BAD", f"known staffing agency: {agency}")
            counts["hard_rej"] += 1
            meter.count("hard_rejected")
            runner_status.patch("brain1", **counts)
            return True

        # ── cap gate: over budget → persist as QUEUED, judge next scan ──
        if not meter.can_judge():
            insert_job_with_verdict(conn, job, "QUEUED", "", judged=False)
            counts["queued"] += 1
            meter.count("queued")
            runner_status.patch("brain1", **counts)
            return True

        _judge_job(job, is_remote=row.get("is_remote"),
                   visa=str(row.get("visa") or ""), progress_label=progress_label)
        return True

    # queues are per-source: an HN-only scan drains HN, never touches the YC
    # backlog. Drain-only runs (no sources enabled) still drain everything.
    enabled_srcs = list(sources)
    if use_yc:
        enabled_srcs.append("yc")
    if use_hn:
        enabled_srcs.append("hn")

    def _queued_count(src: str) -> int:
        return conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE verdict='QUEUED' AND source=?",
            (src,)).fetchone()[0]

    def _scrape_allowed(src: str, label: str) -> bool:
        """No re-scraping a source that still owes queued verdicts."""
        pending = _queued_count(src)
        if pending:
            log.info(f"[stage1] skipping {label} scrape — {pending} queued "
                     f"jobs still owed; draining those first")
            return False
        return True

    try:
        # drain QUEUED overflow from previous scans first, FIFO, same cap
        # skip listings the ledger already buried — the server has always done
        # this, the local app was marking them expired and judging them anyway
        if enabled_srcs:
            ph = ",".join("?" for _ in enabled_srcs)
            queued_rows = conn.execute(
                f"SELECT j.* FROM jobs j LEFT JOIN seen_jobs s ON s.job_key = j.id "
                f"WHERE j.verdict='QUEUED' AND j.source IN ({ph}) "
                f"AND s.expired_at IS NULL ORDER BY j.date_scraped ASC",
                enabled_srcs).fetchall()
        else:
            queued_rows = conn.execute(
                "SELECT j.* FROM jobs j LEFT JOIN seen_jobs s ON s.job_key = j.id "
                "WHERE j.verdict='QUEUED' AND s.expired_at IS NULL "
                "ORDER BY j.date_scraped ASC"
            ).fetchall()
        if queued_rows:
            log.info(f"[stage1] draining {len(queued_rows)} queued jobs from previous scans")
            runner_status.patch(
                "brain1", stage1=f"judging {len(queued_rows)} queued from last scan"
            )
            for qrow in queued_rows:
                if not runner_status.dashboard_is_alive(max_age_seconds=90):
                    log.warning("Dashboard heartbeat lost (>90s). Self-terminating.")
                    aborted = True
                    break
                if not meter.can_judge():
                    break  # rest stays QUEUED for the next scan
                job = {c: qrow[c] for c in JOB_INSERT_COLS}
                _judge_job(job, progress_label="(queued)")

        # YC-only run (no JobSpy sites) skips this loop — avoids an empty JobSpy call.
        scrape_terms = (list(enumerate(search_terms, 1))
                        if not aborted and linkedin_enabled(sources)
                        and all(_scrape_allowed(s, s) for s in sources) else [])
        if not scrape_terms:
            log.info("[stage1] LinkedIn not selected; skipping term scrape.")
        for term_idx, term in scrape_terms:
            if not runner_status.dashboard_is_alive(max_age_seconds=90):
                log.warning("Dashboard heartbeat lost (>90s). Self-terminating.")
                aborted = True
                break
            runner_status.patch(
                "brain1",
                stage1=f"scraping '{term}' ({term_idx}/{len(search_terms)})",
            )
            log.info(f"[stage1] Scraping '{term}'")
            rows = safe_scrape(term, sources, results_wanted, hours_old)
            if not rows:
                continue

            progress = f"({term_idx}/{len(search_terms)})"
            for row in rows:
                if not _process_row(row, progress):
                    aborted = True
                    break

            if aborted:
                break

        # ── YC startups (company-based, scraped once — not per term) ──────────
        if not aborted and cfg.get("use_yc") and _scrape_allowed("yc", "YC"):
            runner_status.patch("brain1", stage1="scraping Y Combinator startups")
            log.info("[stage1] Scraping Y Combinator startups")
            yc_rows = safe_scrape_yc(cfg)
            log.info(f"[stage1] YC returned {len(yc_rows)} listings")
            # Drop non-remote YC jobs before Stage 1 so they never hit Gemma.
            if cfg.get("yc_remote_only", True):
                before = len(yc_rows)
                yc_rows = apply_yc_remote_filter(yc_rows, True)
                log.info(
                    f"[stage1] YC remote filter: dropped {before - len(yc_rows)} "
                    f"non-remote, kept {len(yc_rows)}"
                )
            # YC gets its own wider window: WaaS listings stay up for months,
            # so the global hours_old (tuned for fast sources) would drop nearly all.
            yc_rows, _stale, _undated = apply_yc_date_filter(
                yc_rows, yc_hours_old, return_stats=True)
            log.info(
                f"[stage1] YC date filter (<= {yc_hours_old}h): kept "
                f"{len(yc_rows)}, dropped {_stale} stale, {_undated} undated"
            )
            for row in yc_rows:
                if not _process_row(row, "(YC)"):
                    aborted = True
                    break

        # ── Hacker News "Who is hiring?" (single thread, scraped once) ────────
        if not aborted and cfg.get("use_hn") and _scrape_allowed("hn", "HN"):
            runner_status.patch("brain1", stage1="scraping Hacker News 'Who is hiring?'")
            log.info("[stage1] Scraping Hacker News 'Who is hiring?'")
            hn_rows = hn.scrape_hn_jobs(cfg)
            log.info(f"[stage1] HN returned {len(hn_rows)} listings")
            # Same pre-Stage-1 filters as YC (the filters are source-agnostic).
            if cfg.get("hn_remote_only", True):
                before = len(hn_rows)
                hn_rows = apply_yc_remote_filter(hn_rows, True)
                log.info(
                    f"[stage1] HN remote filter: dropped {before - len(hn_rows)} "
                    f"non-remote, kept {len(hn_rows)}"
                )
            before = len(hn_rows)
            hn_rows = apply_yc_date_filter(hn_rows, hours_old)
            log.info(
                f"[stage1] HN date filter (<= {hours_old}h): dropped "
                f"{before - len(hn_rows)} stale/undated, kept {len(hn_rows)}"
            )
            for row in hn_rows:
                if not _process_row(row, "(HN)"):
                    aborted = True
                    break

        # ── Stage 1 done; start Stage 2 on collected GOODs ────────────────────
        if aborted:
            runner_status.patch("brain1", stage1="aborted (dashboard closed)")
            log.info("[stage1] aborted")
        else:
            runner_status.patch(
                "brain1",
                stage1=f"done ({counts['good']} GOOD to enrich)",
            )
            log.info(
                f"[stage1] done. {counts['good']} GOOD, {counts['maybe']} MAYBE, "
                f"{counts['bad']} BAD, {counts['hard_rej']} hard-rejected"
            )

        # ── enrichment: research + contacts per company, one cached pass ─────
        if not aborted and good_jobs:
            from pipeline import enrich
            log.info(f"[enrich] starting on {len(good_jobs)} GOOD jobs")
            for i, job in enumerate(good_jobs, 1):
                if not runner_status.dashboard_is_alive(max_age_seconds=90):
                    log.warning("[enrich] dashboard gone, stopping")
                    aborted = True
                    break
                runner_status.patch(
                    "brain1",
                    stage2=f"enriching {job['company']} ({i}/{len(good_jobs)})",
                    stage3="merged into research",
                )
                try:
                    # email baked into the posting = the contact hunt is free
                    baked = enrich.posting_emails(job.get("description"))
                    e = enrich.enrich_company(
                        conn, cfg, job["company"], job["domain"],
                        client=s2_client, model=s2_model, backend=s2_backend,
                        yc_slug=job.get("yc_slug") or "",
                        skip_hunt=bool(baked), meter=meter,
                        company_url=job.get("company_url") or "",
                    )
                    research = CompanyResearch(
                        company_summary=e["company_summary"],
                        hiring_signal=e["hiring_signal"],
                        real_stack=e["real_stack"],
                        culture_flags=e["culture_flags"],
                        company_size=e["company_size"],
                    )
                    update_job_research(conn, job["id"], research,
                                        sources=e.get("sources"))
                    job["company_summary"] = research.company_summary
                    contacts = _merge_contacts([baked, e["contacts"]])
                    update_job_outreach(conn, job["id"], contacts)
                    counts["cached" if e.get("from_cache") else "researched"] += 1
                    counts["contacts"] += len(contacts)
                    if _research_is_empty(research.company_summary):
                        counts["research_failed"] += 1

                    # Demote only on agency signals, not bare product-labeling.
                    if _is_staffing_agency(research.culture_flags, research.company_summary):
                        counts["agencies"] += 1
                        conn.execute(
                            "UPDATE jobs SET verdict=?, reject_reason=? WHERE id=?",
                            (
                                "BAD",
                                "stage2_demoted_from_GOOD: staffing/recruiting agency",
                                job["id"],
                            ),
                        )
                        conn.commit()
                        log.info(
                            f"[enrich] [{i}/{len(good_jobs)}] {job['company']} "
                            f"-> DEMOTED (staffing/recruiting)"
                        )
                    else:
                        log.info(
                            f"[enrich] [{i}/{len(good_jobs)}] {job['company']} "
                            f"-> {research.hiring_signal} ({research.company_size}), "
                            f"{len(contacts)} contact(s)"
                            f"{' [cache]' if e.get('from_cache') else ''}"
                            f"{' [posting email]' if baked else ''}"
                        )
                except Exception as e:
                    counts["research_failed"] += 1
                    log.error(
                        f"[enrich] [{i}/{len(good_jobs)}] failed for {job['company']}: {e}"
                    )
            runner_status.patch("brain1", stage2="idle", stage3="idle")

        # ledger housekeeping: flag listings gone from their boards
        if not aborted:
            expired = ledger.prune_expired(conn, ledger_expire_days)
            if expired:
                log.info(f"[ledger] marked {expired} unseen-for-{ledger_expire_days}d "
                         f"listings as expired")

    except Exception as e:
        scan_error = str(e)
        raise
    finally:
        try:
            meter.finish(error=scan_error or
                         ("aborted: dashboard closed" if aborted else None))
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

    runner_status.patch("brain1", **counts)
    if aborted:
        runner_status.finish("brain1", error="aborted: dashboard closed")
        log.info("Brain 1 aborted by missing dashboard heartbeat.")
    else:
        runner_status.finish("brain1")
        cap_note = f" (cap {meter.cap})" if meter.cap > 0 else ""
        log.info("=" * 60)
        log.info(
            f"Brain 1 complete | scraped={counts['scraped']} "
            f"hard_rejected={counts['hard_rej']} "
            f"judged={counts['judged']}{cap_note} queued={counts['queued']} | "
            f"good={counts['good']} maybe={counts['maybe']} "
            f"bad={counts['bad']} no_desc={counts['no_desc']} | "
            # researched/cached count COMPANIES on GOOD jobs, not listings
            f"researched={counts['researched']} cached={counts['cached']} "
            f"failed2research={counts['research_failed']} "
            f"contacts={counts['contacts']} agencies={counts['agencies']}"
        )
        log.info("=" * 60)


# ── Single-job public entry points (for dashboard MAYBE buttons) ──────────────
def _manual_enrich(job_id: str, stage_label: str):
    """Shared body for the manual Research / Find Contact buttons: force a full
    enrichment (research + hunt, cache refreshed) and persist onto the job.
    Returns (job, enrichment) or (None, None)."""
    from pipeline import enrich
    cfg = load_config()
    keys = load_keys()
    client, model, backend = get_gemma_client(cfg, keys, "stage2")
    conn = get_db_connection()
    try:
        job = load_job(conn, job_id)
        if not job:
            return None, None
        try:
            baked = enrich.posting_emails(job.get("description"))
            e = enrich.enrich_company(
                conn, cfg, job["company"], job["domain"],
                client=client, model=model, backend=backend,
                yc_slug=job.get("yc_slug") or "", force=True,
            )
            r = CompanyResearch(
                company_summary=e["company_summary"],
                hiring_signal=e["hiring_signal"],
                real_stack=e["real_stack"],
                culture_flags=e["culture_flags"],
                company_size=e["company_size"],
            )
            update_job_research(conn, job_id, r, sources=e.get("sources"))
            contacts = _merge_contacts([baked, e["contacts"]])
            update_job_outreach(conn, job_id, contacts)

            if _is_staffing_agency(r.culture_flags, r.company_summary):
                original = job.get("verdict", "?")
                conn.execute(
                    "UPDATE jobs SET verdict=?, reject_reason=? WHERE id=?",
                    (
                        "BAD",
                        f"stage2_demoted_from_{original}: staffing/recruiting agency",
                        job_id,
                    ),
                )
                conn.commit()
                log.info(f"[{stage_label}] {job['company']} -> DEMOTED from {original} "
                         f"(staffing/recruiting)")
            else:
                log.info(f"[{stage_label}] {job['company']} -> {r.hiring_signal}, "
                         f"{len(contacts)} contact(s)")
            e["contacts"] = contacts
            return job, e
        except Exception as ex:
            log.error(f"[{stage_label}] failed for {job_id}: {ex}")
            return job, None
    finally:
        conn.close()


def enrich_company_for_job(job_id: str) -> bool:
    """Manual 'Research' button: full forced enrichment. Demotes on agency."""
    job, e = _manual_enrich(job_id, "manual research")
    return bool(job and e)


def find_contact_for_job(job_id: str) -> int | None:
    """Manual 'Find Contact' button: same forced enrichment; returns contact
    count (0 = ran fine but none), None on failure."""
    job, e = _manual_enrich(job_id, "manual contact")
    if not job or e is None:
        return None
    return len(e.get("contacts") or [])


# ── On-demand per-person email search (точечный, UI-triggered only) ────────────
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def search_person_email(name: str, company: str, domain: str = "",
                        timeout: int = 8) -> str:
    """Targeted ddgs search for ONE person's work email. Prefers an address on
    the company domain; falls back to the first non-noreply email only when no
    company domain is known. Returns '' on any failure / no result. Fully
    contained — never raises."""
    from core import websearch
    name = (name or "").strip()
    if not name:
        return ""
    results = websearch.search(f'"{name}" {company} email'.strip(),
                               max_results=5, timeout=timeout)
    blob = " ".join(f"{r['title']} {r['body']}" for r in results)
    emails = [e for e in _EMAIL_RE.findall(blob) if "noreply" not in e.lower()]
    if not emails:
        return ""
    cdomain = clean_domain(domain)
    if cdomain:
        for e in emails:
            if e.lower().endswith("@" + cdomain) or e.lower().endswith("." + cdomain):
                return e
        return ""  # precision: a stray third-party email is worse than none
    return emails[0]


def find_emails_for_contacts(job_id: str, indices: list[int],
                             delay: float = 2.0) -> dict:
    """On-demand email enrichment for the SELECTED contacts of one job. Runs
    one ddgs search per ticked person, sequentially, with a delay between people
    (ddgs throttles aggressively). Each search is contained → a failure skips
    that person, never blocks the rest. Writes results back and returns
    {"found": [names], "not_found": [names]}."""
    report: dict = {"found": [], "not_found": []}
    conn = get_db_connection()
    try:
        job = load_job(conn, job_id)
        if not job:
            return report
        try:
            contacts = json.loads(job.get("contacts") or "[]")
        except (json.JSONDecodeError, TypeError):
            contacts = []
        company = job.get("company") or ""
        domain = job.get("domain") or ""
        targets = [i for i in (indices or []) if 0 <= i < len(contacts)]
        for n, i in enumerate(targets):
            c = contacts[i]
            name = (c.get("name") or "").strip()
            label = name or (c.get("email") or "unknown")
            if not name:
                report["not_found"].append(label)
                continue
            email = search_person_email(name, company, domain)
            if email:
                c["email"] = email
                c["confidence"] = "reported"
                src = c.get("source") or "web"
                if "search" not in src:
                    c["source"] = f"{src}+search"
                report["found"].append(name)
            else:
                report["not_found"].append(name)
            if n < len(targets) - 1:
                time.sleep(delay)  # quota-safe spacing between ddgs calls
        update_job_outreach(conn, job_id, contacts)
        log.info(
            f"[contacts] email search for '{job.get('company')}': "
            f"found {len(report['found'])}, missed {len(report['not_found'])}"
        )
        return report
    except Exception as e:
        log.error(f"[contacts] find_emails_for_contacts failed for {job_id}: {e}")
        return report
    finally:
        conn.close()


import atexit


def _cleanup_on_exit():
    """If brain1 dies for any reason while state=running, flip to error."""
    try:
        s = runner_status.read_status()
        if s["brain1"]["state"] == "running":
            runner_status.finish("brain1", error="process exited unexpectedly")
    except Exception:
        pass


def _is_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is currently running."""
    if not pid:
        return False
    try:
        if os.name == "nt":
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            h = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not h:
                return False
            exit_code = ctypes.c_ulong(0)
            ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(exit_code))
            ctypes.windll.kernel32.CloseHandle(h)
            return bool(ok) and exit_code.value == STILL_ACTIVE
        else:
            os.kill(pid, 0)
            return True
    except (OSError, PermissionError):
        return False
    except Exception:
        return False


def _check_no_duplicate_brain1():
    """Refuse to start if another brain1 process is still alive."""
    s = runner_status.read_status()
    existing_pid = s.get("brain1", {}).get("pid")
    if existing_pid and existing_pid != os.getpid() and _is_pid_alive(existing_pid):
        log.error(
            f"Another brain1 is already running (pid={existing_pid}). "
            f"Refusing to start a duplicate. Use the Stop button or "
            f"`Get-Process python | Stop-Process` first."
        )
        return False
    return True


if __name__ == "__main__":
    atexit.register(_cleanup_on_exit)
    if not _check_no_duplicate_brain1():
        sys.exit(1)
    try:
        run_brain1()
    except Exception as e:
        log.exception("Brain 1 crashed")
        runner_status.finish("brain1", error=str(e))
        raise
