#!/usr/bin/env bash
# Start the NeoAG web demo (MOCK prototype) on http://127.0.0.1:8712
set -euo pipefail
cd "$(dirname "$0")"
PIDF=server.pid
LOGF=data/server.log
if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
  echo "already running (pid $(cat "$PIDF")) - use stop.sh first" >&2
  exit 1
fi
umask 077
nohup python3 -m backend.server >> "$LOGF" 2>&1 &
echo $! > "$PIDF"
# the server requires Basic auth: 401 on a credential-less probe still
# proves the process is up and serving
for i in $(seq 1 20); do
  sleep 0.5
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8712/api/bootstrap || echo 000)
  if [ "$code" = "200" ] || [ "$code" = "401" ]; then
    echo "NeoAG web demo up: http://127.0.0.1:8712 (pid $(cat "$PIDF"), auth: ON - password in data/auth.json)"
    exit 0
  fi
done
echo "server did not come up - check $LOGF" >&2
exit 1
