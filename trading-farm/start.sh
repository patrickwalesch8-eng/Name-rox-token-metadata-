#!/usr/bin/env bash
# Aktien Data Farm - Start fuer macOS/Linux
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements.txt
python farm.py all
