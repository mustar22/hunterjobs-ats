"""One-shot HunterJobs setup: clone/update the YC scraper, editable-install
everything, seed config. Idempotent; works on Linux and Windows.

Usage: python scripts/setup.py
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

HJ_ROOT = Path(__file__).resolve().parents[1]
SCRAPER_URL = "https://github.com/mustar22/ycombinator-jobs-scraper.git"
SCRAPER_DIR = HJ_ROOT.parent / "ycombinator-jobs-scraper"


def step(msg):
    print(f"\n== {msg}")


def run(cmd):
    cmd = [str(c) for c in cmd]
    print(f"   $ {' '.join(cmd)}")
    if subprocess.run(cmd).returncode != 0:
        sys.exit(f"\nFAILED: {' '.join(cmd)}\nFix the error above and re-run.")


def main():
    if sys.version_info < (3, 10):
        sys.exit(f"Python 3.10+ required (this is {sys.version.split()[0]})")
    in_venv = sys.prefix != sys.base_prefix
    print(f"python: {sys.executable}")
    print(f"venv:   {'active' if in_venv else 'NONE — will install into this interpreter'}")
    if not in_venv:
        print("        (ctrl-C and activate a venv first if you don't want that)")
    if not shutil.which("git"):
        sys.exit("git not found on PATH — install git first")

    step(f"YC scraper repo -> {SCRAPER_DIR}")
    if (SCRAPER_DIR / ".git").exists():
        run(["git", "-C", SCRAPER_DIR, "pull", "--ff-only"])
    elif SCRAPER_DIR.exists():
        sys.exit(f"{SCRAPER_DIR} exists but is not a git clone — move it aside and re-run.")
    else:
        run(["git", "clone", SCRAPER_URL, SCRAPER_DIR])

    pip = [sys.executable, "-m", "pip", "install"]
    step("Installing HunterJobs (editable) + dependencies")
    run(pip + ["-e", HJ_ROOT])

    step("Installing YC scraper (editable)")
    run(pip + ["-e", SCRAPER_DIR])

    step("Verifying scraper imports from local source")
    r = subprocess.run(
        [sys.executable, "-c", "import ycombinator_jobs_scraper as m; print(m.__file__)"],
        capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"scraper import failed:\n{r.stderr}")
    mod = Path(r.stdout.strip()).resolve()
    if mod.is_relative_to(SCRAPER_DIR.resolve()):
        print(f"   OK: {mod}")
    else:
        print(f"   WARNING: scraper loads from {mod}")
        print(f"   That is NOT the local clone — scraper edits will be ignored.")
        print(f"   Try: {sys.executable} -m pip uninstall ycombinator-jobs-scraper, then re-run.")

    step("Seeding config.json / keys.py (existing files left alone)")
    cfg = HJ_ROOT / "config.json"
    if cfg.exists():
        print("   config.json exists")
    else:
        sys.path.insert(0, str(HJ_ROOT))
        from core.config import DEFAULT_CONFIG
        cfg.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
        print(f"   wrote {cfg}")
    keys, dummy = HJ_ROOT / "keys.py", HJ_ROOT / "keys_dummy.py"
    if keys.exists():
        print("   keys.py exists")
    elif dummy.exists():
        shutil.copyfile(dummy, keys)
        print("   created keys.py from keys_dummy.py — add your API keys")
    else:
        print("   WARNING: no keys.py and no keys_dummy.py to copy from")

    print("\nSetup complete. Start the dashboard with: python dashboard.py")
    print("Note: a bare 'pip install -e .' does NOT install the YC scraper —")
    print("re-run this script to set up or update it.")


if __name__ == "__main__":
    main()
