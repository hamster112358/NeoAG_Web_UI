#!/usr/bin/env bash
# Install/remove the user-level systemd supervision for the NeoAG quick
# tunnels. Safe to re-run (idempotent). No secrets are written anywhere;
# runtime state lives in /data1/neoag/agents/remote_access/tunnels (0700).
#
#   ./install_tunnels.sh            install + enable + start all units
#   ./install_tunnels.sh --uninstall  stop, disable and remove the units
#                                   (rollback: see README_TUNNELS.md)
#
# Requires: user systemd (loginctl enable-linger $USER for boot recovery),
# cloudflared at the path baked into units/*.service.
set -euo pipefail
cd "$(dirname "$0")"
UNITS_DIR="${HOME}/.config/systemd/user"
STATE_DIR=/data1/neoag/agents/remote_access/tunnels
UNITS=(neoag-tunnel-web.service neoag-tunnel-gw.service
       neoag-tunnel-health.service neoag-tunnel-health.timer)

if [ "${1:-}" = "--uninstall" ]; then
  for u in "${UNITS[@]}"; do
    systemctl --user disable --now "$u" 2>/dev/null || true
    rm -f "$UNITS_DIR/$u"
  done
  systemctl --user daemon-reload
  echo "units stopped, disabled and removed (state/logs kept under $STATE_DIR)"
  exit 0
fi

# ---- preflight ----
CFD=$(sed -n 's/^ExecStart=\([^ ]*\).*/\1/p' units/neoag-tunnel-web.service)
[ -x "$CFD" ] || { echo "cloudflared not found/executable: $CFD" >&2; exit 1; }
for tool in curl ss systemctl pgrep python3; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 1; }
done
systemctl --user show -p Version >/dev/null || {
  echo "user systemd not reachable (XDG_RUNTIME_DIR?)" >&2; exit 1; }
if [ "$(loginctl show-user "$USER" --property=Linger --value 2>/dev/null)" \
     != "yes" ]; then
  echo "WARNING: linger is off - tunnels will NOT come back after a" \
       "reboot/logout until: loginctl enable-linger $USER" >&2
fi

# ---- install ----
install -d -m 700 "$STATE_DIR"
install -d -m 755 "$UNITS_DIR"
for u in "${UNITS[@]}"; do
  install -m 644 -t "$UNITS_DIR" "units/$u"
done
systemctl --user daemon-reload
systemctl --user enable --now neoag-tunnel-health.timer
systemctl --user enable --now neoag-tunnel-web.service
systemctl --user enable --now neoag-tunnel-gw.service
sleep 2
systemctl --user --no-pager --full \
  status neoag-tunnel-web.service neoag-tunnel-gw.service \
  neoag-tunnel-health.timer | grep -E '^\s*\*|●|Active:|Elapsed:|state:' \
  || true
echo
echo "installed. Give the first health cycle up to ~2 min, then run:"
echo "  $PWD/tunnel-status.sh"
