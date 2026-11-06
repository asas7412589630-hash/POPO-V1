#!/bin/sh
# Tunnel wrapper for hark-service. Finds cloudflared portably, waits for the
# public hostname to pass a health check, then records it — a dead hostname in
# tunnel_url.txt is worse than none, because it looks like a proxy failure.
set -e
DIR=$(cd "$(dirname "$0")" && pwd)
# PORT must default to the port the PROXY actually runs on, and the proxy in
# this package reads TRIAD_PORT (default 8412). A tunnel pointed at the wrong
# port connects happily and then 502s every request — which is indistinguishable
# from "the console is broken" (measured 2026-11-05: the tunnel was dialling
# 8412 while the proxy listened on 8420, so every page failed).
PORT=${TRIAD_PORT:-${ARMOR_PROXY_PORT:-8412}}
LOG=$DIR/logs/tunnel.log
URLF=$DIR/tunnel_url.txt
mkdir -p "$DIR/logs"

BIN=${CLOUDFLARED:-}
if [ -z "$BIN" ]; then
  for c in "$DIR/bin/cloudflared" \
           "$(command -v cloudflared 2>/dev/null || true)" \
           "$DIR/../armor_lib/proxy/bin/cloudflared" \
           /usr/local/bin/cloudflared /usr/bin/cloudflared; do
    [ -n "$c" ] && [ -x "$c" ] && BIN=$c && break
  done
fi
[ -z "$BIN" ] && { echo "cloudflared not found" >&2; exit 1; }

: > "$LOG"
"$BIN" tunnel --url "http://127.0.0.1:$PORT" --no-autoupdate >> "$LOG" 2>&1 &
CF=$!
trap 'kill $CF 2>/dev/null' INT TERM

# poll for the hostname, then verify it answers before publishing it
i=0
while [ $i -lt 60 ]; do
  U=$(grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" "$LOG" 2>/dev/null | head -1)
  if [ -n "$U" ]; then
    if curl -s -m 5 "$U/healthz" | grep -q '"ok"'; then
      printf '%s' "$U" > "$URLF"
      echo "tunnel up: $U"
      break
    fi
  fi
  i=$((i+1)); sleep 2
done
wait $CF
