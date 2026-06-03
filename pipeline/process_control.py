"""
pipeline/process_control.py

Process / IPC concerns for HunterJobs, extracted from dashboard.py: launching
the detached brain subprocesses, checking/killing their PIDs, and the dashboard
heartbeat loop. The UI layer no longer owns subprocess wiring.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import core.runner_status as runner_status

# process_control.py lives in pipeline/, so the repo root is two parents up.
# Anchored explicitly (not CWD-relative) so the detached brain processes and
# their `-m` module resolution work regardless of the launching process's CWD.
_ROOT = Path(__file__).resolve().parent.parent


# ── subprocess spawning (detached, refresh-safe) ──────────────────────────────
def spawn_detached(module: str) -> None:
    """Launch a python module (e.g. "pipeline.run_brain1") as a fully detached
    process via `python -m`. On Windows this survives parent shutdown / browser
    refresh; no console window appears.

    We invoke with `-m <module>` and pin cwd to the repo root so the
    pipeline.* / core.* package imports resolve no matter where the dashboard
    was launched from."""
    cmd = [sys.executable, "-m", module]
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
            cwd=str(_ROOT),
        )
    else:
        subprocess.Popen(
            cmd, start_new_session=True, close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(_ROOT),
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


# ── dashboard heartbeat ───────────────────────────────────────────────────────
# Background heartbeat thread: runs for the lifetime of the dashboard process.
# Daemon=True means it dies automatically when the main process exits — exactly
# what we want, since the brains check dashboard_is_alive() and self-terminate
# when the heartbeat stops being refreshed.
def _heartbeat_loop():
    while True:
        try:
            runner_status.dashboard_heartbeat()
        except Exception:
            pass
        time.sleep(5)


def start_heartbeat() -> threading.Thread:
    """Start the daemon heartbeat thread and return it."""
    t = threading.Thread(target=_heartbeat_loop, daemon=True, name="dashboard-heartbeat")
    t.start()
    return t
