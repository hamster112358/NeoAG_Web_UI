#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
PIDF=server.pid
if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
  kill "$(cat "$PIDF")"; sleep 1
  kill -9 "$(cat "$PIDF")" 2>/dev/null || true
  echo "stopped"
else
  echo "not running"
fi
rm -f "$PIDF"
