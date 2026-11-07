#!/usr/bin/env bash
# Install nxremote as a system service on Raspberry Pi OS / Debian / Ubuntu.
# Run from the repository root:  sudo ./deploy/install.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "run as root: sudo $0" >&2
  exit 1
fi

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX=/opt/nxremote

echo "==> installing packages"
apt-get update -qq
apt-get install -y --no-install-recommends python3 python3-venv python3-pip bluez

echo "==> creating virtualenv in $PREFIX"
# Use the distro Python: it ships with Bluetooth socket support (pyenv builds often don't).
python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --upgrade pip
"$PREFIX/venv/bin/pip" install "$REPO"

echo "==> disabling BlueZ's input plugin (this restarts bluetoothd)"
echo "    Bluetooth keyboards/mice stop working on this host; undo with: nxremote setup-bluez --restore"
"$PREFIX/venv/bin/nxremote" setup-bluez

echo "==> installing the systemd service"
install -m 0644 "$REPO/deploy/nxremote.service" /etc/systemd/system/nxremote.service
systemctl daemon-reload
systemctl enable --now nxremote.service

sleep 2
echo
"$PREFIX/venv/bin/nxremote" doctor || true
echo
echo "API:    http://$(hostname -I | awk '{print $1}'):8000   (docs at /docs, web UI at /)"
echo "Token:  sudo NXREMOTE_STATE_DIR=/var/lib/nxremote $PREFIX/venv/bin/nxremote token"
echo "Logs:   journalctl -u nxremote -f"
