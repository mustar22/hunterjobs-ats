"""Standalone runner for Brain 1. Invoked by the dashboard as a detached
subprocess, and usable directly from the CLI for testing:

    python run_brain1.py
"""
from brain1 import run_brain1

if __name__ == "__main__":
    run_brain1()
