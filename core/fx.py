"""
core/fx.py

USD conversion for the salary floor. One keyless source, cached on disk.

Only hh publishes salary at all, and it quotes RUR / UZS / KZT / BYR, so a floor
written in USD can't compare against anything without this.

The rule everywhere here: refuse rather than guess. Unknown currency, dead
network with a stale cache, nonsense amount - all return None, and callers must
read None as "I don't know", never as zero. A salary filter that treats unknown
as low would bury jobs for the crime of being priced in the wrong currency.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)

URL = "https://open.er-api.com/v6/latest/USD"
CACHE_PATH = Path(__file__).parent / "db" / "fx_rates.json"
MAX_AGE_DAYS = 7          # past a week a rate is a guess dressed up as a fact
REFRESH_AFTER = 12 * 3600  # don't hammer it; daily-updated source anyway
TIMEOUT = 15

# hh still quotes the pre-redenomination codes. RUB replaced RUR in 1998 and BYN
# replaced BYR in 2016, so a straight lookup misses more than half the rows.
ALIASES = {"RUR": "RUB", "BYR": "BYN"}

_mem: dict | None = None


def _read_cache() -> dict | None:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_cache(payload: dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(payload), encoding="utf-8")
    except Exception as e:
        log.debug(f"[fx] cache write failed: {e}")


def _fetch() -> dict | None:
    try:
        r = requests.get(URL, timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        log.warning(f"[fx] rate fetch failed: {type(e).__name__}")
        return None
    rates = d.get("rates") or {}
    if not rates.get("EUR"):
        log.warning("[fx] response had no usable rates")
        return None
    return {"fetched_at": time.time(), "rates": rates,
            "as_of": d.get("time_last_update_utc") or ""}


def load(force: bool = False) -> dict | None:
    """Rates per 1 USD, cached. None when there's nothing trustworthy to use."""
    global _mem
    payload = _mem or _read_cache()
    fresh = payload and (time.time() - payload.get("fetched_at", 0)) < REFRESH_AFTER
    if payload and fresh and not force:
        _mem = payload
        return payload

    fetched = _fetch()
    if fetched:
        _write_cache(fetched)
        _mem = fetched
        return fetched

    # network is down: the cache is fine right up until it isn't
    if payload:
        age = time.time() - payload.get("fetched_at", 0)
        if age < MAX_AGE_DAYS * 86400:
            log.info(f"[fx] using cached rates, {age / 3600:.0f}h old")
            _mem = payload
            return payload
        log.warning(f"[fx] cached rates are {age / 86400:.0f} days old — "
                    "refusing to convert rather than quote a stale number")
    return None


def to_usd(amount, currency: str) -> float | None:
    """Convert to USD, or None when I can't stand behind the number."""
    if amount is None or not currency:
        return None
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    code = ALIASES.get(currency.upper(), currency.upper())
    if code == "USD":
        return amount
    payload = load()
    if not payload:
        return None
    rate = (payload.get("rates") or {}).get(code)
    if not rate:
        log.debug(f"[fx] no rate for {currency} (as {code})")
        return None
    return amount / rate
