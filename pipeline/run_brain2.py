"""Standalone runner for Brain 2. Invoked by the dashboard as a detached
subprocess, and usable directly from the CLI for testing:

    python -m pipeline.run_brain2
"""
from pipeline.brain2 import run_brain2

if __name__ == "__main__":
    run_brain2()
