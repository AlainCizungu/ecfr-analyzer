#!/usr/bin/env bash
# run.sh — First-time setup and launch
set -e

echo "==> Installing dependencies…"
pip install -r requirements.txt --quiet

if [ ! -f ecfr.db ] || [ "$1" = "--refresh" ]; then
  echo "==> Downloading eCFR data (this takes several minutes)…"
  echo "    Tip: use '--titles 1 2 3' to download only specific titles for a quick test."
  python downloader.py "${@:2}"
else
  echo "==> Using existing ecfr.db (pass --refresh to re-download)"
fi

echo "==> Starting server at http://localhost:8000"
uvicorn api:app --host 0.0.0.0 --port 8000
