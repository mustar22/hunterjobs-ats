"""
core/websearch.py

One web-search entry point for the whole app. Keyed providers (Tavily, Serper)
when a key exists — reliable, clean snippets; keyless ddgs as the fallback so
the no-accounts local app keeps working. Volume is low post-cache, so free
tiers go a long way.
"""

from __future__ import annotations

import logging
import threading
import time

import requests

log = logging.getLogger(__name__)

# min seconds between ddgs calls — keyless engines 429 when hammered
DDGS_MIN_INTERVAL = 3.0
# measured 2026-08-08: duckduckgo, startpage and mojeek returned nothing at all,
# every query, both languages. Don't put them back without retesting.
DDGS_BACKEND = "yahoo, brave"
# offered in the UI, best first — a toggled set is rebuilt in this order
ENGINES = ("yahoo", "brave", "google", "duckduckgo", "startpage",
           "mojeek", "wikipedia")
# hh is a Russian-language board and Yandex is the only engine that indexes it
# well (11/14 vs yahoo 6, brave 3 on companies with a known site)
HH_BACKEND = "yandex, yahoo, brave"
_ddgs_last_call = 0.0
_ddgs_lock = threading.Lock()


def _backends(source: str = "") -> str:
    """Engine order for one source. Yandex is always on for hh and opt-in
    elsewhere, so a Russian engine only sees Russian queries by default."""
    from core.config import load_config
    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    chain = (cfg.get("search_backends") or DDGS_BACKEND).strip() or DDGS_BACKEND
    if source == "hh":
        return HH_BACKEND if not cfg.get("search_backends") else f"yandex, {chain}"
    if cfg.get("search_yandex") and "yandex" not in chain:
        chain = f"yandex, {chain}"
    return chain


def _load_search_keys() -> dict:
    try:
        import keys
        return {"tavily": getattr(keys, "TAVILY_API_KEY", ""),
                "serper": getattr(keys, "SERPER_API_KEY", "")}
    except ImportError:
        return {"tavily": "", "serper": ""}


def _tavily(query: str, key: str, max_results: int, timeout: int) -> list[dict]:
    r = requests.post(
        "https://api.tavily.com/search",
        json={"api_key": key, "query": query, "max_results": max_results},
        timeout=timeout,
    )
    r.raise_for_status()
    return [{"title": x.get("title") or "", "body": x.get("content") or "",
             "url": x.get("url") or ""} for x in r.json().get("results") or []]


def _serper(query: str, key: str, max_results: int, timeout: int) -> list[dict]:
    r = requests.post(
        "https://google.serper.dev/search",
        headers={"X-API-KEY": key, "Content-Type": "application/json"},
        json={"q": query, "num": max_results},
        timeout=timeout,
    )
    r.raise_for_status()
    return [{"title": x.get("title") or "", "body": x.get("snippet") or "",
             "url": x.get("link") or ""} for x in (r.json().get("organic") or [])[:max_results]]


def _ddgs(query: str, max_results: int, timeout: int,
          source: str = "") -> list[dict]:
    global _ddgs_last_call
    from ddgs import DDGS
    backend = _backends(source)
    with _ddgs_lock:
        wait = DDGS_MIN_INTERVAL - (time.monotonic() - _ddgs_last_call)
        if wait > 0:
            time.sleep(wait)
        try:
            results = DDGS(timeout=timeout).text(
                query, max_results=max_results, backend=backend)
        finally:
            _ddgs_last_call = time.monotonic()
    return [{"title": r.get("title") or "", "body": r.get("body") or "",
             "url": r.get("href") or r.get("url") or ""} for r in (results or [])]


def search(query: str, max_results: int = 5, timeout: int = 8,
           source: str = "") -> list[dict]:
    """[{title, body, url}] via the best available backend: Tavily → Serper →
    ddgs. A keyed provider failing falls through to ddgs; any total failure
    returns [] — search must never break a scan."""
    keys = _load_search_keys()
    for name, key, fn in (("tavily", keys["tavily"], _tavily),
                          ("serper", keys["serper"], _serper)):
        if not key:
            continue
        try:
            out = fn(query, key, max_results, timeout)
            if not out:
                log.warning(f"[websearch] {name} returned 0 results for {query!r}")
            return out
        except Exception as e:
            log.warning(f"[websearch] {name} failed, falling back: {e}")
    try:
        out = _ddgs(query, max_results, timeout, source)
    except Exception as e:
        log.warning(f"[websearch] ddgs ({_backends(source)}) failed: {e}")
        return []
    # three backends died silently once already; never let that be quiet again
    if not out:
        log.warning(f"[websearch] ddgs ({_backends(source)}) returned 0 results "
                    f"for {query!r} — research will be running blind")
    return out


def snippets_text(results: list[dict]) -> str:
    """Flatten results for an LLM prompt / regex pass."""
    return "\n".join(f"{r['title']}: {r['body']}" for r in results).strip()
