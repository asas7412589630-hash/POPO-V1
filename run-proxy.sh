#!/bin/sh
# Startup wrapper so the proxy can run under hark-service (a detached
# supervisor). Every path is derived from this file, so the directory stays
# portable; nothing here is specific to one machine.
set -e
DIR=$(cd "$(dirname "$0")" && pwd)
export ARMOR_PROXY_PORT="${TRIAD_PORT:-8412}"
export ARMOR_BIND=127.0.0.1
export ARMOR_BASE="$DIR"
export ARMOR_MAP_FILE="$DIR/policy/source/armor_map_active.json"
export ARMOR_ALIAS_FILE="$DIR/policy/source/armor_alias.json"
export ARMOR_KEY_POOL="${ARMOR_KEY_POOL:-$DIR/proxy/upstream_keys.txt}"
export ARMOR_CLIENT_KEYS="$DIR/proxy/client_keys.txt"
export ARMOR_STATE_FILE="$DIR/runtime/state/session_state.json"
export ARMOR_ARCHIVE_DIR="$DIR/runtime/state"
export ARMOR_LOG_DIR="$DIR/logs"
export ARMOR_PANEL_FILE="$DIR/proxy/panel_settings.json"
export ARMOR_MEDIA_DIR="$DIR/media"
export ARMOR_PARAMS=1 ARMOR_REFRAME=1 ARMOR_STATE=0 ARMOR_ARCHIVE=0
export ARMOR_LEVEL="${ARMOR_LEVEL:-native}"
cd "$DIR"
exec python3 "$DIR/armor_proxy.py"
