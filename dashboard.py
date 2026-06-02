"""
dashboard.py

NiceGUI dashboard for HunterJobs ATS.

Run with:   python dashboard.py
Then open:  http://localhost:8080

Design: minimalist tooling aesthetic. Dark navy/gray surfaces with gothic
purple accents. Light theme toggle in Setup. Geist for UI, JetBrains Mono
for code/logs/IDs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nicegui import ui, app

# Logo: served from /static/ if present in the gogo_logo/ folder next to this file.
# Falls back gracefully to a colored dot if the file isn't there.
_LOGO_DIR = Path(__file__).resolve().parent / "gogo_logo"
_LOGO_SMALL = _LOGO_DIR / "HJ_112.png"
_LOGO_LARGE = _LOGO_DIR / "HJ_576.png"
if _LOGO_DIR.exists():
    app.add_static_files("/static/gogo_logo", str(_LOGO_DIR))


def _logo_html(size_px: int = 24) -> str:
    """Returns the HTML snippet for the logo (img tag) or a fallback dot."""
    if _LOGO_SMALL.exists():
        return (
            f'<img src="/static/gogo_logo/HJ_112.png" '
            f'alt="HunterJobs" '
            f'style="height: {size_px}px; width: {size_px}px; '
            f'object-fit: contain; margin-right: 10px; vertical-align: middle;" />'
        )
    return '<span class="dot"></span>'

import database  # for live RAG_AVAILABLE flag
from database import get_db_connection, init_db
import embeddings  # RAG: similar past applications
import runner_status
import brain1  # for enrich_company_for_job / find_contact_for_job
import brain2_chat  # for chat interface

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
LOG_PATH = ROOT / "hunterjobs.log"


# ── config ────────────────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "theme": "dark",
    "profile": "",
    "search_terms": "machine learning engineer remote\ngenerative AI engineer remote",
    "hard_rejects": "US citizenship required\nW2 only\nsecurity clearance",
    "salary_floor": 4500,
    "sources": ["linkedin"],
    # YC startups are company-based, scraped separately from JobSpy sites.
    "use_yc": False,
    "yc_max_companies": 100,
    "yc_max_team_size": 50,
    "yc_years_back": 3,
    "yc_remote_only": True,
    "results_wanted": 100,
    "hours_old": 72,
    "brain1_backend": "gemma",
    "brain1_stage1_backend": "gemma",
    "brain1_stage23_backend": "gemma",
    "brain1_lmstudio_url": "http://localhost:1234/v1",
    "brain1_lmstudio_model": "",
    "brain2_backend": "gemini",
    "brain2_gemini_model": "gemini-3.5-flash",
    "brain2_gemma_model": "gemma-4-26b-a4b-it",
    "brain2_anthropic_model": "claude-sonnet-4-6",
    "brain2_openai_model": "gpt-5.5",
    "brain2_lmstudio_url": "http://localhost:1234/v1",
    "brain2_lmstudio_model": "",
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        # fill missing keys
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
        }
    except ImportError:
        return {"google": "", "anthropic": "", "github": "", "openai": ""}


# ── subprocess spawning (detached, refresh-safe) ──────────────────────────────
def spawn_detached(script: str) -> None:
    """Launch a python script as a fully detached process. On Windows this
    survives parent shutdown / browser refresh. No console window appears."""
    cmd = [sys.executable, str(ROOT / script)]
    if os.name == "nt":
        flags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        subprocess.Popen(
            cmd, creationflags=flags, close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(ROOT),
        )
    else:
        subprocess.Popen(
            cmd, start_new_session=True, close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(ROOT),
        )


def _is_pid_alive(pid: int | None) -> bool:
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


def kill_pid(pid: int | None) -> bool:
    """Cross-platform process kill. Returns True if killed (or already dead)."""
    if not pid:
        return False
    try:
        if os.name == "nt":
            # /F = force, /T = kill child tree too
            result = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, text=True, timeout=10,
            )
            return result.returncode == 0 or "not found" in result.stderr.lower()
        else:
            os.kill(pid, 15)  # SIGTERM
            return True
    except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired):
        return False
    except Exception:
        return False


# ── DB helpers ────────────────────────────────────────────────────────────────
def fetch_jobs(verdicts: list[str], query: str = "", limit: int = 300) -> list[dict]:
    if not verdicts:
        return []
    conn = get_db_connection()
    try:
        params: list = []
        if query.strip():
            sql = "SELECT j.* FROM jobs j JOIN jobs_fts f ON j.rowid = f.rowid WHERE "
            safe_q = query.replace('"', '""')
            sql += "jobs_fts MATCH ? AND "
            params.append(f'"{safe_q}"*')
        else:
            sql = "SELECT * FROM jobs WHERE "
        placeholders = ",".join("?" for _ in verdicts)
        sql += f"verdict IN ({placeholders}) AND (applied IS NULL OR applied = 0) "
        sql += "ORDER BY date_scraped DESC LIMIT ?"
        params.extend(verdicts)
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def fetch_applied() -> list[dict]:
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE applied=1 ORDER BY applied_date DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_applied(job_id: str) -> None:
    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE jobs SET applied=1, applied_date=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), job_id),
        )
        conn.commit()
    finally:
        conn.close()


def unmark_applied(job_id: str) -> None:
    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE jobs SET applied=0, applied_date=NULL WHERE id=?", (job_id,)
        )
        conn.commit()
    finally:
        conn.close()


def update_notes(job_id: str, notes: str) -> None:
    conn = get_db_connection()
    try:
        conn.execute("UPDATE jobs SET notes=? WHERE id=?", (notes or "", job_id))
        conn.commit()
    finally:
        conn.close()


def update_row_color(job_id: str, color: str) -> None:
    """Set the user's color label for a job. color: '' (none) or one of
    purple/green/amber/red/blue/gray."""
    if color and color not in ("purple", "green", "amber", "red", "blue", "gray"):
        return
    conn = get_db_connection()
    try:
        conn.execute("UPDATE jobs SET row_color=? WHERE id=?", (color or "", job_id))
        conn.commit()
    finally:
        conn.close()


def update_verdict(job_id: str, verdict: str, reason: str = "") -> None:
    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE jobs SET verdict=?, reject_reason=? WHERE id=?",
            (verdict, reason, job_id),
        )
        conn.commit()
    finally:
        conn.close()


COLOR_SWATCHES = [
    ("",       "swatch-none",   "No color"),
    ("purple", "swatch-purple", "Follow up"),
    ("green",  "swatch-green",  "Interview"),
    ("amber",  "swatch-amber",  "Waiting"),
    ("red",    "swatch-red",    "Reject"),
    ("blue",   "swatch-blue",   "Researched"),
    ("gray",   "swatch-gray",   "Deprioritized"),
]


# ── theme palette ─────────────────────────────────────────────────────────────
PALETTE_CSS = """
:root[data-theme="dark"] {
  --bg:         #0f1117;
  --surface:    #1a1d28;
  --surface-2:  #232735;
  --border:     #2a2e3a;
  --text:       #e8e8ec;
  --text-dim:   #888a96;
  --text-faint: #5c5f6b;
  --accent:     #9d6fff;
  --accent-2:   #b794ff;
  --good:       #52d6a4;
  --maybe:      #d6b04a;
  --bad:        #d65a5a;
  --good-bg:    rgba(82,214,164,0.10);
  --maybe-bg:   rgba(214,176,74,0.10);
  --bad-bg:     rgba(214,90,90,0.10);
  --accent-bg:  rgba(157,111,255,0.10);
}
:root[data-theme="light"] {
  --bg:         #fafafb;
  --surface:    #ffffff;
  --surface-2:  #f4f5f8;
  --border:     #e4e6ec;
  --text:       #1a1d28;
  --text-dim:   #6b7080;
  --text-faint: #9aa0ad;
  --accent:     #7a4fd9;
  --accent-2:   #5e34c4;
  --good:       #2a9d6b;
  --maybe:      #b8862a;
  --bad:        #c14545;
  --good-bg:    rgba(42,157,107,0.08);
  --maybe-bg:   rgba(184,134,42,0.08);
  --bad-bg:     rgba(193,69,69,0.08);
  --accent-bg:  rgba(122,79,217,0.08);
}

@import url('https://fonts.googleapis.com/css2?family=Geist:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');

html, body, .q-page, .q-layout, .q-tab-panels, .q-tab-panel {
  background: var(--bg) !important;
  color: var(--text) !important;
  font-family: 'Geist', system-ui, sans-serif !important;
}
body { letter-spacing: -0.01em; }
.mono { font-family: 'JetBrains Mono', monospace; }

/* ── Force Quasar form components to follow our theme ─────────────────────── */
.q-field__native, .q-field__input, .q-field__control,
.q-field__label, .q-field__marginal,
input, textarea, select {
  color: var(--text) !important;
}
.q-field--outlined .q-field__control {
  background: var(--surface) !important;
}
.q-field--outlined .q-field__control:before {
  border-color: var(--border) !important;
}
.q-field--outlined.q-field--focused .q-field__control:after {
  border-color: var(--accent) !important;
}
.q-field__label {
  color: var(--text-dim) !important;
}

/* Quasar select dropdown menu */
.q-menu, .q-list, .q-item {
  background: var(--surface) !important;
  color: var(--text) !important;
}
.q-item:hover, .q-item--active {
  background: var(--surface-2) !important;
}
.q-item__label {
  color: var(--text) !important;
}

/* Expansion (q-expansion-item) */
.q-expansion-item__container {
  background: var(--surface) !important;
}
.q-expansion-item__toggle-icon,
.q-icon, .q-checkbox__inner {
  color: var(--text-dim) !important;
}

/* Quasar dialogs */
.q-dialog__inner > div, .q-card {
  background: var(--surface) !important;
  color: var(--text) !important;
  border: 1px solid var(--border) !important;
}

/* Notifications — make them readable on both themes */
.q-notification {
  color: var(--text) !important;
}
.q-notification--standard {
  background: var(--surface) !important;
  border: 1px solid var(--border) !important;
}

/* Tooltip */
.q-tooltip {
  background: var(--surface-2) !important;
  color: var(--text) !important;
  border: 1px solid var(--border) !important;
}

/* Select dropdown popup */
.q-virtual-scroll__content {
  background: var(--surface) !important;
}

/* Notify (toast) */
.q-notification {
  background: var(--surface) !important;
  color: var(--text) !important;
  border: 1px solid var(--border) !important;
}

/* Dialog */
.q-dialog .q-card {
  background: var(--surface) !important;
  color: var(--text) !important;
}

/* Checkbox label */
.q-checkbox__label {
  color: var(--text) !important;
}

/* Tooltip */
.q-tooltip {
  background: var(--surface-2) !important;
  color: var(--text) !important;
  border: 1px solid var(--border) !important;
}

/* Header */
.app-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 18px 28px; border-bottom: 1px solid var(--border);
  background: var(--bg);
}
.app-title {
  font-weight: 600; font-size: 17px; letter-spacing: -0.02em;
  color: var(--text);
}
.app-title .dot {
  display: inline-block; width: 8px; height: 8px; border-radius: 50%;
  background: var(--accent); margin-right: 10px; vertical-align: middle;
  box-shadow: 0 0 12px var(--accent);
}
.app-sub { color: var(--text-faint); font-size: 12px; margin-left: 18px; }

/* Tabs */
.q-tabs { border-bottom: 1px solid var(--border); padding: 0 16px; }
.q-tab { color: var(--text-dim) !important; font-weight: 500; font-size: 13px;
         text-transform: none !important; letter-spacing: 0; padding: 0 16px;
         min-height: 42px; }
.q-tab--active { color: var(--text) !important; }
.q-tab__indicator { background: var(--accent) !important; height: 2px !important; }

/* Cards / surfaces */
.card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 16px;
}
.card-tight { padding: 12px 14px; }

/* Job rows */
.job-row {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 14px 16px; margin-bottom: 10px;
  transition: border-color 0.15s, background 0.15s;
}
.job-row:hover { border-color: var(--accent); }
.job-title { font-weight: 600; font-size: 15px; color: var(--text);
             letter-spacing: -0.01em; line-height: 1.3; }
.job-meta { font-size: 12px; color: var(--text-dim); margin-top: 4px; }
.job-meta .mono { color: var(--text-faint); }

/* Verdict pills */
.pill { display: inline-block; padding: 2px 10px; border-radius: 999px;
        font-size: 11px; font-weight: 600; letter-spacing: 0.02em;
        font-family: 'JetBrains Mono', monospace; }
.pill-good   { color: var(--good);  background: var(--good-bg);  border: 1px solid var(--good); }
.pill-maybe  { color: var(--maybe); background: var(--maybe-bg); border: 1px solid var(--maybe); }
.pill-bad    { color: var(--bad);   background: var(--bad-bg);   border: 1px solid var(--bad); }
.pill-real   { color: var(--good);  background: var(--good-bg);  border: 1px solid var(--good); }
.pill-ghost  { color: var(--bad);   background: var(--bad-bg);   border: 1px solid var(--bad); }
.pill-unc    { color: var(--text-dim); background: var(--surface-2); border: 1px solid var(--border); }

/* Metrics */
.metric { background: var(--surface); border: 1px solid var(--border);
          border-radius: 10px; padding: 14px 16px; }
.metric .val { font-family: 'JetBrains Mono', monospace; font-size: 22px;
               font-weight: 600; color: var(--text); letter-spacing: -0.02em; }
.metric .lbl { font-size: 11px; color: var(--text-dim); margin-top: 2px;
               text-transform: uppercase; letter-spacing: 0.06em; }

/* Buttons */
.q-btn { text-transform: none !important; font-weight: 500 !important;
         letter-spacing: 0 !important; border-radius: 8px !important; }
.btn-primary {
  background: var(--accent) !important; color: white !important;
  border: 1px solid var(--accent) !important;
}
.btn-primary:hover { background: var(--accent-2) !important; }
.btn-ghost {
  background: transparent !important; color: var(--text-dim) !important;
  border: 1px solid var(--border) !important;
}
.btn-ghost:hover { color: var(--text) !important; border-color: var(--text-dim) !important; }

/* Inputs / textareas (Quasar) */
.q-field__control, .q-field__native, .q-field__label,
textarea, input { color: var(--text) !important; }
.q-field--outlined .q-field__control:before { border-color: var(--border) !important; }
.q-field--outlined .q-field__control:hover:before { border-color: var(--text-dim) !important; }
.q-field--outlined.q-field--focused .q-field__control:after {
  border-color: var(--accent) !important; border-width: 1px !important;
}

/* Log lines */
.log-line { font-family: 'JetBrains Mono', monospace; font-size: 12px;
            line-height: 1.6; color: var(--text-dim); white-space: pre-wrap;
            padding: 1px 0; }
.log-err { color: var(--bad); }
.log-ok  { color: var(--good); }
.log-warn { color: var(--maybe); }

/* Status bar */
.status-bar { display: flex; gap: 16px; align-items: center;
              background: var(--surface); border: 1px solid var(--border);
              border-radius: 10px; padding: 10px 16px; font-size: 12px; }
.status-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block;
              margin-right: 6px; vertical-align: middle; }
.status-dot.running { background: var(--accent); box-shadow: 0 0 8px var(--accent);
                      animation: pulse 1.4s ease-in-out infinite; }
.status-dot.idle    { background: var(--text-faint); }
.status-dot.done    { background: var(--good); }
.status-dot.error   { background: var(--bad); }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }

.section-title { font-size: 13px; font-weight: 600; color: var(--text-dim);
                 text-transform: uppercase; letter-spacing: 0.08em;
                 margin: 0 0 12px 0; }

/* Expansion items */
.q-expansion-item { background: transparent !important; }
.q-expansion-item__container { border: 1px solid var(--border); border-radius: 8px;
                               margin-bottom: 6px; background: var(--surface-2); }

/* Markdown / description scroll */
.desc-scroll {
  max-height: 220px; overflow-y: auto; font-size: 13px; line-height: 1.55;
  color: var(--text-dim); padding: 10px 12px; background: var(--surface-2);
  border-radius: 8px; border: 1px solid var(--border); white-space: pre-wrap;
}

/* Decree box */
.decree-box {
  background: var(--surface); border-left: 3px solid var(--accent);
  border-top: 1px solid var(--border); border-right: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
  padding: 16px 20px; border-radius: 0 8px 8px 0; font-size: 13.5px;
  line-height: 1.7; white-space: pre-wrap; color: var(--text);
}

/* Search input */
.q-field__control { background: var(--surface) !important; border-radius: 8px !important; }

/* Toggle row */
.toggle-row { display: flex; gap: 16px; align-items: center; flex-wrap: wrap; }

/* ── Per-job color labels ─────────────────────────────────────────────── */
.job-row[data-color="purple"] { border-left: 4px solid #9d6fff; }
.job-row[data-color="green"]  { border-left: 4px solid #52d6a4; }
.job-row[data-color="amber"]  { border-left: 4px solid #d6b04a; }
.job-row[data-color="red"]    { border-left: 4px solid #d65a5a; }
.job-row[data-color="blue"]   { border-left: 4px solid #5aa3d6; }
.job-row[data-color="gray"]   { border-left: 4px solid #6b7080; }

.swatch-row { display: flex; gap: 6px; align-items: center; margin-top: 6px; flex-wrap: wrap; }
.swatch {
  width: 18px; height: 18px;
  min-width: 18px; min-height: 18px;
  flex: 0 0 18px;
  border-radius: 50%; cursor: pointer;
  border: 1.5px solid var(--border);
  transition: transform 0.1s, border-color 0.1s;
  display: inline-block;
}
.swatch:hover { transform: scale(1.2); border-color: var(--text-dim); }
.swatch.active { border-color: var(--text); transform: scale(1.15); }
.swatch.swatch-none   { background: transparent;
                        background-image: linear-gradient(45deg, transparent 45%, var(--text-dim) 45%, var(--text-dim) 55%, transparent 55%); }
.swatch.swatch-purple { background: #9d6fff; }
.swatch.swatch-green  { background: #52d6a4; }
.swatch.swatch-amber  { background: #d6b04a; }
.swatch.swatch-red    { background: #d65a5a; }
.swatch.swatch-blue   { background: #5aa3d6; }
.swatch.swatch-gray   { background: #6b7080; }

/* Notes textarea */
.notes-block {
  margin-top: 12px; padding: 10px; border-radius: 8px;
  background: var(--surface-2); border: 1px solid var(--border);
}
.notes-label {
  font-size: 11px; color: var(--text-dim); text-transform: uppercase;
  letter-spacing: 0.06em; margin-bottom: 6px;
}

/* ── Brain 2 chat bubbles ─────────────────────────────────────────────── */
.chat-container {
  display: flex; flex-direction: column; gap: 12px;
  max-height: 60vh; overflow-y: auto; padding: 8px 4px;
}
.chat-msg {
  max-width: 85%; padding: 10px 14px; border-radius: 12px;
  font-size: 13.5px; line-height: 1.55; white-space: pre-wrap;
}
.chat-msg-user {
  align-self: flex-end;
  background: var(--accent-bg); border: 1px solid var(--accent);
  color: var(--text);
}
.chat-msg-assistant {
  align-self: flex-start;
  background: var(--surface); border: 1px solid var(--border);
  color: var(--text);
}
.chat-msg-tool {
  align-self: flex-start;
  background: var(--surface-2); border: 1px solid var(--border);
  color: var(--text-dim); font-family: 'JetBrains Mono', monospace;
  font-size: 11.5px; max-width: 95%;
}
.chat-msg-meta {
  font-size: 10px; color: var(--text-faint); margin-top: 4px;
  text-transform: uppercase; letter-spacing: 0.05em;
}
.chat-input-row {
  display: flex; gap: 8px; align-items: flex-end; margin-top: 12px;
  padding: 8px; background: var(--surface);
  border: 1px solid var(--border); border-radius: 10px;
}
"""


# ── load config & init DB ─────────────────────────────────────────────────────
init_db()
CFG = load_config()
runner_status.dashboard_heartbeat()  # write first heartbeat before any brain starts


# Background heartbeat thread: runs for the lifetime of the dashboard process.
# Daemon=True means it dies automatically when the main process exits — exactly
# what we want, since the brains check dashboard_is_alive() and self-terminate
# when the heartbeat stops being refreshed.
def _heartbeat_loop():
    import time as _time
    while True:
        try:
            runner_status.dashboard_heartbeat()
        except Exception:
            pass
        _time.sleep(5)


import threading as _threading
_hb_thread = _threading.Thread(target=_heartbeat_loop, daemon=True, name="dashboard-heartbeat")
_hb_thread.start()


# Apply theme attribute to <html> on every page load.
# shared=True applies these to every @ui.page (we only have one, but v2 requires
# being explicit about scope when add_head_html is called at module level).
ui.add_head_html(f"<style>{PALETTE_CSS}</style>", shared=True)
ui.add_body_html(
    f"<script>document.documentElement.setAttribute('data-theme', '{CFG['theme']}');</script>",
    shared=True,
)


# ── helpers for UI ────────────────────────────────────────────────────────────
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
# JOBS TAB
# ══════════════════════════════════════════════════════════════════════════════
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
        spawn_detached("run_brain1.py")
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


# ══════════════════════════════════════════════════════════════════════════════
# APPLIED TAB
# ══════════════════════════════════════════════════════════════════════════════
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
            spawn_detached("run_brain2.py")
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
                      on_click=lambda _: (spawn_detached("run_brain1.py"),
                                          ui.notify("Brain 1 started.")))\
                .classes("btn-ghost")
            ui.button("Wake Brain 2",
                      on_click=lambda _: (spawn_detached("run_brain2.py"),
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
