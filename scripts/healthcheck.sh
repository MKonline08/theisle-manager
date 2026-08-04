#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PORT="$(grep '^PANEL_PORT=' .env | cut -d= -f2)"
curl --fail --silent --show-error "http://127.0.0.1:${PORT:-8080}/api/health"
