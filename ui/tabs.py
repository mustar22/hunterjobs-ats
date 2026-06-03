"""
ui/tabs.py

The Applied, Market Analyzer, Logs, and Setup tabs. Applied reuses
render_job_row from ui.jobs; Market/Setup touch config (core.config), the
brains (pipeline), and process control (spawn/kill). LOG_PATH (read by the Logs
tab) is anchored to the repo root.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nicegui import ui

from core import database  # for live RAG_AVAILABLE flag
from core.database import get_db_connection
import core.embeddings as embeddings  # RAG backfill
import core.runner_status as runner_status
from core.config import load_config, save_config, load_keys
from pipeline import brain2_chat  # chat + clear history
from pipeline.process_control import spawn_detached, kill_pid, _is_pid_alive

from ui.helpers import status_dot_class, fmt_ts, safe_notify, run_in_thread
from ui.db_queries import fetch_applied
from ui.jobs import render_job_row

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
    # No auto-refresh on Applied tab — would close any open expansions
    # while the user is reading. Refresh is via explicit user action only.


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

        ui.html(
            '<div style="font-size: 12px; color: var(--text-dim);">'
            'Powered by Gemini 3.1 Pro with Google Search grounding. '
            'Aggregates 7 days of Brain 1 output and produces a strategic report.</div>'
        )

        # Status line
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

        # Metrics row
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
                    (r["ghost"] or 0, "Ghost"),
                ]:
                    with ui.element("div").classes("metric").style("flex: 1;"):
                        ui.html(f'<div class="val">{val}</div><div class="lbl">{lbl}</div>')

        refresh_market_metrics()
        ui.timer(5.0, refresh_market_metrics)

        # Decree
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

        # Backend display (configured in Setup tab)
        cfg = load_config()
        keys = load_keys()

        backend_options = {
            "gemini":    "Gemini",
            "gemma":     "Gemma 4 26B (free)",
            "anthropic": "Claude (paid)",
            "openai":    "OpenAI GPT (paid)",
            "lmstudio":  "LM Studio (local)",
        }
        gemini_models_pretty = {
            "gemini-3.5-flash":       "3.5 Flash",
            "gemini-3.1-pro-preview": "3.1 Pro",
        }
        anthropic_models_pretty = {
            "claude-opus-4-7":           "Opus 4.7",
            "claude-sonnet-4-6":         "Sonnet 4.6",
            "claude-haiku-4-5-20251001": "Haiku 4.5",
        }

        def _current_backend_label() -> str:
            c = load_config()
            b = c.get("brain2_backend", "gemini")
            label = backend_options.get(b, b)
            if b == "gemini":
                sub = gemini_models_pretty.get(
                    c.get("brain2_gemini_model", "gemini-3.5-flash"),
                    c.get("brain2_gemini_model", ""),
                )
                label = f"{label} {sub}"
            elif b == "anthropic":
                sub = anthropic_models_pretty.get(
                    c.get("brain2_anthropic_model", "claude-sonnet-4-6"),
                    c.get("brain2_anthropic_model", ""),
                )
                label = f"{label} — {sub}"
            elif b == "openai":
                label = f"{label} — {c.get('brain2_openai_model', 'gpt-5.5')}"
            return label

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
        if _b == "lmstudio":
            ui.html(
                '<div style="font-size: 12px; color: var(--maybe);">'
                '⚠ Local models under 20B params often hallucinate the tool-call '
                'format (echoing the JSON result back into their text). For best '
                'chat quality, use Gemini or Claude. Snapshot generation works '
                'fine with local models.</div>'
            )

        # Chat scroll container
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
                    # Tool results: render compact
                    if role == "tool":
                        # Pretty-format JSON if possible
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
                    # Escape HTML in content
                    safe = content.replace("<", "&lt;").replace(">", "&gt;")
                    ui.html(f'<div class="chat-msg {cls}">{safe}</div>')
            # Auto-scroll to bottom
            ui.run_javascript(
                "const el = document.querySelector('.chat-container'); "
                "if (el) el.scrollTop = el.scrollHeight;"
            )

        refresh_chat()

        # Input row
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

        # Status block
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
        # Theme
        ui.html('<div class="section-title">Appearance</div>')
        with ui.row().style("gap: 10px; align-items: center;"):
            def on_theme_change(e):
                # NiceGUI v2: ui.select with dict options passes value via e.value
                # when using on_change=, but if attached via .on('update:model-value')
                # it comes as e.args. Read from the select directly to be safe.
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

        # API keys (read-only display)
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
                f'<br>GITHUB_PAT        = "{"*" * 20 if keys["github"] else "(not set, optional)"}"'
                f'</div>'
            )
        if not keys["google"]:
            ui.html(
                '<div style="font-size: 12px; color: var(--maybe);">'
                'GOOGLE_API_KEY is not set. Brain 1 and Brain 2 (Gemini/Gemma) will fail until you add it.</div>'
            )

        # Profile
        ui.html('<div class="section-title">Candidate Profile</div>')
        ui.html(
            '<div style="font-size: 12px; color: var(--text-dim); margin-bottom: 8px;">'
            'The more specific, the sharper Brain 1 filters. '
            'Include target salary, stack, geo constraints, and what you want to exclude.</div>'
        )
        profile_ta = ui.textarea(value=cfg["profile"]).props("outlined autogrow")\
            .style("width: 100%; font-family: 'JetBrains Mono', monospace; font-size: 12.5px;")

        # Search terms
        ui.html('<div class="section-title">Search Terms</div>')
        ui.html(
            '<div style="font-size: 12px; color: var(--text-dim); margin-bottom: 8px;">'
            'One per line. Each becomes a JobSpy search.</div>'
        )
        terms_ta = ui.textarea(value=cfg["search_terms"]).props("outlined autogrow")\
            .style("width: 100%; font-family: 'JetBrains Mono', monospace; font-size: 12.5px;")

        # Hard rejects
        ui.html('<div class="section-title">Hard Reject Keywords</div>')
        ui.html(
            '<div style="font-size: 12px; color: var(--text-dim); margin-bottom: 8px;">'
            'Substring match against title + company + description. Zero API cost. '
            'Use Export/Import to share blacklists with others.</div>'
        )
        rejects_ta = ui.textarea(value=cfg["hard_rejects"]).props("outlined autogrow")\
            .style("width: 100%; font-family: 'JetBrains Mono', monospace; font-size: 12.5px;")

        with ui.row().style("gap: 8px; margin-top: 8px;"):
            def export_rejects():
                lines = [l.strip() for l in (rejects_ta.value or "").splitlines() if l.strip()]
                exported = datetime.now(timezone.utc).isoformat(timespec="seconds")
                # Plain text format: comment header + one entry per line
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
                        # NiceGUI has changed upload event shape across versions.
                        # Try every known attribute in order.
                        raw = None
                        try:
                            # Try common attribute names
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

                        # Parse: skip blank lines and comments
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

        # Scrape settings
        ui.html('<div class="section-title">Scrape Settings</div>')
        with ui.row().style("gap: 14px; flex-wrap: wrap;"):
            floor_in = ui.number(label="Salary floor (USD/month)",
                                 value=cfg["salary_floor"], step=100)\
                .props("outlined").style("width: 220px;")
            rw_in = ui.number(label="Results per term",
                              value=cfg["results_wanted"], step=10, min=10, max=200)\
                .props("outlined").style("width: 200px;")
            ho_in = ui.number(label="Max hours old",
                              value=cfg["hours_old"], step=12, min=12, max=336)\
                .props("outlined").style("width: 180px;")

        ui.html('<div class="section-title">Sources</div>')
        sources_set = set(cfg["sources"])
        with ui.row().style("gap: 14px; align-items: center;"):
            linkedin_cb = ui.checkbox("LinkedIn", value=("linkedin" in sources_set))
            indeed_cb = ui.checkbox("Indeed", value=("indeed" in sources_set))
            # YC startups are company-based, scraped separately from JobSpy sites.
            yc_cb = ui.checkbox("Y Combinator startups", value=bool(cfg.get("use_yc")))
            yc_remote_cb = ui.checkbox("YC remote only",
                                       value=bool(cfg.get("yc_remote_only", True)))
            yc_team_in = ui.number(label="YC max team size",
                                   value=cfg.get("yc_max_team_size", 50),
                                   step=10, min=1, max=500)\
                .props("outlined dense").style("width: 160px;")

        # Backend selectors
        ui.html('<div class="section-title">Brain 1 — Stage 1 Backend (job filter, high volume)</div>')
        ui.html(
            '<div style="font-size: 12px; color: var(--text-dim); margin-bottom: 8px;">'
            'Stage 1 runs once per scraped listing. Local LM Studio with an 8B model '
            'works great here — fast and free.</div>'
        )
        # Resolve current value: prefer per-stage, fall back to legacy
        s1_current = cfg.get("brain1_stage1_backend") or cfg.get("brain1_backend", "gemma")
        b1s1_select = ui.select(
            {"gemma": "Gemma 4 (Google AI Studio, free tier)",
             "lmstudio": "LM Studio (local)"},
            value=s1_current,
        ).style("min-width: 320px;")

        ui.html('<div class="section-title">Brain 1 — Stage 2/3 Backend (company research + outreach)</div>')
        ui.html(
            '<div style="font-size: 12px; color: var(--text-dim); margin-bottom: 8px;">'
            'Stage 2/3 only runs on GOOD jobs (low volume) and needs solid '
            'instruction-following. Gemma 4 recommended.</div>'
        )
        s23_current = cfg.get("brain1_stage23_backend") or cfg.get("brain1_backend", "gemma")
        b1s23_select = ui.select(
            {"gemma": "Gemma 4 (Google AI Studio, free tier)",
             "lmstudio": "LM Studio (local)"},
            value=s23_current,
        ).style("min-width: 320px;")

        with ui.column().style("gap: 8px;") as b1_lmstudio_box:
            ui.html(
                '<div style="font-size: 12px; color: var(--text-dim); margin: 8px 0;">'
                'LM Studio settings (shared by both stages if either is set to LM Studio):</div>'
            )
            b1_url = ui.input(label="LM Studio URL",
                              value=cfg["brain1_lmstudio_url"])\
                .props("outlined").style("width: 360px;")
            b1_model = ui.input(label="LM Studio model name (blank = auto-detect loaded model)",
                                value=cfg["brain1_lmstudio_model"])\
                .props("outlined").style("width: 360px;")

        def _refresh_b1_lmstudio_visibility():
            any_lm = (b1s1_select.value == "lmstudio"
                      or b1s23_select.value == "lmstudio")
            b1_lmstudio_box.set_visibility(any_lm)

        _refresh_b1_lmstudio_visibility()
        b1s1_select.on("update:model-value", lambda _e: _refresh_b1_lmstudio_visibility())
        b1s23_select.on("update:model-value", lambda _e: _refresh_b1_lmstudio_visibility())

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
                "lmstudio":  "LM Studio (local, no web search)",
            },
            value=cfg["brain2_backend"],
        ).style("min-width: 360px;")

        # Containers wrap each model-field group so we can show/hide them.
        with ui.column().style("gap: 8px; margin-top: 8px;") as b2_gemini_box:
            b2_gem_model = ui.select(
                {
                    "gemini-3.5-flash":        "Gemini 3.5 Flash (recommended — fast, cheap, top agentic)",
                    "gemini-3.1-pro-preview":  "Gemini 3.1 Pro (best for deep reasoning)",
                },
                value=cfg.get("brain2_gemini_model", "gemini-3.5-flash"),
            ).style("min-width: 360px;")
        with ui.column().style("gap: 8px; margin-top: 8px;") as b2_gemma_box:
            b2_gemma_model = ui.input(label="Gemma model",
                                      value=cfg.get("brain2_gemma_model",
                                                    "gemma-4-26b-a4b-it"))\
                .props("outlined").style("width: 360px;")
        with ui.column().style("gap: 8px; margin-top: 8px;") as b2_anthropic_box:
            b2_anthropic_model = ui.select(
                {
                    "claude-opus-4-7":           "Claude Opus 4.7 (top tier)",
                    "claude-sonnet-4-6":         "Claude Sonnet 4.6 (balanced, recommended)",
                    "claude-haiku-4-5-20251001": "Claude Haiku 4.5 (cheap, fast)",
                },
                value=cfg.get("brain2_anthropic_model", "claude-sonnet-4-6"),
            ).style("min-width: 360px;")
        with ui.column().style("gap: 8px; margin-top: 8px;") as b2_openai_box:
            b2_openai_model = ui.select(
                {
                    "gpt-5.5":      "GPT-5.5 (flagship)",
                    "gpt-5.4-mini": "GPT-5.4 Mini (fast, cheap)",
                    "gpt-5.4-nano": "GPT-5.4 Nano (fastest)",
                },
                value=cfg.get("brain2_openai_model", "gpt-5.5"),
            ).style("min-width: 360px;")
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
            b2_lmstudio_box.set_visibility(sel == "lmstudio")

        _refresh_b2_visibility()
        b2_select.on("update:model-value", lambda _e: _refresh_b2_visibility())

        def do_save():
            sources = []
            if linkedin_cb.value: sources.append("linkedin")
            if indeed_cb.value: sources.append("indeed")
            new_cfg = {
                **cfg,
                "use_yc": bool(yc_cb.value),
                "yc_remote_only": bool(yc_remote_cb.value),
                "yc_max_team_size": int(yc_team_in.value or 50),
                "theme": theme_select.value,
                "profile": profile_ta.value,
                "search_terms": terms_ta.value,
                "hard_rejects": rejects_ta.value,
                "salary_floor": int(floor_in.value or 0),
                "results_wanted": int(rw_in.value or 100),
                "hours_old": int(ho_in.value or 72),
                # Save the actual ticked list — an empty list is allowed (YC-only run).
                # Do NOT coerce back to ["linkedin"]; that silently forces LinkedIn on.
                "sources": sources,
                "brain1_stage1_backend": b1s1_select.value,
                "brain1_stage23_backend": b1s23_select.value,
                # Keep legacy key in sync for backwards compat (mirrors stage23 choice)
                "brain1_backend": b1s23_select.value,
                "brain1_lmstudio_url": b1_url.value,
                "brain1_lmstudio_model": b1_model.value,
                "brain2_backend": b2_select.value,
                "brain2_gemini_model": b2_gem_model.value,
                "brain2_gemma_model": b2_gemma_model.value,
                "brain2_anthropic_model": b2_anthropic_model.value,
                "brain2_openai_model": b2_openai_model.value,
                "brain2_lmstudio_url": b2_url.value,
                "brain2_lmstudio_model": b2_model.value,
            }
            save_config(new_cfg)
            ui.notify("Saved.", type="positive")

        ui.button("Save Settings", on_click=do_save).classes("btn-primary")\
            .style("margin-top: 12px; width: 200px;")

        # ── Embeddings (RAG) ────────────────────────────────────────────────────
        ui.html('<div class="section-title" style="margin-top: 24px;">Embeddings (RAG)</div>')
        ui.html(
            '<div style="font-size: 12px; color: var(--text-dim); margin-bottom: 8px;">'
            'Powers the "Similar Past Applications" panel on each job. New jobs are '
            'embedded automatically during a scan; use this to embed jobs that '
            'predate the feature. Idempotent — already-embedded jobs are skipped.</div>'
        )
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
                _bf["running"] = True
                _bf["done"], _bf["total"] = 0, 0
                try:
                    backfill_btn.props("loading")
                    backfill_btn.disable()
                except RuntimeError:
                    pass

                def _tick():
                    # Bail if the Setup tab (and our label) has been torn down —
                    # otherwise the timer fires into a deleted slot.
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
                    # cancel() (not deactivate()) ends the timer's loop and removes
                    # the element from its slot, so it can never fire after the slot
                    # is gone. Runs even if the backfill raises, so no timer leaks.
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
                            if database.RAG_AVAILABLE:
                                conn.execute("DELETE FROM job_embeddings")
                            # Note: brain2_messages is NOT cleared here.
                            # Brain 2 chat history is independent of the jobs
                            # database. Use the dedicated 'Clear Brain 2 Chat'
                            # button in Setup or the Market Analyzer panel.
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
