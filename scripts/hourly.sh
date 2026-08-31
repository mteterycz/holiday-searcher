#!/usr/bin/env bash
# Cogodzinny cykl dla launchd: sprawdź ceny, potem odśwież stronę na GitHub Pages.
# Publikacja nie może wywrócić monitoringu, więc jej błąd tylko logujemy.
set -uo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=src
PY="/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"

echo "=== $(date '+%Y-%m-%d %H:%M') monitor ==="
"$PY" -m holiday_searcher.cli monitor wrzesien-okazje --limit 150 || echo "monitor: BŁĄD"

echo "--- publikacja na GitHub Pages ---"
bash scripts/deploy-pages.sh || echo "deploy: BŁĄD (dane w bazie są bezpieczne)"
