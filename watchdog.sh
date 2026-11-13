#!/usr/bin/env bash
# Supervisor for the demo server. Launched detached (setsid) by start.sh:
# survives the sandbox reaper that SIGTERMs long-lived agent tasks, and
# restarts the server automatically if it is ever killed.
# stop.sh creates data/STOP to end the loop; the sentinel is removed by
# start.sh on the next start.
set -u
cd "$(dirname "$0")"
echo $$ > watchdog.pid
while true; do
  [ -f data/STOP ] && break
  python3 -u -m backend.server >> data/server.log 2>&1 &
  echo $! > server.pid
  wait
  sleep 2
done
rm -f watchdog.pid server.pid
