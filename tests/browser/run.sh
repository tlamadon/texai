#!/usr/bin/env bash
# Layout regression checks: starts texai on the example, drives headless
# Chrome over the DevTools Protocol, measures the geometry, tears everything
# down. No agent turns are sent, so this costs nothing to run.
#
#   tests/browser/run.sh
#
# Requires: node >= 22 (for the global WebSocket) and Google Chrome.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PORT="${TEXAI_PORT:-8795}"
DEBUG_PORT="${CHROME_DEBUG_PORT:-9223}"
PROFILE="$(mktemp -d)"

CHROME="${CHROME_PATH:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
if [ ! -x "$CHROME" ]; then
  CHROME="$(command -v google-chrome || command -v chromium || true)"
fi
if [ -z "$CHROME" ] || [ ! -x "$CHROME" ]; then
  echo "SKIP: Chrome not found (set CHROME_PATH to override)."
  exit 0
fi
if ! command -v node > /dev/null; then
  echo "SKIP: node not found."
  exit 0
fi

cd "$ROOT" || exit 1

if [ ! -f example/main.pdf ]; then
  echo "Building the example first..."
  (cd example && latexmk -pdf -synctex=1 -interaction=nonstopmode main.tex > /dev/null 2>&1)
fi

cleanup() {
  [ -n "${CHROME_PID:-}" ] && kill "$CHROME_PID" 2> /dev/null && wait "$CHROME_PID" 2> /dev/null
  [ -n "${SERVER_PID:-}" ] && kill "$SERVER_PID" 2> /dev/null && wait "$SERVER_PID" 2> /dev/null
  # Chrome releases its profile a moment after exiting.
  sleep 1
  rm -rf "$PROFILE" 2> /dev/null
}
trap cleanup EXIT

uv run texai --root ./example --pdf ./example/main.pdf --port "$PORT" \
  > "$PROFILE/server.log" 2>&1 &
SERVER_PID=$!

for _ in $(seq 1 60); do
  curl -sf -m 1 "http://127.0.0.1:$PORT/api/info" > /dev/null 2>&1 && break
  sleep 0.5
done

"$CHROME" --headless=new --disable-gpu --no-first-run \
  --user-data-dir="$PROFILE/chrome" --window-size=1600,1100 \
  --remote-debugging-port="$DEBUG_PORT" --remote-allow-origins='*' \
  "http://127.0.0.1:$PORT/" > "$PROFILE/chrome.log" 2>&1 &
CHROME_PID=$!

sleep 4
TEXAI_PORT="$PORT" CHROME_DEBUG_PORT="$DEBUG_PORT" node "$ROOT/tests/browser/layout.mjs"
