#!/usr/bin/env bash
#
# setup-wan-failover.sh — multi-WAN internet failover for the FireWatch box.
#
# Supersedes setup-cellular-failover.sh. Sets up three uplinks in priority:
#     eno1 (wired primary) -> eth0 (wired secondary) -> TRM240 cellular.
#
#   * Wired ports are managed by dhcpcd. eth0 auto-joins the moment a cable is
#     plugged in. This script pins deterministic metrics (eno1=1000, eth0=2000)
#     so the wired ordering is guaranteed and leaves room for cellular.
#   * The cellular data link + the probe-based failover + the dashboard status
#     file are driven by /usr/local/sbin/wan-failover.py (installed here),
#     fired every 30s by a systemd timer.
#
# SAFE: the worker only ever touches the cellular default route. dhcpcd stays
# the authority for the wired routes, and nothing changes the on-link LAN route,
# so the dashboard stays reachable no matter what.
#
# Idempotent. Requires root (sudo). Run:  sudo bash ~/setup-wan-failover.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKER_SRC="${SCRIPT_DIR}/wan-failover.py"
WORKER_DST="/usr/local/sbin/wan-failover.py"
DHCPCD_CONF="/etc/dhcpcd.conf"
QMI_CONF="/etc/qmi-network.conf"
MARKER="# --- FireWatch WAN failover metrics ---"

if [[ "${EUID}" -ne 0 ]]; then
  echo "ERROR: run as root:  sudo bash $0" >&2
  exit 1
fi
if [[ ! -f "${WORKER_SRC}" ]]; then
  echo "ERROR: ${WORKER_SRC} not found (keep it next to this script)." >&2
  exit 1
fi

echo "==> Installing libqmi-utils (if missing)…"
if ! command -v qmicli >/dev/null 2>&1 || ! command -v qmi-network >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y libqmi-utils iputils-ping
fi

# ModemManager would grab the modem's serial ports (which FireWatch uses for SMS).
if systemctl is-active --quiet ModemManager 2>/dev/null; then
  echo "==> Masking ModemManager (we drive the modem directly)…"
  systemctl stop ModemManager || true
  systemctl mask ModemManager || true
fi

echo "==> Writing ${QMI_CONF} (APN=telia)…"
cat > "${QMI_CONF}" <<'EOF'
# Managed by setup-wan-failover.sh
APN=telia
IP_TYPE=4
PROXY=yes
EOF

echo "==> Pinning dhcpcd route metrics (eno1=1000, eth0=2000)…"
if ! grep -qF "${MARKER}" "${DHCPCD_CONF}"; then
  cat >> "${DHCPCD_CONF}" <<EOF

${MARKER}
interface eno1
metric 1000
interface eth0
metric 2000
EOF
  echo "   added; reconfiguring dhcpcd…"
  dhcpcd --reconfigure 2>/dev/null || systemctl restart dhcpcd
else
  echo "   already present; leaving as-is."
fi

echo "==> Installing worker ${WORKER_DST}…"
install -m 0755 "${WORKER_SRC}" "${WORKER_DST}"

# Retire the old cellular-only units if they exist (this worker replaces them).
if systemctl list-unit-files | grep -q '^cellular-failover\.timer'; then
  echo "==> Disabling old cellular-failover.timer (superseded)…"
  systemctl disable --now cellular-failover.timer 2>/dev/null || true
fi

echo "==> Installing systemd service + 30s timer…"
cat > /etc/systemd/system/wan-failover.service <<EOF
[Unit]
Description=FireWatch multi-WAN internet failover worker
After=network.target
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 ${WORKER_DST}
EOF

cat > /etc/systemd/system/wan-failover.timer <<EOF
[Unit]
Description=Run the WAN failover worker periodically
[Timer]
OnBootSec=20s
OnUnitActiveSec=30s
AccuracySec=5s
[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now wan-failover.timer

echo "==> First run…"
/usr/bin/python3 "${WORKER_DST}" || true
sleep 3

echo
echo "================ VERIFICATION ================"
echo "-- default routes (eno1<eth0<cellular by metric) --"
ip route show default
echo
echo "-- status file the dashboard reads --"
if [[ -f /run/wan-failover/status.json ]]; then
  python3 -m json.tool /run/wan-failover/status.json
else
  echo "MISSING — worker did not write it; check: journalctl -u wan-failover.service"
fi
echo "=============================================="
echo
echo "Done. The dashboard 'Internet' card will now show each uplink and which is in use."
echo "Plug a cable into eth0 to add the wired backup; pull eno1 to watch failover."
echo "Logs:  journalctl -u wan-failover.service -f"
