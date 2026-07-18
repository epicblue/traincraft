#!/usr/bin/env sh
set -e
cd "$(dirname "$0")"
python3 -m pip install -r requirements.txt
exec python3 server.py --host 0.0.0.0 --port 8765
