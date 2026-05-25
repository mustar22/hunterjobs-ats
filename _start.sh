#!/usr/bin/env bash
# HunterJobs ATS — Linux/macOS quick start
# Run: ./_start.sh  (you may need to chmod +x _start.sh first)

set -e

cd "$(dirname "$0")"

if [ ! -f "keys.py" ]; then
    echo
    echo "  [!] keys.py not found."
    echo "      Copy keys_dummy.py to keys.py and add your API key first."
    echo "      Get a free Google API key at https://aistudio.google.com/apikey"
    echo
    exit 1
fi

echo "Starting HunterJobs ATS..."
echo "Open http://localhost:8080 in your browser."
echo "Press Ctrl+C to stop."
echo

# Prefer python3, fall back to python
if command -v python3 >/dev/null 2>&1; then
    python3 dashboard.py
else
    python dashboard.py
fi
