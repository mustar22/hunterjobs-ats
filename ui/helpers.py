"""
ui/helpers.py

Pure formatting / utility helpers for the UI: verdict/source/signal pills,
timestamp formatting, status-dot class mapping, a crash-safe notify wrapper,
and the run-in-thread async helper. No dependencies on other ui modules.
"""

from __future__ import annotations

import json

from nicegui import ui


def verdict_pill(verdict: str, reject_reason: str = "") -> str:
    if "stage2_demoted" in (reject_reason or ""):
        return '<span class="pill pill-bad">DEMOTED</span>'
    cls = {"GOOD": "pill-good", "MAYBE": "pill-maybe", "BAD": "pill-bad"}.get(
        verdict, "pill-unc"
    )
    return f'<span class="pill {cls}">{verdict}</span>'


def source_pill(source: str) -> str:
    """Brand-colored badge showing where a job came from. Reuses the .pill
    geometry; brand hexes are set inline since they aren't theme variables."""
    brands = {
        "linkedin": ("LinkedIn", "#0A66C2"),
        "indeed":   ("Indeed",   "#003A9B"),
        "yc":       ("YC",       "#FF6600"),
    }
    key = (source or "").strip().lower()
    if key in brands:
        label, color = brands[key]
        return (
            f'<span class="pill" style="color: #fff; background: {color}; '
            f'border: 1px solid {color};">{label}</span>'
        )
    # Unknown/missing → neutral gray pill (theme-consistent), raw value or em dash.
    label = (source or "").strip() or "—"
    return f'<span class="pill pill-unc">{label}</span>'


def signal_pill(signal: str, culture_flags_json: str = "[]") -> str:
    # If staffing/labeling was flagged, override the 'REAL' pill — REAL is
    # misleading when the company is a real-but-staffing firm.
    try:
        flags = [f.lower() for f in json.loads(culture_flags_json or "[]")]
    except (json.JSONDecodeError, TypeError):
        flags = []
    if any("staffing" in f or "data_labeling" in f for f in flags):
        return '<span class="pill pill-bad">STAFFING</span>'

    cls = {"looks_real": "pill-real", "ghost": "pill-ghost"}.get(signal, "pill-unc")
    label = {"looks_real": "REAL", "ghost": "GHOST", "uncertain": "UNC"}.get(
        signal, (signal or "—").upper()
    )
    return f'<span class="pill {cls}">{label}</span>'


def fmt_ts(ts: str | None, length: int = 19) -> str:
    if not ts:
        return "never"
    return str(ts)[:length].replace("T", " ")


def status_dot_class(state: str) -> str:
    return {"running": "running", "done": "done", "error": "error"}.get(state, "idle")


# Run a blocking call in a thread, return awaitable result.
async def run_in_thread(fn, *args, **kwargs):
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))


def safe_notify(msg: str, **kw) -> None:
    """ui.notify that doesn't crash when the calling slot has been destroyed
    (e.g. user navigated away while an async task was running, or the row
    was rebuilt by a refresh during the await)."""
    try:
        ui.notify(msg, **kw)
    except Exception:
        # parent slot deleted, no client context, etc — best-effort only
        pass
