#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

STAMP="$(date '+%Y-%m-%d %H:%M')"

if [ -d .git ]; then
  git add -A
  git diff --cached --quiet || git commit -m "Deploy $STAMP" || true
  git push 2>/dev/null || true
fi

railway up --detach
