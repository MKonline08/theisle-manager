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

capture_steam_failure() {
  local status="$1"
  local log_dir="${HOME}/Steam/logs"
  mkdir -p "$INSTALL_DIR/Logs"
  echo "[manager] SteamCMD failed with exit code ${status}. Saving Steam diagnostics to Logs/."
  for filename in content_log.txt stderr.txt; do
    if [[ -f "$log_dir/$filename" ]]; then
      cp "$log_dir/$filename" "$INSTALL_DIR/Logs/steamcmd-$filename"
      echo "[manager] --- SteamCMD $filename (last 120 lines) ---"
      tail -n 120 "$log_dir/$filename" || true
      echo "[manager] --- end SteamCMD $filename ---"
    fi
  done
}

run_steamcmd() {
  "$STEAMCMD" "$@" || {
    local status=$?
    capture_steam_failure "$status"
    return "$status"
  }
}

install_or_update() {
  echo "[manager] Installing or updating Steam app ${APP_ID} (${STEAM_BRANCH:-public})"
  APP_UPDATE=(+app_update "$APP_ID")
  if [[ -n "$STEAM_BRANCH" && "$STEAM_BRANCH" != "public" ]]; then
    APP_UPDATE+=(-beta "$STEAM_BRANCH")
  fi
  APP_UPDATE+=(validate +quit)
  run_steamcmd +force_install_dir "$INSTALL_DIR" +login anonymous "${APP_UPDATE[@]}"
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
    run_steamcmd +force_install_dir "$INSTALL_DIR" +login anonymous "${APP_UPDATE[@]}"
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
      echo "[manager] Workshop item installed in Mods/$WORKSHOP_ID"
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

# Evrima expects the cooked Gateway path rather than the map's display name.
MAP_PATH="/Game/TheIsle/Maps/Game/Gateway/Gateway"
if [[ "$SERVER_MAP" != "Gateway" ]]; then
  MAP_PATH="$SERVER_MAP"
fi
LAUNCH_URL="${MAP_PATH}?Port=${SERVER_PORT}?QueryPort=${QUERY_PORT}?MaxPlayers=${MAX_PLAYERS}?SessionName=${SERVER_NAME}?MultiHome=0.0.0.0${PASSWORD_ARG[*]}"
EOS_ARGS=()
if [[ -n "${THEISLE_EOS_CLIENT_ID:-}" && -n "${THEISLE_EOS_CLIENT_SECRET:-}" ]]; then
  # Supply the identity both as Engine.ini and launch overrides so Evrima reliably
  # receives it on Linux even when the game's config discovery changes between builds.
  EOS_ARGS=(
    "-ini:Engine:[EpicOnlineServices]:DedicatedServerClientId=${THEISLE_EOS_CLIENT_ID}"
    "-ini:Engine:[EpicOnlineServices]:DedicatedServerClientSecret=${THEISLE_EOS_CLIENT_SECRET}"
  )
else
  echo "[manager] EOS credentials are not configured. Set THEISLE_EOS_CLIENT_ID and THEISLE_EOS_CLIENT_SECRET in MK Panel's .env file." >&2
fi
echo "[manager] Starting '${SERVER_NAME}' on game port ${SERVER_PORT}, query port ${QUERY_PORT}"

# The wrapper can return before Unreal prints its crash reason to stdout. Preserve
# the exit status and expose the game log in the panel console before Docker retries it.
set +e
./TheIsleServer.sh "$LAUNCH_URL" -log "${EOS_ARGS[@]}"
GAME_EXIT_STATUS=$?
set -e
echo "[manager] The Isle process exited with code ${GAME_EXIT_STATUS}."

GAME_LOG_DIR="$INSTALL_DIR/TheIsle/Saved/Logs"
if [[ -d "$GAME_LOG_DIR" ]]; then
  LATEST_GAME_LOG="$(find "$GAME_LOG_DIR" -maxdepth 1 -type f -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -n 1 | cut -d' ' -f2-)"
  if [[ -n "$LATEST_GAME_LOG" && -f "$LATEST_GAME_LOG" ]]; then
    echo "[manager] --- Unreal log: $LATEST_GAME_LOG (last 160 lines) ---"
    tail -n 160 "$LATEST_GAME_LOG" || true
    echo "[manager] --- end Unreal log ---"
  fi
fi

exit "$GAME_EXIT_STATUS"
