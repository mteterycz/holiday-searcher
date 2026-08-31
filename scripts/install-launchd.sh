#!/bin/bash
# Instaluje harmonogram launchd dla `hs monitor` (faza 2).
#
# Co robi:
#   1. kopiuje launchd/com.holiday-searcher.monitor.plist do ~/Library/LaunchAgents/
#   2. wyładowuje starą wersję zadania, jeśli była już załadowana (bootout)
#   3. ładuje nową wersję (bootstrap) i włącza ją
#
# Użycie:
#   scripts/install-launchd.sh
#
# Uwaga: ten skrypt NIE jest uruchamiany automatycznie — trzeba go odpalić
# ręcznie, świadomie, po skonfigurowaniu config/.env (patrz docs/faza2-monitoring.md).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.holiday-searcher.monitor"
PLIST_SRC="${REPO_ROOT}/launchd/${LABEL}.plist"
PLIST_DST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
UID_NUM="$(id -u)"

if [[ ! -f "${PLIST_SRC}" ]]; then
    echo "Nie znaleziono ${PLIST_SRC}" >&2
    exit 1
fi

mkdir -p "${HOME}/Library/LaunchAgents"
mkdir -p "${REPO_ROOT}/data"

echo "Kopiuję ${PLIST_SRC} -> ${PLIST_DST}"
cp "${PLIST_SRC}" "${PLIST_DST}"

echo "Wyładowuję poprzednią wersję (jeśli była)…"
launchctl bootout "gui/${UID_NUM}" "${PLIST_DST}" 2>/dev/null || true

echo "Ładuję zadanie…"
launchctl bootstrap "gui/${UID_NUM}" "${PLIST_DST}"
launchctl enable "gui/${UID_NUM}/${LABEL}"

echo "Gotowe. Sprawdź status: launchctl list | grep holiday-searcher"
echo "Log:   tail -f ${REPO_ROOT}/data/monitor.log"
echo "Ręczne uruchomienie od razu: launchctl kickstart -k gui/${UID_NUM}/${LABEL}"
