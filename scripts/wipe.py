#!/usr/bin/env python3
"""Dev tool: wipe personal data (DB, logs, profile, stale runtime state) before pushing. Run manually."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import CONFIG_PATH
from core.database import DB_PATH
from core.runner_status import STATUS_PATH


def _delete_targets() -> list[Path]:
    db_files = [DB_PATH, Path(f"{DB_PATH}-wal"), Path(f"{DB_PATH}-shm")]
    return db_files + [
        ROOT / "hunterjobs.log",
        STATUS_PATH,                      # core/runner_status.json (live)
        ROOT / "runner_status.json",      # stale pre-refactor copy, if any
        ROOT / ".yc_ats_cache.json",
    ]


def main() -> None:
    targets = _delete_targets()
    print(f"Wipe plan (repo root: {ROOT}):")
    for p in targets:
        print(f"  - {'delete' if p.exists() else 'skip (missing)'}: {p}")
    clear_profile = CONFIG_PATH.exists()
    print(f"  - {'clear profile + persona in' if clear_profile else 'skip config (missing):'} {CONFIG_PATH}")

    try:
        reply = input("\nProceed? [y/N] ").strip().lower()
    except EOFError:
        reply = ""
    if reply != "y":
        print("Aborted. Nothing changed.")
        return

    for p in targets:
        if not p.exists():
            continue
        try:
            p.unlink()
            print(f"deleted {p}")
        except OSError as e:
            print(f"could not delete {p}: {e}")

    # Only the personal fields — leave every other config key untouched.
    if clear_profile:
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg["profile"] = ""
            cfg["brain2_persona"] = ""
            CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
            print(f"cleared profile + persona in {CONFIG_PATH}")
        except (OSError, json.JSONDecodeError) as e:
            print(f"could not clear profile/persona: {e}")

    print("Done.")


if __name__ == "__main__":
    main()
