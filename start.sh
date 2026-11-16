#!/usr/bin/env bash
# Start the NeoAG web demo (MOCK prototype) on http://127.0.0.1:8712
# The server runs under watchdog.sh, detached from the launching session
# (setsid + nohup) so it survives agent sandbox teardown, and is restarted
# automatically if killed. stop.sh stops both.
set -euo pipefail
cd "$(dirname "$0")"
WD=watchdog.pid
if [ -f "$WD" ] && kill -0 "$(cat "$WD")" 2>/dev/null; then
  echo "already running (watchdog pid $(cat "$WD"), server $(cat server.pid 2>/dev/null))" >&2
  exit 1
fi
rm -f data/STOP
mkdir -p data

# Deploy the agent-workspace assets (PreToolUse gate hook, skill, rules)
# into the live workspace the real agent runs in. Runtime dirs (jobs/,
# audit/, .neoag-approvals/) are never removed by this step.
WS=/data1/neoag/agents/neoag_agent_ws
if [ -d agent_workspace_assets ]; then
  if mkdir -p "$WS/.codebuddy/skills" "$WS/jobs" "$WS/audit" \
              "$WS/.neoag-approvals" 2>/dev/null &&
     cp -a agent_workspace_assets/CODEBUDDY.md "$WS/" &&
     cp -a agent_workspace_assets/.codebuddy/. "$WS/.codebuddy/"; then
    chmod 700 "$WS/.neoag-approvals" 2>/dev/null || true
  else
    echo "WARNING: could not deploy agent workspace assets to $WS" >&2
  fi
fi

umask 077
setsid nohup bash watchdog.sh >> data/watchdog.log 2>&1 &
# the server requires Basic auth: 401 on a credential-less probe still
# proves the process is up and serving
for i in $(seq 1 20); do
  sleep 0.5
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8712/api/bootstrap || echo 000)
  if [ "$code" = "200" ] || [ "$code" = "401" ]; then
    echo "NeoAG web demo up: http://127.0.0.1:8712 (watchdog $(cat "$WD" 2>/dev/null), server $(cat server.pid 2>/dev/null), auth: ON - password in data/auth.json)"
    exit 0
  fi
done
echo "server did not come up - check data/server.log and data/watchdog.log" >&2
exit 1
