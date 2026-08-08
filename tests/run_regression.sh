#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
python3 tests/run_all.py
