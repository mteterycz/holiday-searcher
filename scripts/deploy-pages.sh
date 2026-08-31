#!/usr/bin/env bash
# Publikuje statyczny snapshot panelu na GitHub Pages (gałąź gh-pages).
# Gałąź zawiera WYŁĄCZNIE wygenerowany katalog dist/ — nigdy kodu, bazy ani sekretów.
set -euo pipefail
cd "$(dirname "$0")/.."

export PYTHONPATH=src
PY="/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"
"$PY" -m holiday_searcher.cli export --out dist >/dev/null

# .nojekyll — bez tego GitHub Pages ignoruje katalogi zaczynające się od _
touch dist/.nojekyll

WORK="$(mktemp -d)"
git worktree add -q "$WORK" gh-pages 2>/dev/null || {
  git worktree add -q --detach "$WORK"
  git -C "$WORK" checkout -q --orphan gh-pages
  git -C "$WORK" rm -rq --cached . 2>/dev/null || true
}
find "$WORK" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
cp -R dist/. "$WORK"/
git -C "$WORK" add -A
if git -C "$WORK" diff --cached --quiet; then
  echo "Bez zmian — nic nie publikuję."
else
  git -C "$WORK" -c user.email="tet10mac@gmail.com" -c user.name="mteterycz" \
      commit -q -m "Snapshot panelu: $(date '+%Y-%m-%d %H:%M')"
  git -C "$WORK" push -q origin gh-pages
  echo "Opublikowano: https://mteterycz.github.io/holiday-searcher/"
fi
git worktree remove --force "$WORK"
