"""
ui/companies.py

The Companies tab: everything the pipeline has researched, yours and seeded,
as an open grid you can search. Research only - contacts stay attached to the
jobs they belong to, because a scrollable directory of people's emails is the
thing this project exists to complain about.

Loads 48 at a time and grows as you scroll. Leaving the tab for a while resets
it back to 48 so an idle session isn't holding thousands of rows.
"""

from __future__ import annotations

import json
import time

from nicegui import ui

from core.database import get_db_connection
from pipeline.brain1 import _is_staffing_agency
from ui.helpers import fmt_ts, signal_pill

PAGE = 48
RESET_AFTER_S = 60          # away this long and the grid shrinks back to one page


def _esc(s) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _rows(query: str, search_desc: bool, limit: int) -> tuple[list, int]:
    where, args = "COALESCE(company_summary,'') != ''", []
    if query:
        like = f"%{query}%"
        if search_desc:
            where += (" AND (name LIKE ? OR domain LIKE ? OR "
                      "company_summary LIKE ? OR real_stack LIKE ?)")
            args = [like] * 4
        else:
            where += " AND name LIKE ?"
            args = [like]
    conn = get_db_connection()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM companies WHERE {where}", args).fetchone()[0]
        rows = conn.execute(
            f"""SELECT name, domain, company_summary, hiring_signal, real_stack,
                       culture_flags, company_size, sources, researched_at,
                       contacts, hunted
                FROM companies WHERE {where}
                ORDER BY researched_at DESC LIMIT ?""",
            (*args, limit)).fetchall()
    finally:
        conn.close()
    return rows, total


def _loads(raw, fallback):
    try:
        return json.loads(raw or "")
    except (json.JSONDecodeError, TypeError):
        return fallback


def render_companies_tab(is_active=None):
    """is_active: callable telling us whether this tab is the one on screen.
    Without it the grid still works, it just never shrinks back."""
    state = {"limit": PAGE, "left_at": 0.0, "grew_at": 0.0}

    with ui.column().classes("w-full").style("gap: 10px; padding: 8px 0;"):
        ui.html('<div class="section-title" style="margin:0;">Companies</div>')
        ui.label("Every company the pipeline has researched. Handy the night "
                 "before an interview, or just to see who is out there.")\
            .style("color: var(--text-dim); font-size: 13px;")
        with ui.row().classes("w-full items-center").style("gap: 10px;"):
            search = ui.input(placeholder="Search company name - press Enter")\
                .props("outlined dense clearable").style("flex: 1;")
            desc_toggle = ui.switch("Search descriptions too", value=False)\
                .props("dense").style("font-size: 12px;")
        count_lbl = ui.label("").style("color: var(--text-faint); font-size: 12px;")
        grid = ui.element("div").classes("w-full").style(
            "display: grid; grid-template-columns: repeat(auto-fill, "
            "minmax(270px, 1fr)); gap: 10px;")
        more_row = ui.row().classes("w-full justify-center")

    def refresh():
        rows, total = _rows((search.value or "").strip(), desc_toggle.value,
                            state["limit"])
        shown = len(rows)
        count_lbl.set_text(f"{total} researched"
                           + (f" · showing {shown}" if shown < total else ""))
        grid.clear()
        with grid:
            for r in rows:
                flags = _loads(r["culture_flags"], [])
                staffing = _is_staffing_agency(flags, r["company_summary"] or "")
                seeded = not r["hunted"] and (r["contacts"] or "[]") in ("", "[]")
                with ui.element("div").classes("card").style(
                        "display:flex; flex-direction:column; gap:6px; "
                        "min-width:0;"):
                    with ui.row().classes("items-center")\
                            .style("gap:6px; flex-wrap:wrap;"):
                        ui.label(r["name"] or "?")\
                            .style("font-weight:700; font-size:14.5px;")
                        ui.space()
                        if seeded:
                            # so it's always clear what you researched vs what
                            # came free from the hosted pool
                            ui.html('<span class="pill" style="border:1px solid '
                                    'var(--accent); color:var(--accent);">'
                                    'SERVER INTEL</span>')
                    with ui.row().style("gap:4px; flex-wrap:wrap;"):
                        if staffing:
                            ui.html('<span class="pill pill-bad">STAFFING</span>')
                        else:
                            ui.html(signal_pill(r["hiring_signal"]))
                    ui.label(r["company_summary"] or "")\
                        .style("font-size:12.5px; line-height:1.5; "
                               "color:var(--text-dim); display:-webkit-box; "
                               "-webkit-line-clamp:4; -webkit-box-orient:vertical; "
                               "overflow:hidden;")
                    stack = _loads(r["real_stack"], [])
                    if stack:
                        ui.label(", ".join(map(str, stack[:6])))\
                            .style("color:var(--text-faint); font-size:11.5px;")
                    ui.space()
                    links = []
                    if r["domain"]:
                        links.append(f'<a href="https://{_esc(r["domain"])}" '
                                     f'target="_blank" style="color:var(--accent);">'
                                     f'{_esc(r["domain"])}</a>')
                    for s in _loads(r["sources"], [])[:2]:
                        if isinstance(s, dict) and s.get("url"):
                            links.append(f'<a href="{_esc(s["url"])}" '
                                         f'target="_blank" '
                                         f'style="color:var(--text-dim);">'
                                         f'{_esc(s.get("label") or "source")}</a>')
                    ui.html(f'<div style="font-size:11px; color:var(--text-faint);">'
                            + (" · ".join(links) + " · " if links else "")
                            + f'{_esc(fmt_ts(r["researched_at"], 10))}</div>')
        more_row.clear()
        if shown < total:
            with more_row:
                ui.button(f"Load {min(PAGE, total - shown)} more",
                          on_click=_more).classes("btn-ghost")\
                    .style("font-size:12px;")

    def _more():
        state["limit"] += PAGE
        refresh()

    def _new_search():
        state["limit"] = PAGE          # a fresh query starts from the top again
        refresh()

    def _showing() -> bool:
        return is_active() if is_active else True

    async def _scroll_watch():
        """Pull the next page in when you get near the bottom. Everything here
        is best-effort: a timer can fire once more after its slot is gone (the
        client navigated or refreshed), and that must stay silent."""
        try:
            if not _showing() or time.time() - state["grew_at"] < 1.5:
                return
            near_bottom = await ui.run_javascript(
                "window.scrollY + window.innerHeight >"
                " document.body.scrollHeight - 400", timeout=2.0)
            if near_bottom:
                state["grew_at"] = time.time()
                _more()
        except Exception:
            pass

    def _idle_watch():
        """Away from the tab for a while: shrink back so an idle session
        isn't holding thousands of rows in the DOM."""
        try:
            # don't poll the client while you're on another tab
            scroll_timer.active = _showing()
            if _showing():
                state["left_at"] = 0.0
                return
            if state["left_at"] == 0.0:
                state["left_at"] = time.time()
            elif (time.time() - state["left_at"] > RESET_AFTER_S
                    and state["limit"] > PAGE):
                state["limit"] = PAGE
                state["left_at"] = 0.0
                refresh()
        except Exception:
            pass

    search.on("keydown.enter", _new_search)
    search.on("clear", _new_search)
    desc_toggle.on("update:model-value", _new_search)
    scroll_timer = ui.timer(2.0, _scroll_watch)
    ui.timer(10.0, _idle_watch)
    refresh()
