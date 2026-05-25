"""
runner_status.py

Tiny, atomic, file-backed status board shared between background workers
(brain1, brain2) and the dashboard. No locks needed: writes go to a temp
file and are then atomically renamed onto the target path.

Status shape:
{
  "brain1": {
    "state":    "idle" | "running" | "done" | "error",
    "started":  ISO-8601 or null,
    "updated":  ISO-8601 or null,
    "stage1":   "scraping 'AI engineer'..." | "filter 23/100" | "idle",
    "stage2":   "researching CompanyX (3 in queue)" | "idle",
    "stage3":   "outreach for CompanyX (1 in queue)" | "idle",
    "scraped":  int,
    "good":     int,
    "maybe":    int,
    "bad":      int,
    "hard_rej": int,
    "error":    str | null
  },
  "brain2": {
    "state":    "idle" | "running" | "done" | "error",
    "started":  ISO-8601 or null,
    "updated":  ISO-8601 or null,
    "phase":    "aggregating" | "calling gemini" | "writing" | "idle",
    "error":    str | null
  }
}
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATUS_PATH = Path(__file__).resolve().parent / "runner_status.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _default() -> dict[str, Any]:
    return {
        "dashboard_heartbeat": None,
        "brain1": {
            "state": "idle", "started": None, "updated": None,
            "pid": None,
            "stage1": "idle", "stage2": "idle", "stage3": "idle",
            "scraped": 0, "good": 0, "maybe": 0, "bad": 0, "hard_rej": 0,
            "error": None,
        },
        "brain2": {
            "state": "idle", "started": None, "updated": None,
            "pid": None,
            "phase": "idle", "error": None,
        },
    }


def read_status() -> dict[str, Any]:
    if not STATUS_PATH.exists():
        return _default()
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _default()


def is_stale(brain_status: dict[str, Any], max_age_seconds: int = 90) -> bool:
    """Return True if a 'running' status hasn't been updated recently.
    Used by the dashboard to detect crashed/killed workers."""
    if brain_status.get("state") != "running":
        return False
    updated = brain_status.get("updated")
    if not updated:
        return True
    try:
        ts = datetime.fromisoformat(updated)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        return age > max_age_seconds
    except (ValueError, TypeError):
        return True


def _atomic_write(data: dict[str, Any]) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Retry os.replace because Windows can fail if the target file is briefly
    # held open by another reader (dashboard + brain heartbeat thread both
    # touch this file).
    last_err = None
    for attempt in range(5):
        fd, tmp = tempfile.mkstemp(
            prefix=".runner_status.", suffix=".tmp", dir=str(STATUS_PATH.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, STATUS_PATH)
            return
        except OSError as e:
            last_err = e
            try:
                os.unlink(tmp)
            except OSError:
                pass
            # Tiny backoff and retry
            import time as _t
            _t.sleep(0.02 * (attempt + 1))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    # Exhausted retries; log but don't crash the caller.
    try:
        import logging
        logging.getLogger(__name__).warning(
            f"runner_status: atomic_write failed after retries: {last_err}"
        )
    except Exception:
        pass


def patch(brain: str, **fields: Any) -> None:
    """Merge `fields` into status[brain] and refresh `updated`."""
    if brain not in ("brain1", "brain2"):
        raise ValueError(f"Unknown brain: {brain}")
    data = read_status()
    if brain not in data:
        data[brain] = _default()[brain]
    data[brain].update(fields)
    data[brain]["updated"] = _now()
    _atomic_write(data)


def start(brain: str) -> None:
    now = _now()
    patch(brain, state="running", started=now, updated=now, error=None)


def finish(brain: str, error: str | None = None) -> None:
    patch(brain, state=("error" if error else "done"), error=error)


def reset() -> None:
    _atomic_write(_default())


def dashboard_heartbeat() -> None:
    """Called by the dashboard every few seconds to prove it's alive."""
    data = read_status()
    data["dashboard_heartbeat"] = _now()
    _atomic_write(data)


def dashboard_is_alive(max_age_seconds: int = 15) -> bool:
    """Brains call this; if False, they should self-terminate."""
    data = read_status()
    hb = data.get("dashboard_heartbeat")
    if not hb:
        return False
    try:
        ts = datetime.fromisoformat(hb)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        return age <= max_age_seconds
    except (ValueError, TypeError):
        return False


if __name__ == "__main__":
    print(json.dumps(read_status(), indent=2))
