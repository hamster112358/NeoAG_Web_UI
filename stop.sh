#!/usr/bin/env bash
# Stop the demo server AND its watchdog (watchdog restarts the server, so
# it must be stopped first). data/STOP tells the watchdog loop to exit.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p data
touch data/STOP
if [ -f watchdog.pid ] && kill -0 "$(cat watchdog.pid)" 2>/dev/null; then
  kill "$(cat watchdog.pid)" 2>/dev/null || true
  echo "watchdog stopped"
fi
if [ -f server.pid ] && kill -0 "$(cat server.pid)" 2>/dev/null; then
  kill "$(cat server.pid)"; sleep 1
  kill -9 "$(cat server.pid)" 2>/dev/null || true
  echo "server stopped"
fi
rm -f watchdog.pid server.pid
