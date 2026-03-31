#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# Install dependencies if needed
pip install -q -r requirements.txt

# Run the sync script, forwarding all arguments
python3 sync_clockify.py "$@"
