"""
ui/jobs.py

Job-row rendering: the Jobs tab and the expandable per-row sections (company
intel, similar past applications via RAG, contact & outreach, notes + color,
apply buttons). render_job_row is reused by the Applied tab (ui.tabs).
"""

from __future__ import annotations

import json

from nicegui import ui

from core import database  # for live RAG_AVAILABLE flag
from core.database import get_db_connection
import core.embeddings as embeddings  # RAG: similar past applications
import core.runner_status as runner_status
from pipeline import brain1  # enrich_company_for_job / find_contact_for_job
from pipeline.process_control import spawn_detached, kill_pid, _is_pid_alive

from ui.theme import COLOR_SWATCHES
from ui.helpers import (
    verdict_pill, source_pill, signal_pill, fmt_ts, status_dot_class,
    safe_notify, run_in_thread,
)
from ui.db_queries import (
    fetch_jobs, mark_applied, unmark_applied, update_notes,
    update_row_color, update_verdict,
)


def render_jobs_tab():
    state = {
        "verdicts": ["GOOD", "MAYBE"],
        "query": "",
    }

    # ── Top row: scan button + live status ────────────────────────────────────
    with ui.row().classes("w-full").style(
        "align-items: center; gap: 12px; margin: 16px 0 12px 0; padding: 0 16px;"
    ):
        run_btn = ui.button("Run Scan").classes("btn-primary")
        stop_btn = ui.button("Stop").classes("btn-ghost")\
            .style("color: var(--bad); border-color: var(--bad);")
        ui.label("•").style("color: var(--text-faint);")
        status_html = ui.html("").style(
            "font-family: 'JetBrains Mono', monospace; font-size: 12px; "
            "color: var(--text-dim);"
        )

    def start_scan():
        s = runner_status.read_status()
        existing_pid = s["brain1"].get("pid")
        # Real check: is there actually a brain1 process alive right now?
        if existing_pid and _is_pid_alive(existing_pid):
            ui.notify(
                f"Brain 1 is already running (pid={existing_pid}). Stop it first.",
                type="warning",
            )
            return
        # If status says running but PID is dead, clean up before starting.
        if s["brain1"]["state"] == "running":
            runner_status.finish("brain1", error="stale running state cleared")
        spawn_detached("pipeline.run_brain1")
        ui.notify("Brain 1 started. Jobs will stream in as they're filtered.",
                  type="positive")

    def stop_scan():
        s = runner_status.read_status()
        pid = s["brain1"].get("pid")
        if not pid or not _is_pid_alive(pid):
            ui.notify("Brain 1 is not running.", type="info")
            return
        if kill_pid(pid):
            runner_status.finish("brain1", error="stopped by user")
            ui.notify(f"Brain 1 stopped (pid {pid}).", type="positive")
        else:
            ui.notify(f"Could not stop pid {pid}. May need manual kill.",
                      type="negative")

    run_btn.on("click", lambda _: start_scan())
    stop_btn.on("click", lambda _: stop_scan())

    def refresh_jobs_status():
        s = runner_status.read_status()["brain1"]
        stale = runner_status.is_stale(s)
        dot = status_dot_class(s["state"])
        if stale:
            status_html.set_content(
                f'<span class="status-dot error"></span>'
                f'<span style="color: var(--maybe);">stale '
                f'(last update {fmt_ts(s.get("updated"))}) — process likely died</span>'
            )
        elif s["state"] == "running":
            status_html.set_content(
                f'<span class="status-dot {dot}"></span>'
                f'<span class="mono">stage1:</span> {s.get("stage1","idle")} '
                f'&nbsp;&nbsp;<span class="mono">stage2:</span> {s.get("stage2","idle")} '
                f'&nbsp;&nbsp;<span class="mono">stage3:</span> {s.get("stage3","idle")}'
            )
        elif s["state"] == "done":
            status_html.set_content(
                f'<span class="status-dot {dot}"></span>last run: '
                f'<span class="mono">scraped={s.get("scraped",0)} '
                f'good={s.get("good",0)} maybe={s.get("maybe",0)} '
                f'bad={s.get("bad",0)} hard_rej={s.get("hard_rej",0)}</span>'
            )
        elif s["state"] == "error":
            status_html.set_content(
                f'<span class="status-dot {dot}"></span>'
                f'<span style="color: var(--bad);">error: {s.get("error","")}</span>'
            )
        else:
            status_html.set_content(
                f'<span class="status-dot {dot}"></span>idle'
            )

    refresh_jobs_status()
    ui.timer(2.0, refresh_jobs_status)

    # ── Counts row ────────────────────────────────────────────────────────────
    counts_row = ui.row().classes("w-full").style(
        "gap: 10px; padding: 0 16px; margin-bottom: 16px;"
    )

    def refresh_counts():
        conn = get_db_connection()
        try:
            r = conn.execute(
                "SELECT "
                "  SUM(CASE WHEN verdict='GOOD' AND (applied IS NULL OR applied=0) THEN 1 ELSE 0 END) AS good, "
                "  SUM(CASE WHEN verdict='MAYBE' AND (applied IS NULL OR applied=0) THEN 1 ELSE 0 END) AS maybe, "
                "  SUM(CASE WHEN verdict='BAD' THEN 1 ELSE 0 END) AS bad, "
                "  SUM(CASE WHEN applied=1 THEN 1 ELSE 0 END) AS applied, "
                "  COUNT(*) AS total "
                "FROM jobs"
            ).fetchone()
        finally:
            conn.close()
        counts_row.clear()
        with counts_row:
            for val, lbl in [
                (r["total"] or 0,   "Total"),
                (r["good"] or 0,    "Good"),
                (r["maybe"] or 0,   "Maybe"),
                (r["bad"] or 0,     "Bad"),
                (r["applied"] or 0, "Applied"),
            ]:
                with ui.element("div").classes("metric").style("flex: 1;"):
                    ui.html(f'<div class="val">{val}</div><div class="lbl">{lbl}</div>')

    refresh_counts()
    ui.timer(3.0, refresh_counts)

    # ── Filter bar ────────────────────────────────────────────────────────────
    with ui.row().classes("w-full").style(
        "padding: 0 16px; margin-bottom: 12px; gap: 10px; align-items: center;"
    ):
        def toggle_verdict(v, on):
            if on and v not in state["verdicts"]:
                state["verdicts"].append(v)
            if not on and v in state["verdicts"]:
                state["verdicts"].remove(v)
            refresh_list()

        ui.checkbox("Good", value=True,
                    on_change=lambda e: toggle_verdict("GOOD", e.value))
        ui.checkbox("Maybe", value=True,
                    on_change=lambda e: toggle_verdict("MAYBE", e.value))
        ui.checkbox("Bad", value=False,
                    on_change=lambda e: toggle_verdict("BAD", e.value))
        search_input = ui.input(placeholder="Search title, company, stack, description...")\
            .classes("mono").style("flex: 1; min-width: 240px;")

        def on_search(_e=None):
            state["query"] = search_input.value or ""
            refresh_list()
        search_input.on("change", on_search)
        search_input.on("blur", on_search)
        # Live update while typing (debounced by quasar's input event)
        search_input.on("keyup.enter", on_search)

    # ── Job list container ────────────────────────────────────────────────────
    # Track last-known job count; if more arrive during a scan, show a refresh
    # nudge instead of destroying the list (which kills open expansions).
    list_meta = {"last_count": -1, "last_query": None, "last_verdicts": None}
    list_container = ui.column().classes("w-full").style("padding: 0 16px;")
    refresh_nudge_container = ui.row().classes("w-full")\
        .style("padding: 0 16px; margin-bottom: 8px;")

    def refresh_list(force: bool = True):
        if not force:
            return
        list_container.clear()
        refresh_nudge_container.clear()
        rows = fetch_jobs(state["verdicts"], state["query"])
        list_meta["last_count"] = len(rows)
        list_meta["last_query"] = state["query"]
        list_meta["last_verdicts"] = tuple(state["verdicts"])
        with list_container:
            if not rows:
                ui.label("No jobs match filters. Run a scan, or widen filters.")\
                    .style("color: var(--text-dim); padding: 24px 0; text-align: center;")
                return
            ui.label(f"{len(rows)} listings").style(
                "color: var(--text-dim); font-size: 12px; margin-bottom: 4px;"
            )
            for row in rows:
                render_job_row(row, lambda: refresh_list(force=True))

    def check_for_new_jobs():
        # Only show nudge if Brain 1 is running and there are more jobs than last render.
        if runner_status.read_status()["brain1"]["state"] != "running":
            return
        if (list_meta["last_query"] != state["query"]
                or list_meta["last_verdicts"] != tuple(state["verdicts"])):
            return  # filter changed; user will see new list on next interaction
        conn = get_db_connection()
        try:
            params: list = []
            sql = "SELECT COUNT(*) FROM jobs WHERE verdict IN ({}) AND (applied IS NULL OR applied = 0)".format(
                ",".join("?" for _ in state["verdicts"])
            )
            params.extend(state["verdicts"])
            current = conn.execute(sql, params).fetchone()[0]
        finally:
            conn.close()
        if current > list_meta["last_count"] >= 0:
            delta = current - list_meta["last_count"]
            refresh_nudge_container.clear()
            with refresh_nudge_container:
                btn = ui.button(
                    f"⤓ {delta} new listing{'s' if delta != 1 else ''} — click to refresh"
                ).classes("btn-ghost").style("font-size: 12px;")
                btn.on("click", lambda _: refresh_list(force=True))

    refresh_list(force=True)
    ui.timer(4.0, check_for_new_jobs)


def _render_notes_and_color(row: dict, refresh_list_fn):
    """Sticky-note textarea + color swatch row, shared by Jobs and Applied tabs."""
    job_id = row["id"]
    current_color = row.get("row_color") or ""

    with ui.element("div").classes("notes-block"):
        ui.html('<div class="notes-label">Notes</div>')
        notes_ta = ui.textarea(value=row.get("notes") or "")\
            .props("outlined dense autogrow")\
            .style(
                "width: 100%; font-family: 'JetBrains Mono', monospace; "
                "font-size: 12px;"
            )

        def on_blur(_e=None):
            update_notes(job_id, notes_ta.value or "")
        notes_ta.on("blur", on_blur)

        # Color swatch row
        ui.html('<div class="notes-label" style="margin-top: 10px;">Color label</div>')
        with ui.element("div").classes("swatch-row"):
            # We render swatches as plain HTML clickable spans; NiceGUI lets us
            # attach click handlers via .on('click') on ui.html elements.
            for color, cls, tooltip in COLOR_SWATCHES:
                active = " active" if color == current_color else ""
                swatch = ui.html(
                    f'<span class="swatch {cls}{active}" title="{tooltip}"></span>'
                )
                # Capture color in default arg to avoid late-binding bug.
                def make_handler(c=color):
                    def _handler(_e):
                        update_row_color(job_id, c)
                        safe_notify(f"Color: {c or 'none'}", type="positive")
                        try:
                            refresh_list_fn()
                        except Exception:
                            pass
                    return _handler
                swatch.on("click", make_handler())


def render_job_row(row: dict, refresh_list_fn):
    row_color = row.get("row_color") or ""
    job_row = ui.element("div").classes("job-row")
    if row_color:
        # NiceGUI's `.props()` accepts raw HTML attributes too
        job_row.props(f'data-color={row_color}')
    with job_row:
        # Title row
        with ui.row().style("align-items: flex-start; justify-content: space-between; gap: 12px;"):
            with ui.column().style("gap: 2px; flex: 1; min-width: 0;"):
                ui.html(f'<div class="job-title">{row.get("title","(no title)")}</div>')
                meta_bits = [
                    row.get("company") or "—",
                    fmt_ts(row.get("date_posted"), 10),
                ]
                if row.get("location"):
                    meta_bits.append(row["location"])
                sal_min = row.get("salary_min")
                sal_max = row.get("salary_max")
                if sal_min or sal_max:
                    meta_bits.append(
                        f'{sal_min or "?"}–{sal_max or "?"} {row.get("currency") or ""}'
                    )
                ui.html(
                    f'<div class="job-meta">{" · ".join(str(b) for b in meta_bits)}</div>'
                )

            with ui.row().style("gap: 6px; align-items: center; flex-shrink: 0;"):
                ui.html(source_pill(row.get("source") or ""))
                ui.html(verdict_pill(
                    row.get("verdict") or "—",
                    row.get("reject_reason") or "",
                ))
                if row.get("gemma2_done"):
                    ui.html(signal_pill(
                        row.get("hiring_signal") or "uncertain",
                        row.get("culture_flags") or "[]",
                    ))

        # Reject reason if any
        if row.get("reject_reason"):
            color = "var(--bad)" if "hard_reject" in row["reject_reason"] else "var(--text-dim)"
            ui.html(
                f'<div style="font-size: 12px; color: {color}; margin-top: 8px;">'
                f'{row["reject_reason"]}</div>'
            )

        # Expandable: full listing
        with ui.expansion("Listing", icon=None).classes("w-full").style("margin-top: 10px;"):
            ui.html(
                f'<div class="desc-scroll">'
                f'{(row.get("description") or "")[:5000]}'
                f'</div>'
            )
            if row.get("url"):
                ui.html(
                    f'<div style="margin-top: 8px;">'
                    f'<a href="{row["url"]}" target="_blank" '
                    f'style="color: var(--accent); text-decoration: none; font-size: 13px;">'
                    f'Open on {row.get("source","listing")} ↗</a></div>'
                )

        # Expandable: company intel
        with ui.expansion("Company Intel", icon=None).classes("w-full"):
            render_company_intel(row, refresh_list_fn)

        # Expandable: similar past applications (RAG)
        with ui.expansion("Similar Past Applications", icon=None).classes("w-full"):
            render_similar_applications(row)

        # Expandable: contact & outreach (GOOD or MAYBE w/ gemma3)
        if row.get("verdict") in ("GOOD", "MAYBE"):
            with ui.expansion("Contact & Outreach", icon=None).classes("w-full"):
                render_contact_section(row, refresh_list_fn)

        # Notes + color
        _render_notes_and_color(row, refresh_list_fn)

        # Action row
        with ui.row().style("margin-top: 10px; gap: 8px;"):
            _render_apply_button(row, refresh_list_fn)


def _render_apply_button(row: dict, refresh_list_fn):
    """Apply/Unapply toggle button. Used at the bottom of each job row and
    inside the Company Intel / Contact & Outreach expansions for convenience."""
    def _safe_refresh():
        try:
            refresh_list_fn()
        except Exception:
            pass

    if row.get("applied"):
        ui.button(
            "Unapply",
            on_click=lambda _, jid=row["id"]: (
                unmark_applied(jid),
                safe_notify("Unapplied."),
                _safe_refresh(),
            ),
        ).classes("btn-ghost").style("font-size: 12px;")
    else:
        ui.button(
            "Move to Applied",
            on_click=lambda _, jid=row["id"]: (
                mark_applied(jid),
                safe_notify("Moved to Applied."),
                _safe_refresh(),
            ),
        ).classes("btn-ghost").style("font-size: 12px;")

    if row.get("verdict") in ("GOOD", "MAYBE"):
        ui.button(
            "Move to BAD",
            on_click=lambda _, jid=row["id"]: (
                update_verdict(jid, "BAD", "manual_bad"),
                safe_notify("Moved to BAD.", type="warning"),
                _safe_refresh(),
            ),
        ).classes("btn-ghost").style(
            "font-size: 12px; color: var(--bad); border-color: var(--bad);"
        )


def render_company_intel(row: dict, refresh_list_fn):
    if row.get("gemma2_done"):
        summary = row.get("company_summary") or "—"
        ui.html(f'<div style="font-size: 13.5px; line-height: 1.6; margin-bottom: 8px;">{summary}</div>')
        with ui.row().style("gap: 16px; font-size: 12px; color: var(--text-dim);"):
            ui.html(f'<span>Size: <span class="mono" style="color: var(--text);">'
                    f'{row.get("company_size","—")}</span></span>')
            try:
                stack = json.loads(row.get("real_stack") or "[]")
            except json.JSONDecodeError:
                stack = []
            if stack:
                ui.html(f'<span>Stack: <span class="mono" style="color: var(--text);">'
                        f'{" · ".join(stack)}</span></span>')
        try:
            flags = json.loads(row.get("culture_flags") or "[]")
        except json.JSONDecodeError:
            flags = []
        if flags:
            ui.html(
                f'<div style="margin-top: 8px; font-size: 12px; color: var(--maybe);">'
                f'⚠ {" · ".join(flags)}</div>'
            )
    else:
        # MAYBE or GOOD-where-Stage-2-failed
        with ui.row().style("align-items: center; gap: 10px; flex-wrap: wrap;"):
            if row.get("verdict") == "GOOD":
                ui.label("Auto research didn't complete. Retry manually:").style(
                    "color: var(--text-dim); font-size: 13px;"
                )
            else:
                ui.label("Not researched yet.").style(
                    "color: var(--text-dim); font-size: 13px;"
                )
            btn = ui.button("Research Company").classes("btn-ghost")\
                .style("font-size: 12px;")
            async def do_research(jid=row["id"]):
                try:
                    btn.props("loading")
                    btn.disable()
                except RuntimeError:
                    pass
                ok = await run_in_thread(brain1.enrich_company_for_job, jid)
                try:
                    btn.enable()
                    btn.props(remove="loading")
                except RuntimeError:
                    pass
                if ok:
                    # Re-fetch to see if it was demoted to BAD
                    conn = get_db_connection()
                    try:
                        r = conn.execute(
                            "SELECT verdict, reject_reason FROM jobs WHERE id=?",
                            (jid,),
                        ).fetchone()
                    finally:
                        conn.close()
                    if r and r["verdict"] == "BAD" \
                            and "stage2_demoted" in (r["reject_reason"] or ""):
                        safe_notify(
                            f"Demoted to BAD: {r['reject_reason'].split(': ', 1)[-1]}",
                            type="warning",
                            timeout=4000,
                        )
                    else:
                        safe_notify("Company researched.", type="positive")
                    try:
                        refresh_list_fn()
                    except RuntimeError:
                        pass
                else:
                    safe_notify("Research failed. Check logs.", type="negative")
            btn.on("click", lambda _: do_research())


def render_similar_applications(row: dict):
    """RAG: show the top-3 jobs the user has already Applied to that are most
    semantically similar to this one. Quiet, never-error fallback when RAG is
    off, this job has no embedding yet, or there are no applied jobs."""
    def _quiet(msg: str = "No similar applications yet."):
        ui.html(
            f'<div style="font-size: 12.5px; color: var(--text-dim);">{msg}</div>'
        )

    if not database.RAG_AVAILABLE:
        _quiet()
        return

    conn = get_db_connection()
    try:
        results = embeddings.find_similar_applications(conn, row["id"], top_k=3)
    except Exception:
        results = []
    finally:
        conn.close()

    if not results:
        _quiet()
        return

    for r in results:
        pct = f"{r['score'] * 100:.0f}%"
        ui.html(
            f'<div style="display: flex; justify-content: space-between; '
            f'align-items: baseline; gap: 12px; padding: 4px 0; '
            f'border-bottom: 1px solid var(--border);">'
            f'<span style="font-size: 13px;">'
            f'<strong>{r.get("title") or "(no title)"}</strong>'
            f'<span style="color: var(--text-dim);"> · {r.get("company") or "—"}</span>'
            f'</span>'
            f'<span class="mono" style="font-size: 12px; color: var(--accent); '
            f'flex-shrink: 0;">{pct} match</span>'
            f'</div>'
        )


def render_contact_section(row: dict, refresh_list_fn):
    if row.get("gemma3_done"):
        name = row.get("contact_name") or "—"
        title = row.get("contact_title") or "—"
        email = row.get("contact_email") or "—"
        conf = row.get("email_confidence") or "—"
        src = row.get("email_source") or "—"
        ui.html(
            f'<div style="font-size: 13.5px; margin-bottom: 4px;">'
            f'<strong>{name}</strong> — {title}</div>'
            f'<div class="mono" style="font-size: 12px; color: var(--text-dim);">'
            f'{email} <span style="color: var(--text-faint);">'
            f'({conf} via {src})</span></div>'
        )
        ui.textarea(label="Outreach draft", value=row.get("outreach_draft") or "")\
            .style("margin-top: 10px; width: 100%; "
                   "font-family: 'JetBrains Mono', monospace; font-size: 12.5px;")\
            .props("outlined dense autogrow")
    else:
        # Either MAYBE (Stage 3 never ran) or GOOD where Stage 3 failed.
        with ui.row().style("align-items: center; gap: 10px; flex-wrap: wrap;"):
            if row.get("verdict") == "GOOD":
                ui.label("Auto outreach didn't complete. Retry manually:")\
                    .style("color: var(--text-dim); font-size: 13px;")
            elif not row.get("gemma2_done"):
                ui.label("Research the company first, then find contact.")\
                    .style("color: var(--text-dim); font-size: 13px;")
            btn = ui.button("Find Contact").classes("btn-ghost")\
                .style("font-size: 12px;")
            async def do_contact(jid=row["id"]):
                try:
                    btn.props("loading")
                    btn.disable()
                except RuntimeError:
                    pass
                ok = await run_in_thread(brain1.find_contact_for_job, jid)
                try:
                    btn.enable()
                    btn.props(remove="loading")
                except RuntimeError:
                    pass
                if ok:
                    safe_notify("Contact found.", type="positive")
                    try:
                        refresh_list_fn()
                    except RuntimeError:
                        pass
                else:
                    safe_notify("Contact hunt failed. Check logs.", type="negative")
            btn.on("click", lambda _: do_contact())
