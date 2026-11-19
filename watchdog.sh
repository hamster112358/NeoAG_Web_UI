#!/usr/bin/env bash
# Supervisor for the demo server. Launched detached (setsid) by start.sh:
# survives the sandbox reaper that SIGTERMs long-lived agent tasks, and
# restarts the server automatically if it is ever killed.
# stop.sh creates data/STOP to end the loop; the sentinel is removed by
# start.sh on the next start.
set -u
cd "$(dirname "$0")"
# Authorized input roots for the Web layer's READ-ONLY input discovery
# (backend/native.py AUTHORIZED_INPUT_ROOTS; realpath-checked, no symlink
# escape). Deployment reality (operator storage policy): case working data
# lives on the scratch volume under the agent-run root, and the gateway
# execution path already consumes manifests pointing there - discovery is
# kept aligned with that same footprint instead of only the primary volume.
# Override by exporting NEOAG_INPUT_ROOTS before start.sh if the layout
# changes; the native default (/data1/neoag) stays in the list.
export NEOAG_INPUT_ROOTS="${NEOAG_INPUT_ROOTS:-/data1/neoag:/data2/caojingqi/neoag_agent_runs}"
echo $$ > watchdog.pid
while true; do
  [ -f data/STOP ] && break
  python3 -u -m backend.server >> data/server.log 2>&1 &
  echo $! > server.pid
  wait
  sleep 2
done
rm -f watchdog.pid server.pid
