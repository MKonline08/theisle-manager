#!/usr/bin/env bash
# Creates a portable archive of the panel database and manager storage.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
set -a
source .env
set +a

STAMP="$(date -u +%Y%m%d-%H%M%S)"
OUTPUT="${BACKUP_DIR:-$ROOT/backups}/panel-$STAMP"
mkdir -p "$OUTPUT"
PROJECT="$(docker compose config --format json 2>/dev/null | sed -n 's/.*"name":"\([^"]*\)".*/\1/p' | head -n1)"
PROJECT="${PROJECT:-theisle-manager}"

docker compose exec -T postgres pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" > "$OUTPUT/database.sql"
docker run --rm \
  -v "${PROJECT}_manager-data:/source:ro" \
  -v "$OUTPUT:/backup" \
  alpine:3.20 tar -C /source -czf /backup/manager-data.tar.gz .
cp .env "$OUTPUT/env.backup"
chmod 600 "$OUTPUT"/*
echo "Panel backup saved in $OUTPUT"
