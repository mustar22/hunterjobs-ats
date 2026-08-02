"""
ui/tabs.py

The Applied, Market Analyzer, Logs, and Setup tabs. Applied reuses
render_job_row from ui.jobs; Market/Setup touch config (core.config), the
brains (pipeline), and process control (spawn/kill). LOG_PATH (read by the Logs
tab) is anchored to the repo root.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nicegui import ui

from core import database  # for live RAG_AVAILABLE flag
from core.database import get_db_connection
import core.embeddings as embeddings  # RAG backfill
import core.runner_status as runner_status
from core.config import load_config, save_config, load_keys, OPENROUTER_URL
from pipeline import brain2_chat  # chat + clear history
from pipeline.process_control import spawn_detached, kill_pid, _is_pid_alive

from ui.helpers import status_dot_class, fmt_ts, safe_notify, run_in_thread
from ui.db_queries import fetch_applied, fetch_agency_suspects
from ui.jobs import render_job_row


# ── OpenRouter live model picker ──────────────────────────────────────────────
# Session cache so tab switches don't re-hit the API. None = unfetched or last
# fetch failed (retry next render); a list = a reusable successful fetch.
_OPENROUTER_MODELS_CACHE = None


def _fmt_openrouter_price(pricing: dict) -> str:
    """Format OpenRouter per-token pricing into a compact per-million label.
    Returns 'FREE' when both prompt+completion are 0, else e.g.
    '$0.43/M in · $0.87/M out'. Empty string if pricing is unparseable."""
    try:
        prompt = float(pricing.get("prompt", "0") or 0)
        completion = float(pricing.get("completion", "0") or 0)
    except (TypeError, ValueError):
        return ""
    if prompt == 0 and completion == 0:
        return "FREE"
    return (f"${prompt * 1_000_000:.2f}/M in"
            f" · ${completion * 1_000_000:.2f}/M out")


def _fetch_openrouter_models() -> list[dict]:
    """Fetch + cache the OpenRouter model catalog (no auth needed for the list).
    Returns a list of {id, price_label}. On failure returns [] WITHOUT caching,
    so a later render retries (network may have come back)."""
    global _OPENROUTER_MODELS_CACHE
    if _OPENROUTER_MODELS_CACHE is not None:
        return _OPENROUTER_MODELS_CACHE
    try:
        import requests
        r = requests.get(f"{OPENROUTER_URL}/models", timeout=8)
        r.raise_for_status()
        data = r.json().get("data", [])
        models = []
        for m in data:
            mid = m.get("id", "")
            if not mid:
                continue
            models.append({
                "id": mid,
                "price_label": _fmt_openrouter_price(m.get("pricing", {}) or {}),
            })
        models.sort(key=lambda x: x["id"].lower())
        _OPENROUTER_MODELS_CACHE = models  # cache only on success
        return models
    except Exception:
        return []  # leave cache None → retry on next render


def _openrouter_model_picker(current_value: str, label: str):
    """Reusable picker for an OpenRouter model id. Returns a NiceGUI element
    whose `.value` holds the selected/typed model id (read it in do_save).

    Live path: a searchable dropdown (type to filter the 315+ list by substring
    against id + inline pricing). Offline/failed path: a plain free-text input
    pre-filled with the current config value, so the Setup tab always renders."""
    current = (current_value or "openrouter/free").strip()
    models = _fetch_openrouter_models()
    if not models:
        return ui.input(
            label=f"{label} (catalog unavailable — type a model id)",
            value=current,
        ).props("outlined").style("width: 420px;")

    options = {}
    for m in models:
        price = m["price_label"]
        options[m["id"]] = f'{m["id"]}  ·  {price}' if price else m["id"]
    # Always keep the current value selectable, even if it left the catalog.
    if current not in options:
        options[current] = current
    return ui.select(
        options, value=current, with_input=True, label=f"{label} (type to search)",
    ).props("outlined").style("min-width: 420px;")


# ── Gemma live model picker (Google AI Studio) ────────────────────────────────
_GEMMA_MODELS_CACHE = None
_GEMINI_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"


def _fetch_gemma_models(api_key: str) -> list[str]:
    """Fetch + cache the Gemma model ids from Google AI Studio. Filters to Gemma
    only (drops Gemini + embedding models). Returns [] WITHOUT caching on missing
    key or failure, so a later render retries."""
    global _GEMMA_MODELS_CACHE
    if _GEMMA_MODELS_CACHE is not None:
        return _GEMMA_MODELS_CACHE
    if not api_key:
        return []
    try:
        import requests
        r = requests.get(_GEMINI_MODELS_URL, params={"key": api_key}, timeout=8)
        r.raise_for_status()
        out = []
        for m in r.json().get("models", []):
            name = (m.get("name") or "").split("/")[-1]
            low = name.lower()
            if "gemma" in low and "embedding" not in low and "gemini" not in low:
                out.append(name)
        out = sorted(set(out))
        _GEMMA_MODELS_CACHE = out  # cache only on success
        return out
    except Exception:
        return []


# ── Gemini live model picker (same catalog, Gemini side of it) ────────────────
_GEMINI_MODELS_CACHE = None


def _fetch_gemini_models(api_key: str) -> list[str]:
    """Gemini ids from the same AI Studio catalog. Drops embeddings, TTS/vision
    variants and anything not usable as a chat model."""
    global _GEMINI_MODELS_CACHE
    if _GEMINI_MODELS_CACHE is not None:
        return _GEMINI_MODELS_CACHE
    if not api_key:
        return []
    try:
        import requests
        r = requests.get(_GEMINI_MODELS_URL, params={"key": api_key,
                                                     "pageSize": 1000}, timeout=8)
        r.raise_for_status()
        out = []
        for m in r.json().get("models", []):
            if "generateContent" not in (m.get("supportedGenerationMethods") or []):
                continue
            name = (m.get("name") or "").split("/")[-1]
            low = name.lower()
            if "gemini" in low and not any(
                    t in low for t in ("embedding", "tts", "image", "audio",
                                       "vision", "live")):
                out.append(name)
        out = sorted(set(out))
        _GEMINI_MODELS_CACHE = out
        return out
    except Exception:
        return []


# ── OpenAI live model picker ──────────────────────────────────────────────────
_OPENAI_MODELS_CACHE = None


def _fetch_openai_models(api_key: str) -> list[str]:
    """Chat-capable OpenAI ids. Their /models returns everything they host,
    so the non-chat families get filtered out."""
    global _OPENAI_MODELS_CACHE
    if _OPENAI_MODELS_CACHE is not None:
        return _OPENAI_MODELS_CACHE
    if not api_key:
        return []
    try:
        import requests
        r = requests.get("https://api.openai.com/v1/models",
                         headers={"Authorization": f"Bearer {api_key}"},
                         timeout=8)
        r.raise_for_status()
        out = sorted(
            m["id"] for m in r.json().get("data", [])
            if m.get("id") and not any(
                t in m["id"] for t in ("embedding", "whisper", "tts", "dall-e",
                                       "image", "audio", "moderation",
                                       "realtime", "transcribe")))
        _OPENAI_MODELS_CACHE = out
        return out
    except Exception:
        return []


# our picks, pinned to the top of a LIVE catalog - never the catalog itself,
# so a model going away or a new one landing needs no code change
_RECOMMENDED = {
    "gemini": {
        "gemini-3.5-flash": "fast, cheap, good at tool use",
        "gemini-3.1-pro-preview": "when you want it to think harder",
    },
    "gemma": {
        "gemma-4-26b-a4b-it": "the free-tier workhorse",
    },
    # same Google client either way, the model id decides. worth knowing:
    # free-tier usage can be used to improve Google's models, paid isn't.
    "google": {
        "gemma-4-26b-a4b-it": "free tier, but Google may train on what you send",
        "gemini-3.5-flash-lite": "paid, cheap, and they don't train on it",
        "gemini-3.5-flash": "paid, sharper, still cheap",
    },
    "anthropic": {
        "claude-sonnet-4-6": "balanced",
        "claude-haiku-4-5-20251001": "cheap and fast, good for Stage 1",
        "claude-opus-4-7": "top tier, pricey",
    },
    "openai": {
        "gpt-5.5": "flagship",
        "gpt-5.4-mini": "fast and cheap",
    },
}


def _live_picker(models: list[str], current: str, label: str,
                 recommended: dict[str, str] | None = None,
                 width: str = "420px"):
    """Searchable picker over a live catalog, with our picks pinned on top and
    a note saying why. Free-text input when the catalog can't be fetched, so a
    missing key never blocks you from typing a model id."""
    current = (current or "").strip()
    if not models:
        return ui.input(label=f"{label} (catalog unavailable - type a model id)",
                        value=current).props("outlined").style(f"width: {width};")
    options: dict[str, str] = {}
    for mid, why in (recommended or {}).items():
        if mid in models:
            options[mid] = f"{mid} - {why}"
    for m in models:
        options.setdefault(m, m)
    if current and current not in options:
        options[current] = current
    if not current:
        current = next(iter(options))
    return ui.select(options, value=current, with_input=True,
                     label=f"{label} (type to search)")\
        .props("outlined").style(f"min-width: {width};")


# ── Anthropic live model picker ───────────────────────────────────────────────
_ANTHROPIC_MODELS_CACHE = None


def _fetch_anthropic_models(api_key: str) -> list[str]:
    """Live model ids from the Anthropic Models API. Returns [] WITHOUT caching
    on missing key or failure, so a later render retries."""
    global _ANTHROPIC_MODELS_CACHE
    if _ANTHROPIC_MODELS_CACHE is not None:
        return _ANTHROPIC_MODELS_CACHE
    if not api_key:
        return []
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        out = sorted(m.id for m in client.models.list())
        _ANTHROPIC_MODELS_CACHE = out  # cache only on success
        return out
    except Exception:
        return []


def _anthropic_model_picker(current_value: str, label: str, api_key: str):
    """Searchable Claude model picker; free-text fallback when the catalog is
    unavailable (no key / fetch failed). Mirrors the Gemma picker."""
    current = (current_value or "claude-haiku-4-5").strip()
    models = _fetch_anthropic_models(api_key)
    if not models:
        return ui.input(
            label=f"{label} (catalog unavailable — type a model id)",
            value=current,
        ).props("outlined").style("width: 420px;")
    options = {m: m for m in models}
    if current not in options:
        options[current] = current
    return ui.select(
        options, value=current, with_input=True, label=f"{label} (type to search)",
    ).props("outlined").style("min-width: 420px;")


def _google_model_picker(current_value: str, label: str, api_key: str):
    """Everything AI Studio serves you, both families: Gemma (free tier) and
    Gemini (paid, and paid means they don't train on your prompts). Same client
    either way - the model id is the only difference."""
    models = sorted(set(_fetch_gemma_models(api_key) + _fetch_gemini_models(api_key)))
    return _live_picker(models, (current_value or "gemma-4-26b-a4b-it").strip(),
                        label, _RECOMMENDED["google"])


def _gemma_model_picker(current_value: str, label: str, api_key: str):
    """Searchable Gemma model picker; falls back to a free-text input when the
    catalog is unavailable (no key / fetch failed). Mirrors the OpenRouter picker."""
    current = (current_value or "gemma-4-26b-a4b-it").strip()
    models = _fetch_gemma_models(api_key)
    if not models:
        return ui.input(
            label=f"{label} (catalog unavailable — type a model id)",
            value=current,
        ).props("outlined").style("width: 420px;")
    options = {m: m for m in models}
    if current not in options:
        options[current] = current
    return ui.select(
        options, value=current, with_input=True, label=f"{label} (type to search)",
    ).props("outlined").style("min-width: 420px;")


# ── Brain 2 backend label (single source of truth) ────────────────────────────
_BACKEND_OPTIONS = {
    "gemini":    "Gemini",
    "gemma":     "Gemma 4 26B (free)",
    "anthropic": "Claude (paid)",
    "openai":    "OpenAI GPT (paid)",
    "openrouter": "OpenRouter",
    "lmstudio":  "LM Studio (local)",
}
_GEMINI_MODELS_PRETTY = {
    "gemini-3.5-flash":       "3.5 Flash",
    "gemini-3.1-pro-preview": "3.1 Pro",
}
_ANTHROPIC_MODELS_PRETTY = {
    "claude-opus-4-7":           "Opus 4.7",
    "claude-sonnet-4-6":         "Sonnet 4.6",
    "claude-haiku-4-5-20251001": "Haiku 4.5",
}


def brain2_backend_label() -> str:
    """Human-readable label for the currently configured Brain 2 backend +
    model, read fresh from config. Shared by the Market Analyzer header and the
    chat tab's 'Backend:' line so they never drift."""
    c = load_config()
    b = c.get("brain2_backend", "gemini")
    label = _BACKEND_OPTIONS.get(b, b)
    if b == "gemini":
        sub = _GEMINI_MODELS_PRETTY.get(
            c.get("brain2_gemini_model", "gemini-3.5-flash"),
            c.get("brain2_gemini_model", ""),
        )
        label = f"{label} {sub}"
    elif b == "anthropic":
        sub = _ANTHROPIC_MODELS_PRETTY.get(
            c.get("brain2_anthropic_model", "claude-sonnet-4-6"),
            c.get("brain2_anthropic_model", ""),
        )
        label = f"{label} — {sub}"
    elif b == "openai":
        label = f"{label} — {c.get('brain2_openai_model', 'gpt-5.5')}"
    elif b == "openrouter":
        label = f"{label} — {c.get('brain2_openrouter_model', 'openrouter/free')}"
    return label


LOG_PATH = Path(__file__).resolve().parent.parent / "hunterjobs.log"


def render_applied_tab():
    container = ui.column().classes("w-full").style("padding: 16px;")

    def refresh():
        container.clear()
        rows = fetch_applied()
        with container:
            with ui.row().style("justify-content: space-between; align-items: center; "
                                "margin-bottom: 8px;"):
                ui.label(f"{len(rows)} application{'s' if len(rows) != 1 else ''}")\
                    .style("color: var(--text-dim); font-size: 12px;")
                ui.button("Refresh", on_click=refresh).classes("btn-ghost")\
                    .style("font-size: 12px;")
            if not rows:
                ui.label("Nothing applied to yet.").style(
                    "color: var(--text-dim); padding: 24px 0; text-align: center;"
                )
                return
            for row in rows:
                render_job_row(row, refresh)

    refresh()
    # No auto-refresh here: it would close expansions the user is reading.


# ══════════════════════════════════════════════════════════════════════════════
# MARKET ANALYZER TAB
# ══════════════════════════════════════════════════════════════════════════════
def render_market_tab():
    with ui.column().classes("w-full").style("padding: 16px; gap: 16px;"):
        with ui.row().style("align-items: center; justify-content: space-between;"):
            ui.html('<div class="section-title" style="margin: 0;">Market Analyzer</div>')
            with ui.row().style("gap: 8px;"):
                wake_btn = ui.button("Wake Brain 2").classes("btn-primary")
                stop_b2_btn = ui.button("Stop").classes("btn-ghost")\
                    .style("color: var(--bad); border-color: var(--bad);")

        def wake():
            s = runner_status.read_status()
            existing_pid = s["brain2"].get("pid")
            if existing_pid and _is_pid_alive(existing_pid):
                ui.notify(
                    f"Brain 2 is already running (pid={existing_pid}). Stop it first.",
                    type="warning",
                )
                return
            if s["brain2"]["state"] == "running":
                runner_status.finish("brain2", error="stale running state cleared")
            spawn_detached("pipeline.run_brain2")
            ui.notify("Brain 2 awakened. Check back in a minute.", type="positive")

        def stop_b2():
            s = runner_status.read_status()
            pid = s["brain2"].get("pid")
            if not pid or not _is_pid_alive(pid):
                ui.notify("Brain 2 is not running.", type="info")
                return
            if kill_pid(pid):
                runner_status.finish("brain2", error="stopped by user")
                ui.notify(f"Brain 2 stopped (pid {pid}).", type="positive")
            else:
                ui.notify(f"Could not stop pid {pid}.", type="negative")

        wake_btn.on("click", lambda _: wake())
        stop_b2_btn.on("click", lambda _: stop_b2())

        # Reflect the configured backend; grounding is gemini-only, so claim it only there.
        market_blurb = ui.html("")

        def refresh_market_blurb():
            grounding = (
                " with Google Search grounding"
                if load_config().get("brain2_backend", "gemini") == "gemini"
                else ""
            )
            market_blurb.set_content(
                '<div style="font-size: 12px; color: var(--text-dim);">'
                f'Powered by {brain2_backend_label()}{grounding}. '
                'Aggregates 7 days of Brain 1 output and produces a strategic '
                'report.</div>'
            )

        refresh_market_blurb()
        ui.timer(3.0, refresh_market_blurb)

        b2_status = ui.html("").style(
            "font-family: 'JetBrains Mono', monospace; font-size: 12px; "
            "color: var(--text-dim);"
        )

        def refresh_b2_status():
            s = runner_status.read_status()["brain2"]
            dot = status_dot_class(s["state"])
            line = (
                f'<span class="status-dot {dot}"></span>'
                f'brain2: {s["state"]}'
            )
            if s["state"] == "running":
                line += f' · {s.get("phase","")}'
            elif s["state"] == "error":
                line += f' · <span style="color: var(--bad);">{s.get("error","")}</span>'
            elif s.get("updated"):
                line += f' · last updated {fmt_ts(s["updated"])}'
            b2_status.set_content(line)

        refresh_b2_status()
        ui.timer(2.0, refresh_b2_status)

        ui.html('<div class="section-title">Last 7 days</div>')
        metrics_row = ui.row().classes("w-full").style("gap: 10px;")

        def refresh_market_metrics():
            cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            conn = get_db_connection()
            try:
                r = conn.execute(
                    "SELECT "
                    "  SUM(CASE WHEN verdict='GOOD' THEN 1 ELSE 0 END) AS good, "
                    "  SUM(CASE WHEN verdict='MAYBE' THEN 1 ELSE 0 END) AS maybe, "
                    "  SUM(CASE WHEN verdict='BAD' AND reject_reason NOT LIKE 'hard_reject%' THEN 1 ELSE 0 END) AS bad, "
                    "  SUM(CASE WHEN reject_reason LIKE 'hard_reject%' THEN 1 ELSE 0 END) AS hr, "
                    "  SUM(CASE WHEN verdict='QUEUED' THEN 1 ELSE 0 END) AS queued, "
                    "  SUM(CASE WHEN hiring_signal='ghost' THEN 1 ELSE 0 END) AS ghost, "
                    "  COUNT(*) AS total "
                    "FROM jobs WHERE date_scraped >= ?",
                    (cutoff,),
                ).fetchone()
            finally:
                conn.close()
            metrics_row.clear()
            with metrics_row:
                for val, lbl in [
                    (r["total"] or 0, "Scraped"),
                    (r["good"] or 0,  "Good"),
                    (r["maybe"] or 0, "Maybe"),
                    (r["bad"] or 0,   "Bad"),
                    (r["hr"] or 0,    "Hard Rej"),
                    (r["queued"] or 0, "Queued"),
                    (r["ghost"] or 0, "Ghost"),
                ]:
                    with ui.element("div").classes("metric").style("flex: 1;"):
                        ui.html(f'<div class="val">{val}</div><div class="lbl">{lbl}</div>')

        refresh_market_metrics()
        ui.timer(5.0, refresh_market_metrics)

        ui.html('<div class="section-title">Strategist Report</div>')
        decree_container = ui.element("div").classes("w-full")

        def refresh_decree():
            conn = get_db_connection()
            try:
                snap = conn.execute(
                    "SELECT * FROM market_snapshots ORDER BY date DESC LIMIT 1"
                ).fetchone()
            finally:
                conn.close()
            decree_container.clear()
            with decree_container:
                if not snap:
                    ui.label(
                        "No snapshot yet. Wake Brain 2 after running Brain 1."
                    ).style("color: var(--text-dim);")
                    return
                ui.html(f'<div class="decree-box">{snap["analysis"]}</div>')
                if snap["targeting_feedback"]:
                    ui.html(
                        f'<div style="font-size: 11px; color: var(--text-faint); '
                        f'margin-top: 8px;">{snap["targeting_feedback"]}</div>'
                    )
                ui.html(
                    f'<div style="font-size: 11px; color: var(--text-faint); '
                    f'margin-top: 6px;">Generated {fmt_ts(snap["date"])}</div>'
                )

        refresh_decree()
        ui.timer(5.0, refresh_decree)

        # ─────────────────────────────────────────────────────────────────────
        # CHAT with Brain 2
        # ─────────────────────────────────────────────────────────────────────
        ui.html('<div class="section-title" style="margin-top: 16px;">Chat with Brain 2</div>')
        ui.html(
            '<div style="font-size: 12px; color: var(--text-dim); margin-bottom: 8px;">'
            'Ask follow-ups about your data. Brain 2 has read-only access to your jobs DB '
            'and remembers the conversation across sessions.</div>'
        )

        cfg = load_config()
        keys = load_keys()

        _current_backend_label = brain2_backend_label

        with ui.row().style("gap: 12px; align-items: center; margin-bottom: 8px; "
                             "flex-wrap: wrap;"):
            backend_label_html = ui.html("")

            def refresh_backend_label():
                backend_label_html.set_content(
                    f'<div style="font-size: 12px; color: var(--text-dim);">'
                    f'Backend: <span class="mono" style="color: var(--text);">'
                    f'{_current_backend_label()}</span> '
                    f'<span style="color: var(--text-faint);">'
                    f'(change in Setup)</span></div>'
                )

            refresh_backend_label()
            # Pick up config changes from Setup tab within ~3s
            ui.timer(3.0, refresh_backend_label)

            def clear_chat():
                with ui.dialog() as dialog, ui.card():
                    ui.html('<div style="font-weight: 600;">Clear chat history?</div>')
                    ui.html(
                        '<div style="font-size: 12px; color: var(--text-dim);">'
                        'Deletes the entire Brain 2 conversation. Cannot be undone.</div>'
                    )
                    with ui.row():
                        def do_clear():
                            brain2_chat.clear_messages()
                            dialog.close()
                            ui.notify("Chat cleared.", type="positive")
                            refresh_chat()
                        ui.button("Yes, clear", on_click=do_clear).classes("btn-primary")\
                            .style("background: var(--bad) !important; "
                                   "border-color: var(--bad) !important;")
                        ui.button("Cancel", on_click=dialog.close).classes("btn-ghost")
                dialog.open()

            ui.button("Clear Chat", on_click=clear_chat).classes("btn-ghost")\
                .style("font-size: 12px;")

        # Pre-flight key checks (read backend from config fresh)
        _b = cfg.get("brain2_backend", "gemini")
        if not keys.get("google") and _b in ("gemini", "gemma"):
            ui.html(
                '<div style="font-size: 12px; color: var(--maybe);">'
                '⚠ GOOGLE_API_KEY not set in keys.py — chat will fail.</div>'
            )
        if not keys.get("anthropic") and _b == "anthropic":
            ui.html(
                '<div style="font-size: 12px; color: var(--maybe);">'
                '⚠ ANTHROPIC_API_KEY not set in keys.py — chat will fail.</div>'
            )
        if not keys.get("openai") and _b == "openai":
            ui.html(
                '<div style="font-size: 12px; color: var(--maybe);">'
                '⚠ OPENAI_API_KEY not set in keys.py — chat will fail.</div>'
            )
        if not keys.get("openrouter") and _b == "openrouter":
            ui.html(
                '<div style="font-size: 12px; color: var(--maybe);">'
                '⚠ OPENROUTER_API_KEY not set in keys.py — chat will fail.</div>'
            )
        if _b == "lmstudio":
            ui.html(
                '<div style="font-size: 12px; color: var(--maybe);">'
                '⚠ Local models under 20B params often hallucinate the tool-call '
                'format (echoing the JSON result back into their text). For best '
                'chat quality, use Gemini or Claude. Snapshot generation works '
                'fine with local models.</div>'
            )

        chat_container = ui.element("div").classes("chat-container")

        def refresh_chat():
            chat_container.clear()
            msgs = brain2_chat.load_messages(include_hidden=False)
            with chat_container:
                if not msgs:
                    ui.html(
                        '<div style="color: var(--text-dim); text-align: center; '
                        'padding: 24px; font-size: 13px;">'
                        'No conversation yet. Ask Brain 2 something below.</div>'
                    )
                    return
                for m in msgs:
                    role = m["role"]
                    content = m["content"] or ""
                    # Skip empty assistant turns (they were tool-call-only)
                    if role == "assistant" and not content and not m.get("tool_calls"):
                        continue
                    if role == "tool":
                        try:
                            parsed = json.loads(content)
                            preview = json.dumps(parsed, indent=2)[:1200]
                        except (json.JSONDecodeError, TypeError):
                            preview = content[:1200]
                        ui.html(
                            f'<div class="chat-msg chat-msg-tool">'
                            f'<div class="chat-msg-meta">tool: {m.get("tool_name","?")}</div>'
                            f'<pre style="margin: 4px 0 0 0; white-space: pre-wrap;">'
                            f'{preview}</pre></div>'
                        )
                        continue
                    cls = "chat-msg-user" if role == "user" else "chat-msg-assistant"
                    safe = content.replace("<", "&lt;").replace(">", "&gt;")
                    ui.html(f'<div class="chat-msg {cls}">{safe}</div>')
            ui.run_javascript(
                "const el = document.querySelector('.chat-container'); "
                "if (el) el.scrollTop = el.scrollHeight;"
            )

        refresh_chat()

        with ui.element("div").classes("chat-input-row"):
            chat_input = ui.textarea(placeholder="Ask Brain 2 about your data...")\
                .props("outlined dense autogrow")\
                .style("flex: 1; min-height: 50px;")
            send_btn = ui.button("Send").classes("btn-primary")\
                .style("flex-shrink: 0;")

        async def send_message():
            text = (chat_input.value or "").strip()
            if not text:
                return
            chat_input.value = ""
            send_btn.props("loading")
            send_btn.disable()
            # Immediately show the user message
            refresh_chat()
            try:
                # Re-read config in case user changed backend in Setup tab
                cur = load_config().get("brain2_backend", "gemini")
                await run_in_thread(
                    brain2_chat.chat_turn, text, cur,
                )
            finally:
                send_btn.enable()
                send_btn.props(remove="loading")
                refresh_chat()

        send_btn.on("click", lambda _: send_message())


# ══════════════════════════════════════════════════════════════════════════════
# LOGS TAB
# ══════════════════════════════════════════════════════════════════════════════
def render_logs_tab():
    with ui.column().classes("w-full").style("padding: 16px; gap: 12px;"):
        with ui.row().style("gap: 8px;"):
            ui.button("Trigger Brain 1",
                      on_click=lambda _: (spawn_detached("pipeline.run_brain1"),
                                          ui.notify("Brain 1 started.")))\
                .classes("btn-ghost")
            ui.button("Wake Brain 2",
                      on_click=lambda _: (spawn_detached("pipeline.run_brain2"),
                                          ui.notify("Brain 2 awakened.")))\
                .classes("btn-ghost")

        status_block = ui.element("div").classes("status-bar").style(
            "flex-direction: column; align-items: flex-start; gap: 8px;"
        )

        def refresh_status_block():
            s = runner_status.read_status()
            status_block.clear()
            with status_block:
                for brain in ("brain1", "brain2"):
                    b = s[brain]
                    dot = status_dot_class(b["state"])
                    line = (
                        f'<span class="status-dot {dot}"></span>'
                        f'<span class="mono">{brain}</span> : '
                        f'{b["state"]}'
                    )
                    if brain == "brain1" and b["state"] == "running":
                        line += (
                            f' · stage1: {b.get("stage1","idle")}'
                            f' · stage2: {b.get("stage2","idle")}'
                            f' · stage3: {b.get("stage3","idle")}'
                        )
                    elif brain == "brain2" and b["state"] == "running":
                        line += f' · phase: {b.get("phase","idle")}'
                    if b.get("error"):
                        line += f' · <span style="color: var(--bad);">{b["error"]}</span>'
                    if b.get("updated"):
                        line += f' · <span style="color: var(--text-faint);">' \
                                f'updated {fmt_ts(b["updated"])}</span>'
                    ui.html(line).style("font-size: 12px; line-height: 1.6;")

        refresh_status_block()
        ui.timer(2.0, refresh_status_block)

        # Log tail
        ui.html('<div class="section-title">hunterjobs.log (tail)</div>')
        log_container = ui.element("div").classes("card").style(
            "max-height: 60vh; overflow-y: auto;"
        )

        def refresh_logs():
            log_container.clear()
            with log_container:
                if not LOG_PATH.exists():
                    ui.label("No log file yet. Run Brain 1 first.").style(
                        "color: var(--text-dim);"
                    )
                    return
                try:
                    lines = LOG_PATH.read_text(encoding="utf-8", errors="replace") \
                                    .splitlines()[-200:]
                except OSError:
                    ui.label("Could not read log file.").style("color: var(--bad);")
                    return
                html_parts = []
                for line in reversed(lines):
                    cls = "log-line"
                    low = line.lower()
                    if "error" in low or "failed" in low or "crash" in low:
                        cls = "log-line log-err"
                    elif "warn" in low:
                        cls = "log-line log-warn"
                    elif any(w in low for w in ("complete", "done", "good", "ready", "started")):
                        cls = "log-line log-ok"
                    safe = line.replace("<", "&lt;").replace(">", "&gt;")
                    html_parts.append(f'<div class="{cls}">{safe}</div>')
                ui.html("".join(html_parts))

        refresh_logs()
        ui.timer(3.0, refresh_logs)


# ══════════════════════════════════════════════════════════════════════════════
# SETUP TAB
# ══════════════════════════════════════════════════════════════════════════════
def render_setup_tab():
    cfg = load_config()
    keys = load_keys()

    with ui.column().classes("w-full").style("padding: 16px; gap: 18px; max-width: 900px;"):
        ui.html('<div class="section-title">Appearance</div>')
        with ui.row().style("gap: 10px; align-items: center;"):
            def on_theme_change(e):
                # Read from the select directly: NiceGUI v2 varies where the
                # value lands (e.value vs e.args) depending on how it's wired.
                new_theme = theme_select.value
                cfg["theme"] = new_theme
                save_config(cfg)
                ui.run_javascript(
                    f"document.documentElement.setAttribute('data-theme','{new_theme}');"
                )
                ui.notify(f"Theme: {new_theme}", type="positive")

            theme_select = ui.select(
                {"dark": "Dark (default)", "light": "Light"},
                value=cfg["theme"],
                on_change=on_theme_change,
            ).style("min-width: 220px;")

        ui.html('<div class="section-title">Free company research</div>')
        ui.html('<div style="font-size:12px; color:var(--text-dim); '
                'margin-bottom:8px;">Pull down the companies already '
                'researched on hunterjobsats.com - what they build, their '
                'stack, hiring signal, staffing-agency flags. Skips hundreds '
                'of LLM calls on your first scans. No contacts come with it; '
                'those stay yours to hunt on your own keys. It lands in its '
                'own table, so nothing you researched is overwritten.</div>')
        seed_status = ui.label("").style("font-size:12px; color:var(--text-dim);")

        async def _parse_server_db():
            from ui.helpers import run_in_thread
            import scripts.import_seed as imp
            seed_btn.props("loading")
            seed_status.set_text("downloading…")
            try:
                path = await run_in_thread(imp._fetch, imp.SEED_URL)
                c = await run_in_thread(imp.import_seed, path, False)
                seed_status.set_text(
                    f"{c['total']} companies on hand - {c['new']} new, "
                    f"{c['updated']} updated this time")
                ui.notify(f"Imported: {c['new']} new, {c['updated']} updated.",
                          type="positive")
            except Exception as e:
                seed_status.set_text(f"failed: {e}")
                ui.notify(f"Import failed: {e}", type="negative")
            finally:
                seed_btn.props(remove="loading")

        seed_btn = ui.button("Parse server DB", on_click=_parse_server_db)\
            .props("unelevated")\
            .style("background:#ff9540 !important; color:#1b1b22; "
                   "font-weight:600;")

        ui.html('<div class="section-title">API Keys</div>')
        ui.html(
            '<div style="font-size: 12px; color: var(--text-dim); margin-bottom: 8px;">'
            'Edit <span class="mono">keys.py</span> in the project root. '
            'It is gitignored and never sent to the dashboard.</div>'
        )
        with ui.element("div").classes("card card-tight"):
            ui.html(
                f'<div class="mono" style="font-size: 12px; line-height: 1.8;">'
                f'GOOGLE_API_KEY    = "{"*" * 20 if keys["google"] else "(not set)"}"'
                f'<br>ANTHROPIC_API_KEY = "{"*" * 20 if keys.get("anthropic") else "(not set, optional)"}"'
                f'<br>OPENAI_API_KEY    = "{"*" * 20 if keys.get("openai") else "(not set, optional)"}"'
                f'<br>OPENROUTER_API_KEY = "{"*" * 20 if keys.get("openrouter") else "(not set, optional)"}"'
                f'<br>GITHUB_PAT        = "{"*" * 20 if keys["github"] else "(not set, optional)"}"'
                f'</div>'
            )
        if not keys["google"]:
            ui.html(
                '<div style="font-size: 12px; color: var(--maybe);">'
                'GOOGLE_API_KEY is not set. Brain 1 and Brain 2 (Gemini/Gemma) will fail until you add it.</div>'
            )

        ui.html('<div class="section-title">Candidate Profile</div>')
        ui.html(
            '<div style="font-size: 12px; color: var(--text-dim); margin-bottom: 8px;">'
            'The more specific, the sharper Brain 1 filters. '
            'Include target salary, stack, geo constraints, and what you want to exclude.</div>'
        )
        profile_ta = ui.textarea(
            value=cfg["profile"],
            placeholder="Senior backend engineer, 6 yrs Python/Go, distributed systems. "
                        "Target $120k+ remote. Strong on infra, weak on frontend. "
                        "No crypto, no ad-tech.")\
            .props("outlined autogrow")\
            .style("width: 100%; font-family: 'JetBrains Mono', monospace; font-size: 12.5px;")

        ui.html('<div class="section-title">Evaluation Brief (Stage 1 judge)</div>')
        ui.html(
            '<div style="font-size: 12px; color: var(--text-dim); margin-bottom: 8px;">'
            'What the judge is hunting for. Rewrite it freely — HJ is a listing '
            'analyzer, job filtering is just the default mission. The GOOD/MAYBE/BAD '
            'output contract stays fixed no matter what you write here.</div>'
        )
        from pipeline.brain1 import DEFAULT_JUDGE_PROMPT
        judge_ta = ui.textarea(
            value=cfg.get("judge_prompt") or DEFAULT_JUDGE_PROMPT)\
            .props("outlined autogrow")\
            .style("width: 100%; font-family: 'JetBrains Mono', monospace; font-size: 12.5px;")

        def _restore_judge_default():
            judge_ta.set_value(DEFAULT_JUDGE_PROMPT)
            safe_notify("Default brief restored — hit Save to keep it.")

        ui.button("Restore default brief", on_click=_restore_judge_default)\
            .classes("btn-ghost").style("font-size: 12px;")

        ui.html('<div class="section-title">Geo-Eligibility</div>')
        ui.html(
            '<div style="font-size: 12px; color: var(--text-dim); margin-bottom: 8px;">'
            'Where you can legally work. Leave empty to skip geo filtering entirely. '
            'Format: base country, passport, work authorization, sponsorship/relocation '
            'stance, remote scope, timezone.</div>'
        )
        geo_ta = ui.textarea(
            value=cfg.get("geo_eligibility", ""),
            placeholder="EU citizen based in Lisbon. Can work anywhere in the EU, "
                        "remote-global OK, no US work authorization, UTC+1.")\
            .props("outlined autogrow")\
            .style("width: 100%; font-family: 'JetBrains Mono', monospace; font-size: 12.5px;")

        ui.html('<div class="section-title">Search Terms</div>')
        ui.html(
            '<div style="font-size: 12px; color: var(--text-dim); margin-bottom: 8px;">'
            'One per line. Each becomes a JobSpy search.</div>'
        )
        terms_ta = ui.textarea(
            value=cfg["search_terms"],
            placeholder="machine learning engineer\nbackend engineer fintech\n"
                        "senior data engineer")\
            .props("outlined autogrow")\
            .style("width: 100%; font-family: 'JetBrains Mono', monospace; font-size: 12.5px;")

        ui.html('<div class="section-title">Blacklist &amp; Suspects</div>')
        with ui.row().style("gap: 24px; width: 100%; align-items: stretch; "
                            "flex-wrap: nowrap;"):
            # ── Blacklist (active / manual) ─────────────────────────────────
            with ui.column().style("flex: 1; min-width: 0; gap: 4px;"):
                ui.html('<div style="font-weight: 600; font-size: 13px;">'
                        'Blacklist <span style="color: var(--text-dim); '
                        'font-weight: 400;">(active)</span></div>')
                ui.html(
                    '<div style="font-size: 12px; color: var(--text-dim); margin-bottom: 8px;">'
                    'Substring match against title + company + description. Zero API cost. '
                    'Use Export/Import to share blacklists with others.</div>'
                )
                rejects_ta = ui.textarea(
                    value=cfg["hard_rejects"],
                    placeholder="staffing agency\nsecurity clearance\nunpaid\ninternship")\
                    .props("outlined autogrow")\
                    .style("width: 100%; font-family: 'JetBrains Mono', monospace; font-size: 12.5px;")

                with ui.row().style("gap: 8px; margin-top: 8px;"):
                    def export_rejects():
                        lines = [l.strip() for l in (rejects_ta.value or "").splitlines() if l.strip()]
                        exported = datetime.now(timezone.utc).isoformat(timespec="seconds")
                        content = (
                            f"# HunterJobs blacklist\n"
                            f"# Exported: {exported}\n"
                            f"# Entries: {len(lines)}\n"
                            f"# One keyword/phrase per line. Lines starting with # are comments.\n"
                            f"#\n"
                            + "\n".join(lines)
                            + "\n"
                        )
                        ui.run_javascript(
                            "const blob = new Blob([" + json.dumps(content) + "], "
                            "{type: 'text/plain'});"
                            "const url = URL.createObjectURL(blob);"
                            "const a = document.createElement('a'); a.href = url;"
                            "a.download = 'hunterjobs_blacklist.txt';"
                            "a.click(); URL.revokeObjectURL(url);"
                        )
                        ui.notify(f"Exported {len(lines)} entries.", type="positive")

                    ui.button("Export Blacklist", on_click=export_rejects).classes("btn-ghost")\
                        .style("font-size: 12px;")

                    def open_import():
                        with ui.dialog() as dialog, ui.card():
                            ui.html('<div style="font-weight: 600;">Import Blacklist</div>')
                            ui.html(
                                '<div style="font-size: 12px; color: var(--text-dim); '
                                'margin-bottom: 8px;">'
                                'Upload a hunterjobs_blacklist.txt file (or any .txt with '
                                'one keyword per line). Lines starting with # are ignored. '
                                'Entries are merged with your current list (no duplicates).</div>'
                            )

                            def handle_upload(e):
                                # NiceGUI's upload event shape varies across versions, so
                                # probe each known attribute in turn.
                                raw = None
                                try:
                                    for attr in ("content", "file", "data"):
                                        obj = getattr(e, attr, None)
                                        if obj is None:
                                            continue
                                        # obj could be file-like, bytes, or string
                                        if hasattr(obj, "read"):
                                            try:
                                                obj.seek(0)
                                            except Exception:
                                                pass
                                            raw = obj.read()
                                            break
                                        if isinstance(obj, (bytes, bytearray)):
                                            raw = obj
                                            break
                                        if isinstance(obj, str):
                                            raw = obj
                                            break
                                    # Last resort: check e.args (some versions)
                                    if raw is None and hasattr(e, "args"):
                                        args = e.args
                                        if isinstance(args, dict):
                                            raw = args.get("content") or args.get("file") or args.get("data")
                                        else:
                                            raw = args
                                    if raw is None:
                                        raise ValueError(
                                            f"could not extract content from upload event "
                                            f"(attrs: {[a for a in dir(e) if not a.startswith('_')]})"
                                        )
                                    if isinstance(raw, (bytes, bytearray)):
                                        raw = raw.decode("utf-8", errors="replace")
                                    if not raw or not raw.strip():
                                        raise ValueError("empty file")
                                except Exception as ex:
                                    ui.notify(f"Bad file: {ex}", type="negative")
                                    return

                                entries = [
                                    line.strip()
                                    for line in raw.splitlines()
                                    if line.strip() and not line.strip().startswith("#")
                                ]
                                current = {
                                    l.strip().lower(): l.strip()
                                    for l in (rejects_ta.value or "").splitlines()
                                    if l.strip()
                                }
                                added = 0
                                for entry in entries:
                                    if entry.lower() not in current:
                                        current[entry.lower()] = entry
                                        added += 1
                                rejects_ta.value = "\n".join(current.values())
                                dialog.close()
                                render_suspects()  # imported entries may cover suspects
                                ui.notify(
                                    f"Imported {added} new entries "
                                    f"(of {len(entries)} in file).",
                                    type="positive",
                                )

                            ui.upload(
                                label="Choose .txt file",
                                on_upload=handle_upload,
                                auto_upload=True,
                            ).props("accept=.txt").style("width: 100%;")

                            with ui.row().style("gap: 8px; margin-top: 8px;"):
                                ui.button("Close", on_click=dialog.close).classes("btn-ghost")
                        dialog.open()

                    ui.button("Import Blacklist", on_click=open_import).classes("btn-ghost")\
                        .style("font-size: 12px;")

            # ── Suspects (Stage 2 flags + manual) ───────────────────────────
            with ui.column().style("flex: 1; min-width: 0; gap: 4px;"):
                ui.html('<div style="font-weight: 600; font-size: 13px;">'
                        'Suspects <span style="color: var(--text-dim); '
                        'font-weight: 400;">(flagged + your own)</span></div>')
                ui.html(
                    '<div style="font-size: 12px; color: var(--text-dim); margin-bottom: 8px;">'
                    'Companies the pipeline demoted as staffing/recruiting agencies, plus '
                    'any you add yourself. Nothing is blacklisted automatically — promote '
                    'the real agencies, dismiss or remove the rest.</div>'
                )

                with ui.row().style("gap: 6px; width: 100%; margin-bottom: 6px; "
                                    "align-items: center;"):
                    add_in = ui.input(placeholder="Add a company you suspect…")\
                        .props("outlined dense").style("flex: 1; min-width: 0;")

                    def do_add_suspect():
                        if add_manual_suspect(add_in.value):
                            add_in.value = ""

                    add_in.on("keydown.enter", lambda _: do_add_suspect())
                    ui.button(icon="add", on_click=lambda: do_add_suspect())\
                        .props("flat dense round size=sm").tooltip("Add to Suspects")

                suspects_box = ui.column().style("gap: 6px; width: 100%;")

                def render_suspects():
                    suspects_box.clear()
                    # Live textarea is the source of truth so promotes drop off at once.
                    blacklisted = {l.strip().lower()
                                   for l in (rejects_ta.value or "").splitlines() if l.strip()}
                    dismissed = {d.strip().lower()
                                 for d in cfg.get("dismissed_suspects", []) if d.strip()}
                    rows = []           # (name, badge, kind)
                    seen = set()

                    def _hidden(low):
                        return not low or any(b in low for b in blacklisted)

                    for r in fetch_agency_suspects():
                        name = (r.get("company") or "").strip()
                        low = name.lower()
                        if low in dismissed or low in seen or _hidden(low):
                            continue
                        seen.add(low)
                        rows.append((name, f"×{r.get('hits', 0)}", "auto"))
                    for name in cfg.get("manual_suspects", []):
                        name = (name or "").strip()
                        low = name.lower()
                        if low in seen or _hidden(low):
                            continue
                        seen.add(low)
                        rows.append((name, "added", "manual"))

                    with suspects_box:
                        if not rows:
                            ui.html(
                                '<div style="font-size: 12px; color: var(--text-dim);">'
                                'No suspects yet — agencies the pipeline flags appear here, '
                                'or add your own above.</div>'
                            )
                            return
                        for name, badge, kind in rows:
                            with ui.row().style("align-items: center; gap: 6px; width: 100%; "
                                                "justify-content: space-between;"):
                                ui.html(
                                    f'<div style="font-size: 12.5px; font-family: '
                                    f"'JetBrains Mono', monospace; overflow: hidden; "
                                    f'text-overflow: ellipsis; white-space: nowrap;">'
                                    f'{html.escape(name)} '
                                    f'<span style="color: var(--text-dim);">{badge}</span></div>'
                                )
                                with ui.row().style("gap: 2px; flex: none;"):
                                    ui.button(icon="block",
                                              on_click=lambda c=name: promote_suspect(c))\
                                        .props("flat dense round size=sm")\
                                        .tooltip("Add to Blacklist")
                                    if kind == "manual":
                                        ui.button(icon="edit",
                                                  on_click=lambda c=name: edit_manual_suspect(c))\
                                            .props("flat dense round size=sm")\
                                            .tooltip("Edit")
                                        ui.button(icon="delete",
                                                  on_click=lambda c=name: remove_manual_suspect(c))\
                                            .props("flat dense round size=sm")\
                                            .tooltip("Remove")
                                    else:
                                        ui.button(icon="close",
                                                  on_click=lambda c=name: dismiss_suspect(c))\
                                            .props("flat dense round size=sm")\
                                            .tooltip("Not an agency — dismiss")

                def rerender_suspects():
                    # Defer to the next tick: promote/dismiss buttons live inside
                    # suspects_box, so re-rendering synchronously would delete the very
                    # element whose click is still being handled (NiceGUI "parent slot
                    # deleted" RuntimeError).
                    ui.timer(0.05, render_suspects, once=True)

                def add_manual_suspect(name: str) -> bool:
                    name = (name or "").strip()
                    if not name:
                        return False
                    m = list(cfg.get("manual_suspects", []))
                    if name.lower() in {x.strip().lower() for x in m}:
                        safe_notify(f"'{name}' is already a suspect.", type="info")
                        return False
                    m.append(name)
                    cfg["manual_suspects"] = m
                    save_config(cfg)
                    rerender_suspects()
                    safe_notify(f"Added '{name}' to Suspects.", type="positive")
                    return True

                def remove_manual_suspect(name: str):
                    cfg["manual_suspects"] = [
                        x for x in cfg.get("manual_suspects", [])
                        if x.strip().lower() != name.strip().lower()
                    ]
                    save_config(cfg)
                    rerender_suspects()
                    safe_notify(f"Removed '{name}'.", type="info")

                def edit_manual_suspect(old: str):
                    with ui.dialog() as dialog, ui.card():
                        ui.html('<div style="font-weight: 600;">Edit suspect</div>')
                        inp = ui.input(value=old).props("outlined dense")\
                            .style("width: 260px;")

                        def save_edit():
                            new = (inp.value or "").strip()
                            m = [x for x in cfg.get("manual_suspects", [])
                                 if x.strip().lower() != old.strip().lower()]
                            if new and new.lower() not in {x.strip().lower() for x in m}:
                                m.append(new)
                            cfg["manual_suspects"] = m
                            save_config(cfg)
                            dialog.close()
                            rerender_suspects()

                        with ui.row().style("gap: 8px; margin-top: 8px;"):
                            ui.button("Save", on_click=save_edit).classes("btn-primary")\
                                .style("font-size: 12px;")
                            ui.button("Cancel", on_click=dialog.close).classes("btn-ghost")\
                                .style("font-size: 12px;")
                    dialog.open()

                def promote_suspect(company: str):
                    existing = {l.strip().lower()
                                for l in (rejects_ta.value or "").splitlines() if l.strip()}
                    if company.lower() not in existing:
                        cur = rejects_ta.value or ""
                        rejects_ta.value = (cur + ("\n" if cur and not cur.endswith("\n") else "")
                                            + company)
                    cfg["hard_rejects"] = rejects_ta.value
                    # A promoted manual suspect has served its purpose — drop it.
                    cfg["manual_suspects"] = [x for x in cfg.get("manual_suspects", [])
                                              if x.strip().lower() != company.lower()]
                    save_config(cfg)
                    rerender_suspects()
                    safe_notify(f"'{company}' added to Blacklist.", type="positive")

                def dismiss_suspect(company: str):
                    d = list(cfg.get("dismissed_suspects", []))
                    if company not in d:
                        d.append(company)
                    cfg["dismissed_suspects"] = d
                    save_config(cfg)
                    rerender_suspects()
                    safe_notify(f"Dismissed '{company}'.", type="info")

                render_suspects()

        ui.html('<div class="section-title">Scrape Settings</div>')
        with ui.row().style("gap: 14px; flex-wrap: wrap;"):
            floor_in = ui.number(label="Salary floor (USD/month)",
                                 value=cfg["salary_floor"], step=100)\
                .props("outlined").style("width: 220px;")
            rw_in = ui.number(label="Results per term",
                              value=cfg["results_wanted"], step=10, min=10, max=200)\
                .props("outlined").style("width: 200px;")
            rw_in.tooltip("LinkedIn/Indeed only: listings fetched per search term. "
                          "No effect on YC (whole companies) or HN (whole thread).")
            ho_in = ui.number(label="Max hours old",
                              value=cfg["hours_old"], step=12, min=12)\
                .props("outlined").style("width: 180px;")
            ho_in.tooltip("Freshness window for LinkedIn/Indeed/HN real dates. "
                          "YC has its own window; estimated dates are ledger-governed.")
            cap_in = ui.number(label="LLM jobs per scan (0 = no cap)",
                               value=cfg.get("max_llm_jobs_per_scan", 100),
                               step=10, min=0)\
                .props("outlined").style("width: 220px;")
            expire_in = ui.number(label="Listing expiry, days",
                                  value=cfg.get("ledger_expire_days", 60),
                                  step=10, min=0)\
                .props("outlined").style("width: 170px;")
            expire_in.tooltip("A job unseen on its board for this many days is "
                              "marked dead (probably filled). 0 = never.")
            ttl_in = ui.number(label="Company cache, days",
                               value=cfg.get("company_ttl_days", 30),
                               step=5, min=0)\
                .props("outlined").style("width: 170px;")
            ttl_in.tooltip("How long company research + contacts stay valid before "
                           "a scan re-researches them. 0 = keep forever.")

        ui.html('<div class="section-title">Sources</div>')
        sources_set = set(cfg["sources"])
        with ui.row().style("gap: 14px; align-items: center;"):
            linkedin_cb = ui.checkbox("LinkedIn", value=("linkedin" in sources_set))
            # jobspy's Indeed scraper came back empty across 21 broad terms,
            # so it's off and greyed rather than quietly returning nothing
            indeed_cb = ui.checkbox("Indeed", value=False)
            indeed_cb.disable()
            indeed_cb.tooltip("Disabled: the Indeed scraper isn't returning "
                              "results at the moment.")
            indeed_cb.style("opacity: .45;")
            # YC startups are company-based, scraped separately from JobSpy sites.
            yc_cb = ui.checkbox("Y Combinator startups", value=bool(cfg.get("use_yc")))
            yc_remote_cb = ui.checkbox("YC remote only",
                                       value=bool(cfg.get("yc_remote_only", True)))
            yc_team_in = ui.number(label="YC max team size (0 = any)",
                                   value=cfg.get("yc_max_team_size", 50),
                                   step=10, min=0, max=1000)\
                .props("outlined dense").style("width: 180px;")
            yc_comp_in = ui.number(label="YC max companies (0 = all)",
                                   value=cfg.get("yc_max_companies", 100),
                                   step=50, min=0, max=2000)\
                .props("outlined dense").style("width: 190px;")
            yc_ho_in = ui.number(label="YC max hours old",
                                 value=cfg.get("yc_hours_old", 720),
                                 step=24, min=24, max=2160)\
                .props("outlined dense").style("width: 160px;")
            # Hacker News "Who is hiring?" — single monthly thread, free APIs.
            hn_cb = ui.checkbox("Hacker News (Who is hiring?)",
                                value=bool(cfg.get("use_hn")))
            hn_remote_cb = ui.checkbox("HN remote only",
                                       value=bool(cfg.get("hn_remote_only", True)))

        ui.html('<div class="section-title">Brain 1 — Stage 1 Backend (job filter, high volume)</div>')
        ui.html(
            '<div style="font-size: 12px; color: var(--text-dim); margin-bottom: 8px;">'
            'Stage 1 runs once per scraped listing. Local LM Studio with an 8B model '
            'works great here — fast and free.</div>'
        )
        # Prefer per-stage backend, fall back to the legacy single key.
        s1_current = cfg.get("brain1_stage1_backend") or cfg.get("brain1_backend", "gemma")
        b1s1_select = ui.select(
            {"gemma": "Google AI Studio (Gemma free, Gemini paid)",
             "anthropic": "Claude (Anthropic API, paid)",
             "openrouter": "OpenRouter (free + paid, OpenAI-compatible)",
             "lmstudio": "LM Studio (local)"},
            value=s1_current,
        ).style("min-width: 320px;")
        with ui.column().style("gap: 8px;") as b1_gemma_s1_box:
            b1_s1_gemma_model = _google_model_picker(
                cfg.get("brain1_stage1_gemma_model", "gemma-4-26b-a4b-it"),
                "Stage 1 model", keys.get("google", ""),
            )
        with ui.column().style("gap: 8px;") as b1_or_s1_box:
            b1_s1_or_model = _openrouter_model_picker(
                cfg.get("brain1_stage1_openrouter_model")
                or cfg.get("brain1_openrouter_model", "openrouter/free"),
                "Stage 1 OpenRouter model",
            )
        with ui.column().style("gap: 8px;") as b1_ant_s1_box:
            b1_s1_ant_model = _anthropic_model_picker(
                cfg.get("brain1_stage1_anthropic_model")
                or cfg.get("brain1_anthropic_model", "claude-haiku-4-5"),
                "Stage 1 Claude model", keys.get("anthropic", ""),
            )
        with ui.column().style("gap: 8px;") as b1_lms_s1_box:
            b1_s1_lms_model = ui.input(
                label="Stage 1 LM Studio model (blank = auto-detect)",
                value=cfg.get("brain1_stage1_lmstudio_model")
                or cfg.get("brain1_lmstudio_model", ""))\
                .props("outlined dense").style("width: 360px;")

        ui.html('<div class="section-title">Brain 1 — Enrichment Backend (company research + outreach)</div>')
        ui.html(
            '<div style="font-size: 12px; color: var(--text-dim); margin-bottom: 8px;">'
            'Enrichment only runs on GOOD jobs (low volume) and needs solid '
            'instruction-following. Gemma 4 is fine here; pick a Gemini model '
            'if you would rather Google did not train on what you send.</div>'
        )
        s23_current = cfg.get("brain1_stage23_backend") or cfg.get("brain1_backend", "gemma")
        b1s23_select = ui.select(
            {"gemma": "Google AI Studio (Gemma free, Gemini paid)",
             "anthropic": "Claude (Anthropic API, paid)",
             "openrouter": "OpenRouter (free + paid, OpenAI-compatible)",
             "lmstudio": "LM Studio (local)"},
            value=s23_current,
        ).style("min-width: 320px;")
        with ui.column().style("gap: 8px;") as b1_gemma_s23_box:
            b1_s2_gemma_model = _google_model_picker(
                cfg.get("brain1_stage2_gemma_model", "gemma-4-26b-a4b-it"),
                "Research model (company intel)", keys.get("google", ""),
            )
            b1_s3_gemma_model = _google_model_picker(
                cfg.get("brain1_stage3_gemma_model", "gemma-4-26b-a4b-it"),
                "Outreach model (contacts + drafts)", keys.get("google", ""),
            )
        with ui.column().style("gap: 8px;") as b1_or_s23_box:
            b1_s23_or_model = _openrouter_model_picker(
                cfg.get("brain1_stage23_openrouter_model")
                or cfg.get("brain1_openrouter_model", "openrouter/free"),
                "Enrichment OpenRouter model",
            )
        with ui.column().style("gap: 8px;") as b1_ant_s23_box:
            b1_s23_ant_model = _anthropic_model_picker(
                cfg.get("brain1_stage23_anthropic_model")
                or cfg.get("brain1_anthropic_model", "claude-sonnet-4-6"),
                "Enrichment Claude model", keys.get("anthropic", ""),
            )
        with ui.column().style("gap: 8px;") as b1_lms_s23_box:
            b1_s23_lms_model = ui.input(
                label="Enrichment LM Studio model (blank = auto-detect)",
                value=cfg.get("brain1_stage23_lmstudio_model")
                or cfg.get("brain1_lmstudio_model", ""))\
                .props("outlined dense").style("width: 360px;")

        with ui.column().style("gap: 8px;") as b1_lmstudio_box:
            # one server, so the URL is shared; the models are picked per stage
            b1_url = ui.input(label="LM Studio URL",
                              value=cfg["brain1_lmstudio_url"])\
                .props("outlined").style("width: 360px;")

        def _refresh_b1_backend_boxes():
            s1, s23 = b1s1_select.value, b1s23_select.value
            # each picker sits under the stage that chose it, not at the bottom
            for box, want in ((b1_gemma_s1_box, "gemma"), (b1_or_s1_box, "openrouter"),
                              (b1_ant_s1_box, "anthropic"), (b1_lms_s1_box, "lmstudio")):
                box.set_visibility(s1 == want)
            for box, want in ((b1_gemma_s23_box, "gemma"), (b1_or_s23_box, "openrouter"),
                              (b1_ant_s23_box, "anthropic"), (b1_lms_s23_box, "lmstudio")):
                box.set_visibility(s23 == want)
            b1_lmstudio_box.set_visibility("lmstudio" in (s1, s23))

        _refresh_b1_backend_boxes()
        b1s1_select.on("update:model-value", lambda _e: _refresh_b1_backend_boxes())
        b1s23_select.on("update:model-value", lambda _e: _refresh_b1_backend_boxes())

        ui.html('<div class="section-title">Brain 2 Backend</div>')
        ui.html(
            '<div style="font-size: 12px; color: var(--text-dim); margin-bottom: 8px;">'
            'Drives the periodic snapshot AND the chat. Gemini default for web '
            'grounding. Gemma is free but no search. Claude is paid, strong reasoning. '
            'LM Studio is fully local.</div>'
        )
        b2_select = ui.select(
            {
                "gemini":    "Gemini (paid, web search, recommended)",
                "gemma":     "Gemma 4 26B (free, no web search)",
                "anthropic": "Anthropic Claude (paid)",
                "openai":    "OpenAI GPT (paid)",
                "openrouter": "OpenRouter (free + paid, no web search)",
                "lmstudio":  "LM Studio (local, no web search)",
            },
            value=cfg["brain2_backend"],
        ).style("min-width: 360px;")

        # Containers wrap each model-field group so we can show/hide them.
        with ui.column().style("gap: 8px; margin-top: 8px;") as b2_gemini_box:
            b2_gem_model = _live_picker(
                _fetch_gemini_models(keys["google"]),
                cfg.get("brain2_gemini_model", "gemini-3.5-flash"),
                "Gemini model", _RECOMMENDED["gemini"])
        with ui.column().style("gap: 8px; margin-top: 8px;") as b2_gemma_box:
            b2_gemma_model = _google_model_picker(
                cfg.get("brain2_gemma_model", "gemma-4-26b-a4b-it"),
                "Google model", keys["google"])
        with ui.column().style("gap: 8px; margin-top: 8px;") as b2_anthropic_box:
            b2_anthropic_model = _live_picker(
                _fetch_anthropic_models(keys["anthropic"]),
                cfg.get("brain2_anthropic_model", "claude-sonnet-4-6"),
                "Claude model", _RECOMMENDED["anthropic"])
        with ui.column().style("gap: 8px; margin-top: 8px;") as b2_openai_box:
            ui.html('<div style="font-size: 12px; color: var(--text-dim);">'
                    'Needs OPENAI_API_KEY in keys.py.</div>')
            b2_openai_model = _live_picker(
                _fetch_openai_models(keys["openai"]),
                cfg.get("brain2_openai_model", "gpt-5.5"),
                "OpenAI model", _RECOMMENDED["openai"])
        with ui.column().style("gap: 8px; margin-top: 8px;") as b2_openrouter_box:
            ui.html(
                '<div style="font-size: 12px; color: var(--text-dim);">'
                'Needs OPENROUTER_API_KEY in keys.py.</div>'
            )
            b2_openrouter_model = _openrouter_model_picker(
                cfg.get("brain2_openrouter_model", "openrouter/free"),
                "OpenRouter model",
            )
        with ui.column().style("gap: 8px; margin-top: 8px;") as b2_lmstudio_box:
            b2_url = ui.input(label="LM Studio URL",
                              value=cfg["brain2_lmstudio_url"])\
                .props("outlined").style("width: 360px;")
            b2_model = ui.input(label="LM Studio model name (blank = auto-detect)",
                                value=cfg["brain2_lmstudio_model"])\
                .props("outlined").style("width: 360px;")

        def _refresh_b2_visibility():
            sel = b2_select.value
            b2_gemini_box.set_visibility(sel == "gemini")
            b2_gemma_box.set_visibility(sel == "gemma")
            b2_anthropic_box.set_visibility(sel == "anthropic")
            b2_openai_box.set_visibility(sel == "openai")
            b2_openrouter_box.set_visibility(sel == "openrouter")
            b2_lmstudio_box.set_visibility(sel == "lmstudio")

        _refresh_b2_visibility()
        b2_select.on("update:model-value", lambda _e: _refresh_b2_visibility())

        # Persona applies to every backend, so it lives outside the per-backend boxes.
        with ui.expansion("Brain 2 Persona / Behavior", icon="theater_comedy")\
                .classes("w-full").style("margin-top: 12px;"):
            ui.html(
                '<div style="font-size: 12px; color: var(--text-dim); margin-bottom: 8px;">'
                "Shape Brain 2's voice and behavior (snapshot + chat). "
                'Leave blank for default. Styling only — does not change tool access '
                'or analysis.</div>'
            )
            b2_persona_ta = ui.textarea(value=cfg.get("brain2_persona", ""))\
                .props("outlined autogrow")\
                .style("width: 100%; font-family: 'JetBrains Mono', monospace; font-size: 12.5px;")

        def do_save():
            sources = []
            if linkedin_cb.value: sources.append("linkedin")
            if indeed_cb.value: sources.append("indeed")
            new_cfg = {
                **cfg,
                "use_yc": bool(yc_cb.value),
                "yc_remote_only": bool(yc_remote_cb.value),
                "yc_max_team_size": int(yc_team_in.value or 0),
                "yc_max_companies": int(yc_comp_in.value or 0),
                "yc_hours_old": int(yc_ho_in.value or 720),
                "use_hn": bool(hn_cb.value),
                "hn_remote_only": bool(hn_remote_cb.value),
                "theme": theme_select.value,
                "profile": profile_ta.value,
                # default brief saved as "" so future default improvements propagate
                "judge_prompt": ("" if (judge_ta.value or "").strip() == DEFAULT_JUDGE_PROMPT
                                 else (judge_ta.value or "").strip()),
                "geo_eligibility": geo_ta.value,
                "search_terms": terms_ta.value,
                "hard_rejects": rejects_ta.value,
                "salary_floor": int(floor_in.value or 0),
                "results_wanted": int(rw_in.value or 100),
                "hours_old": int(ho_in.value or 72),
                # `or 0` not 100: 0 is a legit "no cap" choice here
                "max_llm_jobs_per_scan": int(cap_in.value or 0),
                "ledger_expire_days": int(expire_in.value or 0),
                "company_ttl_days": int(ttl_in.value or 0),
                "use_rag": bool(rag_cb.value),
                # Save the actual ticked list — an empty list is allowed (YC-only run).
                # Do NOT coerce back to ["linkedin"]; that silently forces LinkedIn on.
                "sources": sources,
                "brain1_stage1_backend": b1s1_select.value,
                "brain1_stage23_backend": b1s23_select.value,
                "brain1_stage1_gemma_model": (b1_s1_gemma_model.value or "gemma-4-26b-a4b-it").strip(),
                "brain1_stage2_gemma_model": (b1_s2_gemma_model.value or "gemma-4-26b-a4b-it").strip(),
                "brain1_stage3_gemma_model": (b1_s3_gemma_model.value or "gemma-4-26b-a4b-it").strip(),
                # Keep legacy key in sync for backwards compat (mirrors stage23 choice)
                "brain1_backend": b1s23_select.value,
                "brain1_lmstudio_url": b1_url.value,
                "brain1_stage1_lmstudio_model": b1_s1_lms_model.value,
                "brain1_stage23_lmstudio_model": b1_s23_lms_model.value,
                "brain1_stage1_openrouter_model": b1_s1_or_model.value,
                "brain1_stage23_openrouter_model": b1_s23_or_model.value,
                "brain1_stage1_anthropic_model": (b1_s1_ant_model.value or "claude-haiku-4-5").strip(),
                "brain1_stage23_anthropic_model": (b1_s23_ant_model.value or "claude-sonnet-4-6").strip(),
                "brain2_backend": b2_select.value,
                "brain2_persona": b2_persona_ta.value,
                "brain2_gemini_model": b2_gem_model.value,
                "brain2_gemma_model": b2_gemma_model.value,
                "brain2_anthropic_model": b2_anthropic_model.value,
                "brain2_openai_model": b2_openai_model.value,
                "brain2_lmstudio_url": b2_url.value,
                "brain2_lmstudio_model": b2_model.value,
                "brain2_openrouter_model": b2_openrouter_model.value,
            }
            save_config(new_cfg)
            ui.notify("Saved.", type="positive")

        ui.button("Save Settings", on_click=do_save).props("unelevated size=lg")\
            .style("margin-top: 14px; width: 260px; background:#22c55e "
                   "!important; color:#0b1f12; font-weight:700; "
                   "font-size:15px;")

        # ── Run one half of the pipeline ────────────────────────────────────────
        with ui.expansion("Run one step on its own")\
                .classes("w-full").style(
                    "margin-top: 16px; border: 1px solid var(--accent); "
                    "border-radius: 10px; background: var(--accent-bg); "
                    "box-shadow: 0 0 18px rgba(157,111,255,.22);"):
            ui.html('<div style="font-size:12px; color:var(--text-dim); '
                    'margin-bottom:10px;">A normal run scrapes, judges and '
                    'researches in one go. Sometimes you want just one of '
                    'those - bank listings now and spend the LLM budget '
                    'later, or research companies without pulling anything '
                    'new. Both run detached, so you can leave this tab.</div>')
            with ui.row().style("gap: 14px; align-items: center; flex-wrap: wrap;"):
                ui.label("Sources:").style("font-size:12px; color:var(--text-dim);")
                src_yc = ui.checkbox("YC", value=True).props("dense")
                src_hn = ui.checkbox("Hacker News", value=True).props("dense")
                src_li = ui.checkbox("LinkedIn", value=True).props("dense")

            def _picked() -> str:
                return ",".join(n for n, cb in (("yc", src_yc), ("hn", src_hn),
                                                ("linkedin", src_li)) if cb.value)

            with ui.row().style("gap: 10px; flex-wrap: wrap; margin-top: 8px;"):
                ui.button(
                    "Scrape only",
                    on_click=lambda _: (spawn_detached("pipeline.run_scrape", _picked()),
                                        ui.notify("Scraping. Everything lands "
                                                  "as QUEUED for the next "
                                                  "run.", type="positive")))\
                    .props("unelevated")\
                    .style("background:#2dd4bf !important; color:#10201e; "
                           "font-weight:600;")
                ui.button(
                    "Enrich only",
                    on_click=lambda _: (spawn_detached("pipeline.run_enrich", _picked()),
                                        ui.notify("Researching companies. "
                                                  "Cache-first, so only new "
                                                  "ones cost calls.",
                                                  type="positive")))\
                    .props("unelevated")\
                    .style("background:#60a5fa !important; color:#0d1b2e; "
                           "font-weight:600;")
            ui.html('<div style="font-size:11.5px; color:var(--text-faint); '
                    'margin-top:8px;">Watch the Logs tab for progress.</div>')

            # ── Listing pulse: ask listings if they still exist ──────────────
            ui.html('<div style="font-size:12px; color:var(--text-dim); '
                    'margin:16px 0 8px;">Every listing you keep will outlive '
                    'the job eventually. This asks them directly instead of '
                    'expiring them on a timer - the ten oldest listings here '
                    'were five weeks old and every one was still open. Never '
                    'runs on its own; pick a source and press it. No LLM '
                    'calls, no keys.</div>')
            with ui.row().style("gap: 14px; align-items: center; flex-wrap: wrap;"):
                pulse_li = ui.checkbox("LinkedIn", value=False).props("dense")
                pulse_hn = ui.checkbox("Hacker News", value=False).props("dense")
                pulse_n = ui.number(label="max per source", value=250,
                                    min=10, max=5000, step=50)\
                    .props("outlined dense").style("width: 150px;")

            def _run_pulse(_=None):
                srcs = ([("linkedin") ] if pulse_li.value else []) + \
                       (["hn"] if pulse_hn.value else [])
                if not srcs:
                    ui.notify("Pick at least one source.", type="warning")
                    return
                spawn_detached("pipeline.run_pulse",
                               ",".join(srcs), str(int(pulse_n.value or 250)))
                ui.notify(f"Checking {', '.join(srcs)}. LinkedIn is paced at "
                          f"~6s per listing on purpose - rushing it makes live "
                          f"jobs look dead.", type="positive", timeout=8000)

            ui.button("Listing pulse", on_click=_run_pulse)\
                .props("unelevated").style(
                    "background:#ff6fb5 !important; color:#2b0d1e; "
                    "font-weight:700; margin-top:10px;")
            ui.html('<div style="font-size:11.5px; color:var(--text-faint); '
                    'margin-top:8px;">Y Combinator is not listed because its '
                    'scrape reads every company board in full - listings that '
                    'vanish are already caught for free during a normal run.'
                    '</div>')

        # ── Embeddings (RAG) ────────────────────────────────────────────────────
        ui.html('<div class="section-title" style="margin-top: 24px;">Embeddings (RAG)</div>')
        ui.html(
            '<div style="font-size: 12px; color: var(--text-dim); margin-bottom: 8px;">'
            'Powers the "Similar Past Applications" panel on each job. New jobs are '
            'embedded automatically during a scan; use this to embed jobs that '
            'predate the feature. Idempotent — already-embedded jobs are skipped.</div>'
        )
        rag_cb = ui.checkbox("Enable RAG (embeddings + similar past applications)",
                             value=bool(cfg.get("use_rag", True)))
        if not database.RAG_AVAILABLE:
            ui.html(
                '<div style="font-size: 12px; color: var(--maybe);">'
                'sqlite-vec extension is unavailable, so RAG is disabled. '
                'Install it with <span class="mono">pip install sqlite-vec</span> '
                'and restart the dashboard.</div>'
            )
        else:
            backfill_status = ui.label("").style(
                "font-size: 12px; color: var(--text-dim);"
            )
            backfill_btn = ui.button("Backfill embeddings for existing jobs")\
                .classes("btn-ghost").style("font-size: 12px;")
            _bf = {"done": 0, "total": 0, "running": False}

            def _bf_progress(done, total):
                _bf["done"], _bf["total"] = done, total

            async def do_backfill():
                if _bf["running"]:
                    return
                if not rag_cb.value:
                    backfill_status.set_text("RAG is disabled — enable the toggle first.")
                    return
                _bf["running"] = True
                _bf["done"], _bf["total"] = 0, 0
                try:
                    backfill_btn.props("loading")
                    backfill_btn.disable()
                except RuntimeError:
                    pass

                def _tick():
                    # Don't fire into a deleted slot if the tab was torn down.
                    if backfill_status.is_deleted:
                        return
                    if _bf["total"]:
                        backfill_status.set_text(
                            f"Embedding… {_bf['done']}/{_bf['total']}"
                        )
                timer = ui.timer(0.4, _tick)

                try:
                    embedded, total = await run_in_thread(
                        embeddings.backfill_embeddings, _bf_progress
                    )
                finally:
                    # cancel() (not deactivate()) fully removes the timer so it
                    # can't fire after teardown; in finally so it never leaks.
                    timer.cancel()
                    _bf["running"] = False

                try:
                    backfill_btn.enable()
                    backfill_btn.props(remove="loading")
                    if total == 0:
                        backfill_status.set_text("All jobs already embedded.")
                    else:
                        backfill_status.set_text(
                            f"Done. Embedded {embedded} of {total} job(s)."
                        )
                except RuntimeError:
                    pass
                safe_notify(
                    f"Backfill complete: {embedded} embedded.", type="positive"
                )

            backfill_btn.on("click", lambda _: do_backfill())

        # ── Danger zone ───────────────────────────────────────────────────────
        ui.html('<div class="section-title" style="margin-top: 24px; color: var(--bad);">Danger Zone</div>')
        ui.html(
            '<div style="font-size: 12px; color: var(--text-dim); margin-bottom: 8px;">'
            'Clear all scraped jobs and analyzer snapshots. Useful after changing '
            'profile or hard rejects. Cannot be undone.</div>'
        )

        def clear_db():
            with ui.dialog() as dialog, ui.card():
                ui.html('<div style="font-weight: 600;">Clear entire database?</div>')
                ui.html(
                    '<div style="font-size: 12px; color: var(--text-dim);">'
                    'Deletes all jobs (including applied) and market snapshots. '
                    'Cannot be undone.</div>'
                )
                with ui.row():
                    def do_clear():
                        conn = get_db_connection()
                        try:
                            conn.execute("DELETE FROM jobs")
                            conn.execute("DELETE FROM market_snapshots")
                            conn.execute("DELETE FROM jobs_fts")
                            # ledger goes with the jobs — stale first_seen would
                            # ghost-hide rescraped jobs from the window filter
                            conn.execute("DELETE FROM seen_jobs")
                            if database.RAG_AVAILABLE:
                                conn.execute("DELETE FROM job_embeddings")
                            # brain2_messages is intentionally left alone — chat
                            # history is independent; 'Clear Brain 2 Chat' handles it.
                            conn.commit()
                        finally:
                            conn.close()
                        runner_status.reset()
                        dialog.close()
                        ui.notify("Job database cleared.", type="positive")
                    ui.button("Yes, clear", on_click=do_clear).classes("btn-primary")\
                        .style("background: var(--bad) !important; border-color: var(--bad) !important;")
                    ui.button("Cancel", on_click=dialog.close).classes("btn-ghost")
            dialog.open()

        with ui.row().style("gap: 8px; margin-top: 8px;"):
            ui.button("Clear Job Database", on_click=clear_db).classes("btn-ghost")\
                .style("color: var(--bad); border-color: var(--bad);")

            def clear_chat_setup():
                with ui.dialog() as dialog, ui.card():
                    ui.html('<div style="font-weight: 600;">Clear Brain 2 chat history?</div>')
                    ui.html(
                        '<div style="font-size: 12px; color: var(--text-dim); '
                        'margin: 6px 0;">'
                        'Deletes the entire Brain 2 conversation history. '
                        'This is independent of the job database — clearing '
                        'jobs does NOT clear chat, and vice versa.</div>'
                    )

                    def do_clear_chat():
                        brain2_chat.clear_messages()
                        dialog.close()
                        ui.notify("Brain 2 chat cleared.", type="positive")

                    with ui.row().style("gap: 8px; margin-top: 8px;"):
                        ui.button("Yes, clear chat", on_click=do_clear_chat)\
                            .classes("btn-primary")\
                            .style("background: var(--bad) !important; "
                                   "border-color: var(--bad) !important;")
                        ui.button("Cancel", on_click=dialog.close).classes("btn-ghost")
                dialog.open()

            ui.button("Clear Brain 2 Chat", on_click=clear_chat_setup).classes("btn-ghost")\
                .style("color: var(--bad); border-color: var(--bad);")

            def clear_snapshots():
                with ui.dialog() as dialog, ui.card():
                    ui.html('<div style="font-weight: 600;">Clear Strategist reports?</div>')
                    ui.html(
                        '<div style="font-size: 12px; color: var(--text-dim); '
                        'margin: 6px 0;">'
                        'Deletes all Brain 2 market snapshot reports. Chat history '
                        'and jobs are not affected.</div>'
                    )

                    def do_clear_snap():
                        conn = get_db_connection()
                        try:
                            conn.execute("DELETE FROM market_snapshots")
                            conn.commit()
                        finally:
                            conn.close()
                        dialog.close()
                        ui.notify("Strategist reports cleared.", type="positive")

                    with ui.row().style("gap: 8px; margin-top: 8px;"):
                        ui.button("Yes, clear", on_click=do_clear_snap)\
                            .classes("btn-primary")\
                            .style("background: var(--bad) !important; "
                                   "border-color: var(--bad) !important;")
                        ui.button("Cancel", on_click=dialog.close).classes("btn-ghost")
                dialog.open()

            ui.button("Clear Strategist Reports", on_click=clear_snapshots)\
                .classes("btn-ghost")\
                .style("color: var(--bad); border-color: var(--bad);")
