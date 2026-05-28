import os
import sys

# Ensure the repo root is on sys.path so tests can import the top-level
# modules (brain1, brain2_chat, etc.) regardless of pytest's rootdir logic.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
