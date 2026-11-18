#!/bin/bash
# Sets up a local Python environment on first run, then starts the Mac client.
# Usage: ./start_mac.sh            (auto-discovers the PC)
#        ./start_mac.sh --host 192.168.1.20
set -e
cd "$(dirname "$0")"
if [ ! -x ".venv/bin/python" ]; then
    echo "First run: creating Python environment..."
    if ! command -v python3 >/dev/null; then
        echo "python3 not found. Run: xcode-select --install   (or install Python from python.org)"
        exit 1
    fi
    python3 -m venv .venv
    .venv/bin/python -m pip install --quiet --upgrade pip
    .venv/bin/python -m pip install --quiet -r requirements-mac.txt
fi
exec .venv/bin/python mac_client.py "$@"
