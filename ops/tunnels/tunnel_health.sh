#!/usr/bin/env bash
# ExecStart for neoag-tunnel-health.service: one layered health/recovery
# cycle for every supervised tunnel (probes, streaks, backoff-gated
# restarts, state file update). All logic lives in tunnelctl.py.
set -euo pipefail
cd "$(dirname "$0")"
exec python3 tunnelctl.py health-cycle
