"""
dashboard.py

NiceGUI dashboard for HunterJobs ATS — application entry point.

Run with:   python dashboard.py
Then open:  http://localhost:8080

The UI lives in the ui/ package (theme, helpers, db_queries, jobs, tabs); this
module just wires it together: bootstraps config/DB/heartbeat, injects the theme
CSS, defines the single @ui.page("/") that assembles the tabs, and runs the
server.
"""

from __future__ import annotations

from nicegui import ui

from core.database import init_db
import core.runner_status as runner_status
from core.config import load_config
from pipeline.process_control import start_heartbeat

from ui.theme import PALETTE_CSS, _logo_html, _LOGO_SMALL
from ui.helpers import status_dot_class
from ui.jobs import render_jobs_tab
from ui.tabs import (
    render_applied_tab, render_market_tab, render_logs_tab, render_setup_tab,
)


# ── load config & init DB ─────────────────────────────────────────────────────
init_db()
CFG = load_config()
runner_status.dashboard_heartbeat()  # write first heartbeat before any brain starts

# Background heartbeat thread: runs for the lifetime of the dashboard process.
# Lives in pipeline.process_control; the brains check dashboard_is_alive() and
# self-terminate when the heartbeat stops being refreshed.
_hb_thread = start_heartbeat()


# Apply theme attribute to <html> on every page load.
# shared=True applies these to every @ui.page (we only have one, but v2 requires
# being explicit about scope when add_head_html is called at module level).
ui.add_head_html(f"<style>{PALETTE_CSS}</style>", shared=True)
ui.add_body_html(
    f"<script>document.documentElement.setAttribute('data-theme', '{CFG['theme']}');</script>",
    shared=True,
)


# ══════════════════════════════════════════════════════════════════════════════
# PAGE
# ══════════════════════════════════════════════════════════════════════════════
@ui.page("/")
def index():
    # Re-apply theme on every page render (in case it changed).
    cfg = load_config()
    ui.run_javascript(
        f"document.documentElement.setAttribute('data-theme', '{cfg['theme']}');"
    )

    # ── Header ────────────────────────────────────────────────────────────────
    with ui.element("div").classes("app-header"):
        with ui.row().style("align-items: center; gap: 0;"):
            ui.html(f'<span class="app-title">{_logo_html(24)}HunterJobs ATS</span>')
            ui.html('<span class="app-sub">three-stage job intelligence pipeline</span>')
        # right-side: live status
        status_label = ui.html("").style("font-family: 'JetBrains Mono', monospace; "
                                          "font-size: 12px; color: var(--text-dim);")

        def refresh_header_status():
            s = runner_status.read_status()
            b1 = s["brain1"]
            b2 = s["brain2"]
            dot1 = status_dot_class(b1["state"])
            dot2 = status_dot_class(b2["state"])
            status_label.set_content(
                f'<span class="status-dot {dot1}"></span>brain1: {b1["state"]} '
                f'&nbsp;&nbsp;<span class="status-dot {dot2}"></span>brain2: {b2["state"]}'
            )

        refresh_header_status()
        ui.timer(2.0, refresh_header_status)

    # ── Tabs ──────────────────────────────────────────────────────────────────
    with ui.tabs().classes("w-full") as tabs:
        t_jobs    = ui.tab("Jobs")
        t_applied = ui.tab("Applied")
        t_market  = ui.tab("Market Analyzer")
        t_logs    = ui.tab("Logs")
        t_setup   = ui.tab("Setup")

    with ui.tab_panels(tabs, value=t_jobs).classes("w-full").style(
        "background: var(--bg);"
    ):
        # ──────────────────────────────────────────────────────────────────────
        # JOBS TAB
        # ──────────────────────────────────────────────────────────────────────
        with ui.tab_panel(t_jobs):
            render_jobs_tab()

        # ──────────────────────────────────────────────────────────────────────
        # APPLIED TAB
        # ──────────────────────────────────────────────────────────────────────
        with ui.tab_panel(t_applied):
            render_applied_tab()

        # ──────────────────────────────────────────────────────────────────────
        # MARKET ANALYZER TAB
        # ──────────────────────────────────────────────────────────────────────
        with ui.tab_panel(t_market):
            render_market_tab()

        # ──────────────────────────────────────────────────────────────────────
        # LOGS TAB
        # ──────────────────────────────────────────────────────────────────────
        with ui.tab_panel(t_logs):
            render_logs_tab()

        # ──────────────────────────────────────────────────────────────────────
        # SETUP TAB
        # ──────────────────────────────────────────────────────────────────────
        with ui.tab_panel(t_setup):
            render_setup_tab()


# ══════════════════════════════════════════════════════════════════════════════
# RUN
# ══════════════════════════════════════════════════════════════════════════════
if __name__ in {"__main__", "__mp_main__"}:
    favicon = str(_LOGO_SMALL) if _LOGO_SMALL.exists() else "🎯"
    ui.run(
        title="HunterJobs ATS",
        port=8080,
        reload=False,
        favicon=favicon,
        dark=None,  # we manage theme via data-theme attribute
        show=False,
    )
