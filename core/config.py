"""
core/config.py

Single source of truth for HunterJobs config + API keys.

CONFIG_PATH is anchored explicitly to the repo root (one level up from this
file, which lives in core/), so it resolves the same regardless of the process
CWD. Previously this logic was duplicated across dashboard.py, brain1.py,
brain2.py and brain2_chat.py.
"""

from __future__ import annotations

import json
from pathlib import Path

# core/config.py sits one level deep, so the repo root is two parents up.
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"

# OpenRouter is OpenAI-API-compatible; single source of truth for its base URL.
OPENROUTER_URL = "https://openrouter.ai/api/v1"


DEFAULT_CONFIG = {
    "theme": "dark",
    "profile": "",
    # Stage 1 geo-eligibility. Empty = no geo filtering (no assumptions). Format:
    # base country, passport, work authorization, sponsorship/relocation stance,
    # remote scope, timezone.
    "geo_eligibility": "",
    "search_terms": "machine learning engineer remote\ngenerative AI engineer remote",
    "hard_rejects": "US citizenship required\nW2 only\nsecurity clearance",
    # Agency-suspects the user dismissed as "not an agency" — kept out of suggestions.
    "dismissed_suspects": [],
    # Companies the user manually staged as suspects (alongside Stage 2 auto-flags).
    "manual_suspects": [],
    "salary_floor": 4500,
    "sources": ["linkedin"],
    # YC startups are company-based, scraped separately from JobSpy sites.
    "use_yc": False,
    "yc_max_companies": 100,  # 0 = all hiring companies
    "yc_max_team_size": 50,  # 0 = no cap
    "yc_years_back": 3,
    "yc_remote_only": True,
    "yc_hours_old": 720,  # YC/WaaS listings stay up for months; global hours_old is too tight
    # Hacker News "Who is hiring?" — single monthly thread, scraped via free APIs.
    "use_hn": False,
    "hn_remote_only": True,
    "hn_max_jobs": 200,
    "results_wanted": 100,
    "hours_old": 72,
    "use_rag": True,  # off = no embedding calls, no similar-applications panel
    "brain1_backend": "gemma",
    "brain1_stage1_backend": "gemma",
    "brain1_stage23_backend": "gemma",
    # Per-stage Gemma model (stages 2/3 share a backend but pick models separately).
    "brain1_stage1_gemma_model": "gemma-4-26b-a4b-it",
    "brain1_stage2_gemma_model": "gemma-4-26b-a4b-it",
    "brain1_stage3_gemma_model": "gemma-4-26b-a4b-it",
    "brain1_lmstudio_url": "http://localhost:1234/v1",
    "brain1_lmstudio_model": "",
    "brain1_openrouter_model": "openrouter/free",
    "brain2_backend": "gemini",
    # Brain 2 persona/voice (snapshot + chat). Empty = no persona injected.
    "brain2_persona": "",
    "brain2_gemini_model": "gemini-3.5-flash",
    "brain2_gemma_model": "gemma-4-26b-a4b-it",
    "brain2_anthropic_model": "claude-sonnet-4-6",
    "brain2_openai_model": "gpt-5.5",
    "brain2_lmstudio_url": "http://localhost:1234/v1",
    "brain2_lmstudio_model": "",
    "brain2_openrouter_model": "openrouter/free",
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        for k, v in DEFAULT_CONFIG.items():
            data.setdefault(k, v)
        return data
    CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def load_keys() -> dict:
    try:
        import keys
        return {
            "google": getattr(keys, "GOOGLE_API_KEY", ""),
            "anthropic": getattr(keys, "ANTHROPIC_API_KEY", ""),
            "github": getattr(keys, "GITHUB_PAT", ""),
            "openai": getattr(keys, "OPENAI_API_KEY", ""),
            "openrouter": getattr(keys, "OPENROUTER_API_KEY", ""),
        }
    except ImportError:
        return {"google": "", "anthropic": "", "github": "", "openai": "",
                "openrouter": ""}
