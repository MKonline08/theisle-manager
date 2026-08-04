#!/usr/bin/env bash
# Install from a checked-out release directory. Run with: sudo ./scripts/install.sh
set -Eeuo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="${INSTALL_DIR:-/opt/theisle-manager}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer with sudo."
  exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required. Install Docker Engine and the Docker Compose plugin, then run this script again."
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose v2 is required (docker compose)."
  exit 1
fi

mkdir -p "$INSTALL_DIR"
if [[ "$SOURCE_DIR" != "$INSTALL_DIR" ]]; then
  if [[ -n "$(find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    echo "$INSTALL_DIR is not empty. Refusing to overwrite an existing installation."
    exit 1
  fi
  cp -a "$SOURCE_DIR/." "$INSTALL_DIR/"
fi

cd "$INSTALL_DIR"
if [[ ! -f .env ]]; then
  umask 077
  POSTGRES_PASSWORD="$(openssl rand -base64 36 | tr -d '\n' | tr '/+' 'ab' | cut -c1-40)"
  JWT_SECRET="$(openssl rand -base64 48 | tr -d '\n' | tr '/+' 'cd' | cut -c1-56)"
  sed \
    -e "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=${POSTGRES_PASSWORD}/" \
    -e "s/^JWT_SECRET=.*/JWT_SECRET=${JWT_SECRET}/" \
    .env.example > .env
  echo "Created .env with unique secrets. Keep it private."
fi

mkdir -p data/{servers,backups,logs,uploads} backups logs mods plugins
chmod 750 data backups logs mods plugins
docker compose up -d --build

HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "The Isle Manager is running."
echo "Open: http://${HOST_IP:-localhost}:$(grep '^PANEL_PORT=' .env | cut -d= -f2)"
echo "Create the first Owner account on the setup screen."
