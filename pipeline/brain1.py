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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from markdownify import markdownify as md
from jobspy import scrape_jobs
from google import genai
from google.genai import types
from openai import OpenAI

from core.config import OPENROUTER_URL
from core.database import get_db_connection, init_db
from core.schemas import JobFilter, CompanyResearch, ContactFind
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
def get_gemma_client_for_stage(cfg: dict, keys: dict, stage_group: str):
    """stage_group: 'stage1' or 'stage23'. Each can use a different backend."""
    backend = cfg.get(f"brain1_{stage_group}_backend") or cfg.get("brain1_backend", "gemma")

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

    return genai.Client(api_key=keys["google"]), "gemma-4-26b-a4b-it", "gemma"


# Legacy single-backend helper (kept for compatibility with manual MAYBE buttons).
def get_gemma_client(cfg: dict, keys: dict):
    return get_gemma_client_for_stage(cfg, keys, "stage23")


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


def search_github_email(query_name: str, company: str, github_pat: str) -> str | None:
    """Best-effort GitHub OSINT for a public commit email."""
    if not github_pat:
        return None
    headers = {
        "Authorization": f"token {github_pat}",
        "Accept": "application/vnd.github.v3+json",
    }
    try:
        q = f"{query_name} {company}"
        res = requests.get(
            f"https://api.github.com/search/users?q={q}&per_page=1",
            headers=headers,
            timeout=5,
        )
        data = res.json()
        if not data.get("total_count"):
            return None
        username = data["items"][0]["login"]
        events = requests.get(
            f"https://api.github.com/users/{username}/events/public",
            headers=headers,
            timeout=5,
        ).json()
        for event in events:
            if event.get("type") == "PushEvent":
                for commit in event.get("payload", {}).get("commits", []):
                    email = commit.get("author", {}).get("email", "")
                    if email and "noreply" not in email:
                        return email
    except Exception:
        return None
    return None


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
    return call_gemma(client, model, backend, system, prompt, CompanyResearch, stage=stage)


def gemma3_outreach(
    client, model, backend,
    company: str, domain: str, company_summary: str,
    github_pat: str,
    stage: str = "stage3",
) -> ContactFind:
    cdomain = clean_domain(domain)

    found_email = search_github_email("founder", company, github_pat) if github_pat else None
    if found_email:
        email_confidence = "verified"
        email_source = "github"
    elif cdomain:
        found_email = permutation_emails("founder", cdomain)[0]
        email_confidence = "pattern"
        email_source = "permutation"
    else:
        # No usable domain (only linkedin/indeed URL, or nothing) — don't fabricate.
        found_email = ""
        email_confidence = "unconfirmed"
        email_source = "permutation"

    system = (
        "You are writing a cold outreach DRAFT for the user to edit before sending. "
        "Direct, technical, under 4 sentences, no corporate fluff. The email/contact "
        "fields will be overridden by code - just write a strong outreach_draft and "
        "guess the most likely decision-maker name/title for a company this size."
    )
    prompt = (
        f"Company: {company}\n"
        f"What they do: {company_summary or '(no summary available)'}\n"
        f"Target: founder or engineering lead."
    )

    result = call_gemma(client, model, backend, system, prompt, ContactFind, stage=stage)
    # OSINT facts override the LLM's guessed email fields.
    result.email = found_email
    result.email_confidence = email_confidence  # type: ignore[assignment]
    result.email_source = email_source  # type: ignore[assignment]
    return result


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


def update_job_outreach(conn, job_id: str, c: ContactFind) -> None:
    conn.execute(
        """
        UPDATE jobs SET
            contact_name     = ?,
            contact_title    = ?,
            contact_email    = ?,
            email_confidence = ?,
            email_source     = ?,
            outreach_draft   = ?,
            gemma3_done      = 1
        WHERE id = ?
        """,
        (
            c.name, c.title, c.email,
            c.email_confidence, c.email_source, c.outreach_draft,
            job_id,
        ),
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


def has_scrape_source(sources: list[str], use_yc: bool) -> bool:
    """True when there is anything to scrape at all — JobSpy sites or YC.
    When both are off there is genuinely nothing to do (vs. silently forcing LinkedIn)."""
    return bool(sources) or bool(use_yc)


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
    results_wanted = int(cfg.get("results_wanted", 100))
    hours_old = int(cfg.get("hours_old", 72))
    github_pat = keys.get("github", "")

    # Two separate clients allow Stage 1 (filter, high volume) and Stage 2/3
    # (research+outreach, needs intelligence) to use different backends.
    s1_client, s1_model, s1_backend = get_gemma_client_for_stage(cfg, keys, "stage1")
    s23_client, s23_model, s23_backend = get_gemma_client_for_stage(cfg, keys, "stage23")

    log.info("=" * 60)
    log.info(f"Brain 1 started")
    log.info(f"Stage 1 (filter):   backend={s1_backend} model={s1_model}")
    log.info(f"Stage 2/3 (enrich): backend={s23_backend} model={s23_model}")
    log.info(f"Terms: {len(search_terms)} | Rejects: {len(hard_rejects)} | Sources: {sources} | YC: {use_yc}")
    log.info("=" * 60)

    # Empty JobSpy sources is legit for a YC-only run; both off = nothing to do,
    # so exit rather than silently forcing LinkedIn back on.
    if not has_scrape_source(sources, use_yc):
        log.warning(
            "No scrape sources enabled: JobSpy site list is empty and YC is off. "
            "Nothing to scrape — enable LinkedIn/Indeed or Y Combinator in Setup."
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
            for row in yc_rows:
                if not _process_row(row, "(YC)"):
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
                        s23_client, s23_model, s23_backend,
                        job["company"], job["domain"],
                    )
                    update_job_research(conn, job["id"], research)
                    job["company_summary"] = research.company_summary

                    # Demote on staffing/labeling/IT consulting detection.
                    flags_lower = [f.lower() for f in research.culture_flags]
                    summary_lower = (research.company_summary or "").lower()
                    staffing_markers = (
                        "staffing_agency", "data_labeling", "staffing",
                        "recruiting agency", "body shop", "gig platform",
                        "labeling service", "annotation service",
                    )
                    summary_markers = (
                        "staffing", "recruiting agency", "data labeling",
                        "ai training", "annotation service",
                        "talent scaling", "network of talent",
                        "leverage a network", "managed it capabilities",
                        "it consulting",
                    )
                    if (
                        any(m in " ".join(flags_lower) for m in staffing_markers)
                        or any(m in summary_lower for m in summary_markers)
                    ):
                        conn.execute(
                            "UPDATE jobs SET verdict=?, reject_reason=? WHERE id=?",
                            (
                                "BAD",
                                "stage2_demoted_from_GOOD: staffing/labeling agency",
                                job["id"],
                            ),
                        )
                        conn.commit()
                        log.info(
                            f"[stage2] [{i}/{len(good_jobs)}] {job['company']} "
                            f"-> DEMOTED (staffing/labeling)"
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
                    contact = gemma3_outreach(
                        s23_client, s23_model, s23_backend,
                        job["company"], job["domain"],
                        job.get("company_summary", ""),
                        github_pat,
                    )
                    update_job_outreach(conn, job["id"], contact)
                    log.info(
                        f"[stage3] [{i}/{len(survivors)}] {job['company']} "
                        f"-> contact via {contact.email_source}"
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
    client, model, backend = get_gemma_client(cfg, keys)
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

            flags_lower = [f.lower() for f in r.culture_flags]
            summary_lower = (r.company_summary or "").lower()
            staffing_markers = (
                "staffing_agency", "data_labeling", "staffing", "recruiting agency",
                "body shop", "gig platform", "labeling service", "annotation service",
            )
            summary_markers = (
                "staffing", "recruiting agency", "data labeling",
                "ai training", "annotation service",
                "talent scaling", "network of talent", "leverage a network",
                "managed it capabilities", "it consulting",
            )
            if (
                any(m in " ".join(flags_lower) for m in staffing_markers)
                or any(m in summary_lower for m in summary_markers)
            ):
                original = job.get("verdict", "?")
                conn.execute(
                    "UPDATE jobs SET verdict=?, reject_reason=? WHERE id=?",
                    (
                        "BAD",
                        f"stage2_demoted_from_{original}: staffing/labeling agency",
                        job_id,
                    ),
                )
                conn.commit()
                log.info(
                    f"[manual stage2] {job['company']} -> DEMOTED from {original} "
                    f"(staffing/labeling)"
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
    """Run Gemma 3 for a single job. Blocking; for the MAYBE 'Find Contact' button."""
    cfg = load_config()
    keys = load_keys()
    client, model, backend = get_gemma_client(cfg, keys)
    github_pat = keys.get("github", "")
    conn = get_db_connection()
    try:
        job = load_job(conn, job_id)
        if not job:
            return False
        try:
            c = gemma3_outreach(
                client, model, backend,
                job["company"], job["domain"],
                job.get("company_summary") or "",
                github_pat,
                stage="manual",
            )
            update_job_outreach(conn, job_id, c)
            log.info(f"[manual stage3] contact for {job['company']} via {c.email_source}")
            return True
        except Exception as e:
            log.error(f"[manual stage3] failed for {job_id}: {e}")
            return False
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
