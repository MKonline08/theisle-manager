#!/usr/bin/env bash
set -Eeuo pipefail

APP_ID="${STEAM_APP_ID:-412680}"
# The current public dedicated-server depot is published on this Steam beta branch.
# Set STEAM_BRANCH=public only if the publisher makes the default branch usable again.
STEAM_BRANCH="${STEAM_BRANCH:-evrima}"
INSTALL_DIR="${INSTALL_DIR:-/server}"
SERVER_PORT="${SERVER_PORT:-7777}"
QUERY_PORT="${QUERY_PORT:-7778}"
MAX_PLAYERS="${MAX_PLAYERS:-100}"
SERVER_NAME="${SERVER_NAME:-The Isle Server}"
SERVER_PASSWORD="${SERVER_PASSWORD:-}"
SERVER_MAP="${SERVER_MAP:-Gateway}"
# cm2network/steamcmd exposes SteamCMD as this script, not as a PATH command.
STEAMCMD="${STEAMCMD_PATH:-/home/steam/steamcmd/steamcmd.sh}"

if [[ ! -x "$STEAMCMD" ]]; then
  echo "[manager] SteamCMD is missing at $STEAMCMD" >&2
  exit 127
fi

mkdir -p "$INSTALL_DIR"/{Saved,Config,Logs,Mods,Plugins,Backups}

install_or_update() {
  echo "[manager] Installing or updating Steam app ${APP_ID} (${STEAM_BRANCH:-public})"
  APP_UPDATE=(+app_update "$APP_ID")
  if [[ -n "$STEAM_BRANCH" && "$STEAM_BRANCH" != "public" ]]; then
    APP_UPDATE+=(-beta "$STEAM_BRANCH")
  fi
  APP_UPDATE+=(validate +quit)
  "$STEAMCMD" +force_install_dir "$INSTALL_DIR" +login anonymous "${APP_UPDATE[@]}"
}

case "${SERVER_ACTION:-run}" in
  build) echo "[manager] The Isle server image is ready"; exit 0 ;;
  install) install_or_update; exit 0 ;;
  update) install_or_update; exit 0 ;;
  validate)
    echo "[manager] Validating Steam app ${APP_ID}"
    APP_UPDATE=(+app_update "$APP_ID")
    if [[ -n "$STEAM_BRANCH" && "$STEAM_BRANCH" != "public" ]]; then
      APP_UPDATE+=(-beta "$STEAM_BRANCH")
    fi
    APP_UPDATE+=(validate +quit)
    "$STEAMCMD" +force_install_dir "$INSTALL_DIR" +login anonymous "${APP_UPDATE[@]}"
    exit 0 ;;
  workshop)
    : "${WORKSHOP_ID:?WORKSHOP_ID is required for workshop downloads}"
    echo "[manager] Downloading Workshop item ${WORKSHOP_ID}"
    "$STEAMCMD" +force_install_dir "$INSTALL_DIR" +login anonymous +workshop_download_item "$APP_ID" "$WORKSHOP_ID" +quit
    SOURCE="${HOME}/Steam/steamapps/workshop/content/${APP_ID}/${WORKSHOP_ID}"
    if [[ -d "$SOURCE" ]]; then
      rm -rf "$INSTALL_DIR/Mods/$WORKSHOP_ID"
      mkdir -p "$INSTALL_DIR/Mods"
      mv "$SOURCE" "$INSTALL_DIR/Mods/$WORKSHOP_ID"
      echo "[manager] Workshop item installed in Mods/${WORKSHOP_ID}"
    else
      echo "[manager] SteamCMD completed but did not produce a workshop folder" >&2
      exit 1
    fi
    exit 0 ;;
  run) ;;
  *) echo "Unknown SERVER_ACTION: ${SERVER_ACTION}" >&2; exit 64 ;;
esac

if [[ ! -x "$INSTALL_DIR/TheIsleServer.sh" ]]; then
  install_or_update
fi

cd "$INSTALL_DIR"
PASSWORD_ARG=()
[[ -n "$SERVER_PASSWORD" ]] && PASSWORD_ARG=("?Password=${SERVER_PASSWORD}")
echo "[manager] Starting '${SERVER_NAME}' on game port ${SERVER_PORT}, query port ${QUERY_PORT}"
exec ./TheIsleServer.sh MultiHome=0.0.0.0 "${SERVER_MAP}?Port=${SERVER_PORT}?QueryPort=${QUERY_PORT}?MaxPlayers=${MAX_PLAYERS}?SessionName=${SERVER_NAME}${PASSWORD_ARG[*]}" -log
