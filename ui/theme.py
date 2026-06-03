"""
ui/theme.py

Visual layer extracted from dashboard.py: logo helpers + the /static mount,
color-swatch definitions, and the full palette CSS. theme.py lives in ui/, so
the gogo_logo dir is re-anchored to the repo root (two parents up).
"""

from __future__ import annotations

from pathlib import Path

from nicegui import app

# Logo: served from /static/ if present in the gogo_logo/ folder at the repo
# root. Falls back gracefully to a colored dot if the file isn't there.
_LOGO_DIR = Path(__file__).resolve().parent.parent / "gogo_logo"
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
