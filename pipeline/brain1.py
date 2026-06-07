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
from jobspy import scrape_jobs
from google import genai
from google.genai import types
from openai import OpenAI

from core.config import OPENROUTER_URL
from core.database import get_db_connection, init_db
from core.schemas import JobFilter, CompanyResearch, WebContacts
from pipeline.sources import hn
import core.embeddings as embeddings
import core.runner_status as runner_status

# JobSpy 1.1.82 bug: an unrecognized posting country (e.g. "moldova") raises in
# Country.from_string() and aborts the whole scrape. LinkedIn ignores country
# anyway, so coercing unknowns to WORLDWIDE is safe for our linkedin-only use.
# Patched at runtime to stay portable across venvs. Remove when fixed upstream.
from jobspy.model import Country as _JobSpyCountry

_orig_country_from_string = _JobSpyCountry.from_string


def _safe_country_from_string(country_str: str):
    try:
        return _orig_country_from_string(country_str)
    except ValueError:
        return _JobSpyCountry.WORLDWIDE


_JobSpyCountry.from_string = staticmethod(_safe_country_from_string)

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

    if backend == "lmstudio":
        base_url = cfg.get("brain1_lmstudio_url", "http://localhost:1234/v1")
        model_name = (cfg.get("brain1_lmstudio_model") or "").strip()
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
        model_name = (cfg.get("brain1_openrouter_model") or "openrouter/free").strip()
        return (
            OpenAI(base_url=OPENROUTER_URL, api_key=keys.get("openrouter", "")),
            model_name,
            "openrouter",
        )

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
    lower = text.lower()
    for kw in rejects:
        if kw.lower() in lower:
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


# ── contact OSINT: web search (ddgs) → LLM snippet parse ──────────────────────
def web_search_contacts(company: str, domain: str,
                        client=None, model=None, backend=None) -> list[dict]:
    """ddgs search for the company's leadership, then let the stage-23 LLM extract
    any named person from the snippets. Fully contained: ANY failure → []."""
    if not company or client is None:
        return []
    try:
        from ddgs import DDGS
        query = f"{company} founder OR CEO OR CTO"
        results = DDGS(timeout=8).text(query, max_results=5)  # new instance per call
        snippets = "\n".join(
            f"{r.get('title', '')}: {r.get('body', '')}" for r in (results or [])
        ).strip()
    except Exception as e:
        log.warning(f"[stage3] web search failed (skipping): {e}")
        return []
    if not snippets:
        return []
    try:
        system = (
            "Extract real people named as founders/leadership of the company in "
            "these web search snippets. Include ONLY names explicitly present in "
            "the text. Never invent anyone. Empty list if no one is clearly named."
        )
        prompt = f"Company: {company}\n\n=== SEARCH SNIPPETS ===\n{snippets}"
        result = call_gemma(client, model, backend, system, prompt,
                            WebContacts, stage="stage3")
        out = []
        for c in result.contacts:
            nm = (c.name or "").strip()
            if nm:
                out.append({
                    "name": nm, "title": (c.title or "").strip(), "email": "",
                    "source": "web", "confidence": "reported",
                })
        return out
    except Exception as e:
        log.warning(f"[stage3] web snippet parse failed (skipping): {e}")
        return []


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
    from urllib.parse import quote

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

    confirmed: list[str] = []
    seen_login: set[str] = set()
    for slug in candidates:
        org = _get(f"https://api.github.com/orgs/{slug}", quiet_404=True)
        if org and org.get("login") and org["login"].lower() not in seen_login:
            seen_login.add(org["login"].lower())
            confirmed.append(org["login"])

    # type:org search catches orgs whose login differs from the slug.
    sdata = _get(
        f"https://api.github.com/search/users"
        f"?q={quote(f'{company} type:org')}&per_page=3"
    )
    for it in (sdata or {}).get("items", []) or []:
        login = it.get("login")
        if login and login.lower() not in seen_login:
            seen_login.add(login.lower())
            confirmed.append(login)

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
            if email and "noreply" in email.lower():
                email = ""  # GitHub's privacy alias — useless for outreach
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
    # JobSpy sometimes returns junk strings as the company URL
    if d in ("", "nan", "none", "null", "n/a"):
        return ""
    # Strip www./uk./es./etc subdomain
    if d.startswith("www."):
        d = d[4:]
    # Drop entirely if it's a job board or social platform — not the real company domain
    bad_hosts = (
        "linkedin.com", "indeed.com", "glassdoor.com", "google.com",
        "ziprecruiter.com", "monster.com", "wellfound.com", "ycombinator.com",
        "facebook.com", "twitter.com", "x.com",
    )
    if any(d.endswith(host) for host in bad_hosts):
        return ""
    # Sanity check: must contain a dot
    if "." not in d:
        return ""
    return d


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


# ── Gemma call wrapper ────────────────────────────────────────────────────────
def call_gemma(
    client, model: str, backend: str,
    system: str, prompt: str, schema,
    stage: str = "stage1",
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
            if backend in ("lmstudio", "openrouter"):
                def _call_lmstudio():
                    response = client.chat.completions.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": system},
                            {"role": "user", "content": prompt},
                        ],
                        temperature=0.1,
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
                    return schema.model_validate_json(_strip_json_fence(raw))
                return _run_with_timeout(_call_lmstudio, per_call_timeout + 5)

            def _call_gemma_api():
                config = types.GenerateContentConfig(
                    system_instruction=system,
                    temperature=0.1,
                    response_mime_type="application/json",
                    response_schema=schema,
                )
                response = client.models.generate_content(
                    model=model, contents=prompt, config=config
                )
                return schema.model_validate_json(_strip_json_fence(response.text or ""))
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
def gemma1_filter(client, model, backend, description: str, profile: str) -> JobFilter:
    system = (
        "You are a strict job filter. Evaluate this listing against the candidate "
        "profile below. Return GOOD if it's a strong match, MAYBE if uncertain but "
        "possible, BAD if it clearly doesn't fit. For BAD, give a brief reject_reason "
        "(under 15 words). For GOOD/MAYBE, leave reject_reason empty.\n\n"
        f"CANDIDATE PROFILE:\n{profile or '(no profile provided)'}"
    )
    prompt = f"Job listing:\n\n{description[:6000]}"
    return call_gemma(client, model, backend, system, prompt, JobFilter, stage="stage1")


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


def _is_staffing_agency(culture_flags, summary: str) -> bool:
    """True only on agency-specific signals (places people at other companies),
    not on bare product-labeling terms."""
    flags = " ".join((f or "").lower() for f in (culture_flags or []))
    blob = f"{flags} {(summary or '').lower()}"
    return any(m in blob for m in _AGENCY_MARKERS)


def gemma2_research(client, model, backend, company: str, domain: str, stage: str = "stage2") -> CompanyResearch:
    # Validate domain — skip fetch entirely for garbage values like 'nan', '', None.
    domain_clean = (domain or "").strip().lower()
    if domain_clean in ("", "nan", "none", "null"):
        site_content = "(no company domain available)"
    else:
        site_content = scrape_markdown(domain)

    # No Google-search fetch: it returns CAPTCHA HTML and wastes tokens; the
    # company site alone is enough for the classifier.

    system = (
        "You are a company OSINT analyst. Be brief and factual.\n"
        "hiring_signal: looks_real if active hiring signs, ghost if posts old/empty/"
        "evasive, uncertain if unclear.\n"
        "company_size: tiny (<50), mid (50-500), enterprise (500+).\n"
        "culture_flags: MUST include the literal string 'staffing_agency' if the company "
        "is a staffing firm, recruiting agency, gig platform, body shop, data labeling "
        "service, or any business that hires people to place them at other companies. "
        "Also include 'data_labeling' for AI training/labeling/RLHF services. "
        "Other red flags as plain strings. Empty list if none.\n"
        "real_stack: confirmed tech only, empty list if nothing found."
    )
    prompt = (
        f"Company: {company}\nDomain: {domain}\n\n"
        f"=== WEBSITE CONTENT ===\n{site_content}"
    )
    r = call_gemma(client, model, backend, system, prompt, CompanyResearch, stage=stage)
    r.company_summary = _sanitize_summary(r.company_summary)
    return r


def find_contacts(company: str, domain: str, github_pat: str,
                  client=None, model=None, backend=None) -> list[dict]:
    """Build a contacts list from REAL data only — no fabricated names.

    Merges (in priority order) team-page scrape, GitHub committers, web-search
    snippets parsed by the LLM, then role-based permutation fallback. Every
    source fails to [] independently. Deduped, decision-makers sorted first.
    Each contact: {name, title, email, source, confidence}.
    """
    team = scrape_team_contacts(domain, company)
    gh = [{
        "name": g.get("name") or "", "title": g.get("title") or "",
        "email": g.get("email") or "",
        "source": "github",
        "confidence": "verified" if g.get("email") else "reported",
    } for g in github_contacts(company, github_pat, domain)]
    web = web_search_contacts(company, domain, client, model, backend)

    perm = []
    cdomain = clean_domain(domain)
    if cdomain:
        for local in ("founder", "hello"):
            perm.append({
                "name": "", "title": "Founder / Eng lead — unverified",
                "email": f"{local}@{cdomain}",
                "source": "permutation", "confidence": "pattern",
            })

    merged = _merge_contacts([team, gh, web, perm])

    # Connect real names (team/web/github) that lack an email to a usable
    # pattern address — this is what makes the named people actually contactable.
    # The real name + title stay attached; only the email is inferred.
    if cdomain:
        for c in merged:
            if (c.get("name") and not c.get("email")
                    and c.get("source") in ("team_page", "web", "github")):
                cands = permutation_emails(c["name"], cdomain)
                if cands:
                    c["email"] = cands[0]
                    c["confidence"] = "pattern"
                    c["source"] = f"{c['source']}+permutation"

    for c in merged:
        c.setdefault("name", "")
        c.setdefault("title", "")
        c.setdefault("email", "")
    merged.sort(key=lambda c: (_CONF_TIER.get(c.get("confidence"), 3),
                               _title_rank(c.get("title", ""))))
    return merged


# ── DB write helpers ──────────────────────────────────────────────────────────
def insert_job_with_verdict(conn, job: dict, verdict: str, reject_reason: str) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO jobs (
            id, title, company, domain, location, job_type,
            salary_min, salary_max, currency, source, url,
            description, date_posted, date_scraped, description_hash,
            verdict, reject_reason, gemma1_done,
            company_summary, hiring_signal, real_stack, culture_flags, company_size,
            gemma2_done, gemma3_done,
            applied, applied_date
        ) VALUES (
            :id, :title, :company, :domain, :location, :job_type,
            :salary_min, :salary_max, :currency, :source, :url,
            :description, :date_posted, :date_scraped, :description_hash,
            :verdict, :reject_reason, 1,
            NULL, 'uncertain', '[]', '[]', 'tiny',
            0, 0,
            0, NULL
        )
        """,
        {**job, "verdict": verdict, "reject_reason": reject_reason},
    )
    conn.commit()


def update_job_research(conn, job_id: str, r: CompanyResearch) -> None:
    conn.execute(
        """
        UPDATE jobs SET
            company_summary = ?,
            hiring_signal   = ?,
            real_stack      = ?,
            culture_flags   = ?,
            company_size    = ?,
            gemma2_done     = 1
        WHERE id = ?
        """,
        (
            r.company_summary,
            r.hiring_signal,
            json.dumps(r.real_stack),
            json.dumps(r.culture_flags),
            r.company_size,
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
    """Returns (should_process, is_new). Handles smart dedup on description hash."""
    row = conn.execute(
        "SELECT description_hash FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if not row:
        return True, True
    if row["description_hash"] != new_hash:
        conn.execute(
            "UPDATE jobs SET description_hash=?, gemma1_done=0, gemma2_done=0, "
            "gemma3_done=0 WHERE id=?",
            (new_hash, job_id),
        )
        conn.commit()
        return True, False
    return False, False


def load_job(conn, job_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def fallback_job_id(row) -> str:
    """Build a stable, collision-free id for scraped rows that lack a native id
    (YC listings — JobSpy rows already carry a numeric id).

    Two distinct YC postings can share company + title + date: the same role is
    often listed for several locations, and YC's date_posted is truncated to the
    day. So ``company_title_date`` alone is NOT unique and two different jobs
    would collide on one id. We append a short hash of job_url — unique per
    posting and stable across runs (same posting → same id), which dedup and RAG
    both rely on."""
    base = f"{row.get('company')}_{row.get('title')}_{row.get('date_posted')}"
    url = str(row.get("job_url") or "")
    if url:
        suffix = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
        return f"{base}_{suffix}"
    return base


# ── JobSpy wrapper with defensive country retry ───────────────────────────────
def safe_scrape(term: str, sources: list[str], results_wanted: int, hours_old: int):
    """JobSpy occasionally injects random country strings into LinkedIn flow
    (kenya/iceland bug in 1.1.82). Retry up to 4 times; the injected country
    is random per-call so retrying often gets a clean call."""
    base_kwargs = dict(
        site_name=sources,
        search_term=term,
        is_remote=True,
        results_wanted=results_wanted,
        hours_old=hours_old,
        linkedin_fetch_description=True,
        location="Worldwide",
    )
    max_attempts = 4
    for attempt in range(1, max_attempts + 1):
        try:
            return scrape_jobs(country_indeed="worldwide", **base_kwargs)
        except Exception as e:
            if "Invalid country string" in str(e):
                if attempt < max_attempts:
                    log.warning(
                        f"JobSpy country bug for '{term}' (attempt {attempt}/{max_attempts}), retrying"
                    )
                    time.sleep(1)
                else:
                    log.error(
                        f"JobSpy country bug for '{term}' persisted after {max_attempts} attempts"
                    )
                    return None
            else:
                log.error(f"Scrape failed for '{term}': {e}")
                return None


# ── YC startups scraper (company-based, separate from JobSpy) ─────────────────
def yc_jobs_to_rows(yc_jobs: list[dict]) -> list[dict]:
    """Convert ycombinator_jobs_scraper output into JobSpy-style row dicts so YC
    listings flow through the exact same Stage 1 path as LinkedIn/Indeed. YC has
    no salary or numeric id; we leave id=None so the downstream fallback builds a
    stable one from company/title/date."""
    rows = []
    for j in yc_jobs:
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
            # Keep YC's tri-state remote flag (True/False/None=unknown) for the pre-Stage-1 filter.
            "is_remote": j.get("is_remote"),
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
    """Parse a YC row's date_posted (ISO 'YYYY-MM-DD' from the company ATS) to a
    date. Returns None when empty or unparseable."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def apply_yc_date_filter(rows: list[dict], hours_old: int,
                         now: datetime | None = None) -> list[dict]:
    """Keep only YC rows whose date_posted falls within the last `hours_old`
    hours — the freshness window JobSpy enforces server-side but YC has no param
    for, so stale 2024/2025 listings would otherwise leak in.

    YC dates are day-granular ISO strings from the company ATS (Greenhouse
    updated_at / Lever createdAt / Ashby publishedAt), so the window is compared
    at day granularity (erring toward inclusion at the boundary). Undated or
    unparseable rows are DROPPED, not kept: we can't confirm they're fresh, and
    silently treating them as fresh is exactly the stale-leak bug. hours_old<=0
    disables the filter (keep all)."""
    if not hours_old or hours_old <= 0:
        return rows
    now = now or datetime.now(timezone.utc)
    cutoff_date = (now - timedelta(hours=hours_old)).date()
    kept = []
    for r in rows:
        d = _parse_yc_date(str(r.get("date_posted") or ""))
        if d is not None and d >= cutoff_date:
            kept.append(r)
    return kept


def safe_scrape_yc(cfg: dict):
    """Scrape small early-stage YC startups once (company-based, not per-term).
    Any failure is non-fatal and returns [] — a YC error must never kill the
    LinkedIn/Indeed scrape (mirrors the JobSpy country-bug handling)."""
    try:
        from ycombinator_jobs_scraper import scrape_yc_jobs
    except Exception as e:
        log.error(f"YC scraper unavailable (skipping): {e}")
        return []
    try:
        # keyword=None on purpose: YC's keyword is a crude single-substring title
        # filter; Stage 1's LLM does the real filtering on the description instead.
        jobs = scrape_yc_jobs(
            max_companies=int(cfg.get("yc_max_companies", 100)),
            max_team_size=int(cfg.get("yc_max_team_size", 50)),
            years_back=int(cfg.get("yc_years_back", 3)),
            keyword=None,
        )
        return yc_jobs_to_rows(jobs)
    except Exception as e:
        log.error(f"YC scrape failed (skipping): {e}")
        return []


# ── Source selection helpers ──────────────────────────────────────────────────
def jobspy_enabled(sources: list[str]) -> bool:
    """True when at least one JobSpy site (LinkedIn/Indeed) is selected.
    An empty list is legitimate (YC-only run) and means skip the JobSpy term loop."""
    return bool(sources)


def has_scrape_source(sources: list[str], use_yc: bool, use_hn: bool = False) -> bool:
    """True when there is anything to scrape at all — JobSpy sites, YC, or HN.
    When all are off there is genuinely nothing to do (vs. silently forcing LinkedIn)."""
    return bool(sources) or bool(use_yc) or bool(use_hn)


# ── Main entry: sequential Stage 1 → Stage 2 → Stage 3 ────────────────────────
def run_brain1() -> None:
    cfg = load_config()
    keys = load_keys()
    profile_text = cfg.get("profile", "")

    search_terms = [
        t.strip() for t in cfg.get("search_terms", "").splitlines() if t.strip()
    ] or ["machine learning engineer remote"]
    hard_rejects = [
        t.strip() for t in cfg.get("hard_rejects", "").splitlines() if t.strip()
    ]
    sources = cfg.get("sources", ["linkedin"])
    use_yc = bool(cfg.get("use_yc"))
    use_hn = bool(cfg.get("use_hn"))
    results_wanted = int(cfg.get("results_wanted", 100))
    hours_old = int(cfg.get("hours_old", 72))
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

    # Empty JobSpy sources is legit for a YC/HN-only run; all off = nothing to do,
    # so exit rather than silently forcing LinkedIn back on.
    if not has_scrape_source(sources, use_yc, use_hn):
        log.warning(
            "No scrape sources enabled: JobSpy site list is empty and YC/HN are off. "
            "Nothing to scrape — enable LinkedIn/Indeed, Y Combinator, or Hacker News in Setup."
        )
        runner_status.start("brain1")
        runner_status.finish("brain1", error="no sources enabled")
        return

    init_db()
    runner_status.start("brain1")
    runner_status.patch(
        "brain1",
        pid=os.getpid(),
        stage1="initializing", stage2="idle", stage3="idle",
        scraped=0, good=0, maybe=0, bad=0, hard_rej=0,
    )

    # Watchdog: hard-kills us if the dashboard dies while we're stuck inside
    # synchronous jobspy code that can't reach the cooperative heartbeat checks.
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

    counts = {"scraped": 0, "good": 0, "maybe": 0, "bad": 0, "hard_rej": 0}

    good_jobs: list[dict] = []
    # Cross-source dedup by job_url (LinkedIn/Indeed/YC can overlap within a run).
    seen_urls: set[str] = set()

    conn = get_db_connection()
    last_heartbeat = time.monotonic()
    aborted = False

    def _process_row(row, progress_label: str) -> bool:
        """Run one scraped row (JobSpy Series or YC dict) through hard-reject +
        Stage 1. Returns False if the dashboard heartbeat died and we should
        abort the whole scrape; True otherwise (including normal skips)."""
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
            return True
        url = str(row.get("job_url") or "")
        if url and url in seen_urls:
            return True
        if url:
            seen_urls.add(url)
        dhash = description_hash(desc)
        process, is_new = should_process(conn, job_id, dhash)
        if not process:
            return True

        job = {
            "id": job_id,
            "title": str(row.get("title") or ""),
            "company": str(row.get("company") or ""),
            "domain": str(row.get("company_url_direct") or row.get("company_url") or ""),
            "location": str(row.get("location") or ""),
            "job_type": str(row.get("job_type") or ""),
            "salary_min": row.get("min_amount"),
            "salary_max": row.get("max_amount"),
            "currency": str(row.get("currency") or ""),
            "source": str(row.get("site") or ""),
            "url": url,
            "description": desc,
            "date_posted": str(row.get("date_posted") or ""),
            "date_scraped": datetime.now(timezone.utc).isoformat(),
            "description_hash": dhash,
        }

        counts["scraped"] += 1

        # ── hard reject ──
        # Include company name: many staffing firms have giveaway names but normal job text.
        reject_text = f"{job['title']} {job['company']} {desc}"
        reject_kw = hard_reject_check(reject_text, hard_rejects)
        if reject_kw:
            insert_job_with_verdict(conn, job, "BAD", f"hard_reject: {reject_kw}")
            counts["hard_rej"] += 1
            runner_status.patch("brain1", **counts)
            return True

        # ── Gemma 1 filter ──
        runner_status.patch(
            "brain1",
            stage1=f"filter {counts['scraped']} {progress_label}",
        )
        try:
            g1 = gemma1_filter(s1_client, s1_model, s1_backend, desc, profile_text)
            insert_job_with_verdict(conn, job, g1.verdict, g1.reject_reason)
            log.info(f"[stage1] {g1.verdict:5s} {job['title']} @ {job['company']}")
            # Best-effort embed-on-scrape for RAG; a failed embed must never fail the scrape.
            embeddings.embed_and_store(conn, job)
            if g1.verdict == "GOOD":
                counts["good"] += 1
                job["verdict"] = "GOOD"  # stage 2 reads this
                good_jobs.append(job)
            elif g1.verdict == "MAYBE":
                counts["maybe"] += 1
            else:
                counts["bad"] += 1
            runner_status.patch("brain1", **counts)

        except Exception as e:
            log.error(f"[stage1] Gemma1 failed for {job_id}: {e}")
            insert_job_with_verdict(conn, job, "BAD", f"gemma1_error: {e}")
            counts["bad"] += 1
            time.sleep(1)
        return True

    try:
        # YC-only run (no JobSpy sites) skips this loop — avoids an empty JobSpy call.
        scrape_terms = list(enumerate(search_terms, 1)) if jobspy_enabled(sources) else []
        if not scrape_terms:
            log.info("[stage1] No JobSpy sources selected; skipping LinkedIn/Indeed scrape.")
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
            df = safe_scrape(term, sources, results_wanted, hours_old)
            if df is None or len(df) == 0:
                continue

            progress = f"({term_idx}/{len(search_terms)})"
            for _, row in df.iterrows():
                if not _process_row(row, progress):
                    aborted = True
                    break

            if aborted:
                break

        # ── YC startups (company-based, scraped once — not per term) ──────────
        if not aborted and cfg.get("use_yc"):
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
            # YC bypasses JobSpy's server-side hours_old, so apply the same
            # freshness window here before Stage 1 (stale jobs never hit Gemma).
            before = len(yc_rows)
            yc_rows = apply_yc_date_filter(yc_rows, hours_old)
            log.info(
                f"[stage1] YC date filter (<= {hours_old}h): dropped "
                f"{before - len(yc_rows)} stale/undated, kept {len(yc_rows)}"
            )
            for row in yc_rows:
                if not _process_row(row, "(YC)"):
                    aborted = True
                    break

        # ── Hacker News "Who is hiring?" (single thread, scraped once) ────────
        if not aborted and cfg.get("use_hn"):
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

        # ── Stage 2: research each GOOD company sequentially ─────────────────
        survivors: list[dict] = []
        if not aborted and good_jobs:
            log.info(f"[stage2] starting research on {len(good_jobs)} GOOD jobs")
            for i, job in enumerate(good_jobs, 1):
                if not runner_status.dashboard_is_alive(max_age_seconds=90):
                    log.warning("[stage2] dashboard gone, stopping enrichment")
                    aborted = True
                    break
                runner_status.patch(
                    "brain1",
                    stage2=f"researching {job['company']} ({i}/{len(good_jobs)})",
                )
                try:
                    research = gemma2_research(
                        s2_client, s2_model, s2_backend,
                        job["company"], job["domain"],
                    )
                    update_job_research(conn, job["id"], research)
                    job["company_summary"] = research.company_summary

                    # Demote only on agency signals, not bare product-labeling.
                    if _is_staffing_agency(research.culture_flags, research.company_summary):
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
                            f"[stage2] [{i}/{len(good_jobs)}] {job['company']} "
                            f"-> DEMOTED (staffing/recruiting)"
                        )
                    else:
                        survivors.append(job)
                        log.info(
                            f"[stage2] [{i}/{len(good_jobs)}] {job['company']} "
                            f"-> {research.hiring_signal} ({research.company_size})"
                        )
                except Exception as e:
                    log.error(
                        f"[stage2] [{i}/{len(good_jobs)}] failed for {job['company']}: {e}"
                    )
                    # Failed enrichment: keep the job as GOOD so user can manually retry
                    survivors.append(job)
            runner_status.patch("brain1", stage2="idle")

        # ── Stage 3: outreach for survivors ──────────────────────────────────
        if not aborted and survivors:
            log.info(f"[stage3] starting outreach on {len(survivors)} survivors")
            for i, job in enumerate(survivors, 1):
                if not runner_status.dashboard_is_alive(max_age_seconds=90):
                    log.warning("[stage3] dashboard gone, stopping outreach")
                    aborted = True
                    break
                runner_status.patch(
                    "brain1",
                    stage3=f"outreach for {job['company']} ({i}/{len(survivors)})",
                )
                try:
                    contacts = find_contacts(
                        job["company"], job["domain"], github_pat,
                        s3_client, s3_model, s3_backend,
                    )
                    update_job_outreach(conn, job["id"], contacts)
                    log.info(
                        f"[stage3] [{i}/{len(survivors)}] {job['company']} "
                        f"-> {len(contacts)} contact(s)"
                    )
                except Exception as e:
                    log.error(
                        f"[stage3] [{i}/{len(survivors)}] failed for {job['company']}: {e}"
                    )
            runner_status.patch("brain1", stage3="idle")

    finally:
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
        log.info("=" * 60)
        log.info(
            f"Brain 1 complete | scraped={counts['scraped']} "
            f"good={counts['good']} maybe={counts['maybe']} "
            f"bad={counts['bad']} hard_rej={counts['hard_rej']}"
        )
        log.info("=" * 60)


# ── Single-job public entry points (for dashboard MAYBE buttons) ──────────────
def enrich_company_for_job(job_id: str) -> bool:
    """Run Gemma 2 for a single job. Blocking; meant for the MAYBE 'Research' button.
    Also demotes the job to BAD if Gemma 2 detects staffing/labeling agency."""
    cfg = load_config()
    keys = load_keys()
    client, model, backend = get_gemma_client(cfg, keys, "stage2")
    conn = get_db_connection()
    try:
        job = load_job(conn, job_id)
        if not job:
            return False
        try:
            r = gemma2_research(
                client, model, backend, job["company"], job["domain"],
                stage="manual",
            )
            update_job_research(conn, job_id, r)

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
                log.info(
                    f"[manual stage2] {job['company']} -> DEMOTED from {original} "
                    f"(staffing/recruiting)"
                )
            else:
                log.info(f"[manual stage2] {job['company']} -> {r.hiring_signal}")
            return True
        except Exception as e:
            log.error(f"[manual stage2] failed for {job_id}: {e}")
            return False
    finally:
        conn.close()


def find_contact_for_job(job_id: str) -> bool:
    """Find real contacts for a single job. Blocking; for the 'Find Contact' button.
    Team-page + GitHub + web-search OSINT, permutation fallback. No fabricated names."""
    cfg = load_config()
    keys = load_keys()
    client, model, backend = get_gemma_client(cfg, keys, "stage3")
    github_pat = keys.get("github", "")
    conn = get_db_connection()
    try:
        job = load_job(conn, job_id)
        if not job:
            return False
        try:
            contacts = find_contacts(
                job["company"], job["domain"], github_pat,
                client, model, backend,
            )
            update_job_outreach(conn, job_id, contacts)
            log.info(f"[manual stage3] {len(contacts)} contact(s) for {job['company']}")
            return True
        except Exception as e:
            log.error(f"[manual stage3] failed for {job_id}: {e}")
            return False
    finally:
        conn.close()


# ── On-demand per-person email search (точечный, UI-triggered only) ────────────
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def search_person_email(name: str, company: str, domain: str = "",
                        timeout: int = 8) -> str:
    """Targeted ddgs search for ONE person's work email. Prefers an address on
    the company domain; falls back to the first non-noreply email only when no
    company domain is known. Returns '' on any failure / no result. Fully
    contained — never raises."""
    name = (name or "").strip()
    if not name:
        return ""
    try:
        from ddgs import DDGS
        query = f'"{name}" {company} email'.strip()
        results = DDGS(timeout=timeout).text(query, max_results=5)
    except Exception as e:
        log.warning(f"[contacts] email search failed for {name} (skipping): {e}")
        return ""
    blob = " ".join(
        f"{r.get('title', '')} {r.get('body', '')}" for r in (results or [])
    )
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
