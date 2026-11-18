#!/usr/bin/env bash
# Print the CURRENT public URLs and health of the supervised NeoAG tunnels
# without hunting through logs. Exit code: 0 all healthy, 2 anything else.
# Usage:  ./tunnel-status.sh [--json]
set -euo pipefail
cd "$(dirname "$0")"
exec python3 tunnelctl.py status "$@"
