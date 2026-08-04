#!/usr/bin/env bash
# Backs up persistent panel data before rebuilding the current checkout.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

./scripts/backup.sh
if [[ -d .git ]]; then
  git pull --ff-only
else
  echo "No Git checkout detected; replace the application files, then run this script again."
fi
docker compose up -d --build --remove-orphans
docker image prune -f --filter "label=io.theisle.manager=true" || true
echo "Update complete."
