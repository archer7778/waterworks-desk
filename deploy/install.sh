#!/usr/bin/env bash
# Waterworks Desk installer for Raspberry Pi OS (Lite) and other Debian-based Linux.
# Run from the repository folder:   ./deploy/install.sh
# Safe to run again; it keeps your config.toml, API key and data.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="$(id -un)"
cd "$ROOT"

if [ "$(id -u)" -eq 0 ]; then
  echo "Run this as your normal user (it asks for sudo when it needs it), not with sudo:"
  echo "  ./deploy/install.sh"
  exit 1
fi

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

say "1/5  Installing system packages (needs your password once)"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip sqlite3 >/dev/null

say "2/5  Creating a Python environment"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

say "3/5  Configuration"
mkdir -p data secrets
chmod 700 secrets
if [ ! -f config.toml ]; then
  cp config.example.toml config.toml
  read -r -p "  Latitude of the centre of your region, decimal degrees (e.g. 51.48, or Enter to skip): " LAT || true
  read -r -p "  Longitude of the centre of your region, decimal degrees (e.g. -0.01, or Enter to skip): " LON || true
  num='^-?[0-9]+([.][0-9]+)?$'
  if [ -n "${LAT:-}${LON:-}" ] && ! [[ "${LAT:-}" =~ $num && "${LON:-}" =~ $num ]]; then
    echo "  Those don't look like decimal degrees; skipped. Edit lat and lon in config.toml later."
    LAT=""; LON=""
  fi
  if [ -n "${LAT:-}" ] && [ -n "${LON:-}" ]; then
    sed -i "s/^lat = .*/lat = ${LAT}/; s/^lon = .*/lon = ${LON}/" config.toml
  fi
  echo "  Wrote config.toml (edit it any time, then: sudo systemctl restart waterworks-desk-web)"
else
  echo "  Keeping your existing config.toml"
fi
chmod 600 config.toml   # it can hold the operator passcode and your private alert topic
say "4/5  Installing background services"
for unit in waterworks-desk-web.service waterworks-desk-ingest.service waterworks-desk-ingest.timer \
            waterworks-desk-update.service waterworks-desk-update.timer; do
  sed -e "s#@ROOT@#${ROOT}#g" -e "s#@USER@#${USER_NAME}#g" "deploy/${unit}" | sudo tee "/etc/systemd/system/${unit}" >/dev/null
done
cfgval() { awk -F= -v k="$1" '$1 ~ "^"k"[ ]*$" {sub(/#.*/,"",$2); gsub(/[ "]/,"",$2); print $2; exit}' config.toml; }
POLL=$(cfgval poll_minutes); POLL=${POLL%%.*}
[[ "$POLL" =~ ^[0-9]+$ ]] || POLL=5
if [ "$POLL" -lt 1 ]; then POLL=1; fi
sudo sed -i "s#^OnUnitActiveSec=.*#OnUnitActiveSec=${POLL}min#" /etc/systemd/system/waterworks-desk-ingest.timer
sudo systemctl daemon-reload
sudo systemctl enable --now waterworks-desk-web.service waterworks-desk-ingest.timer >/dev/null
AUTO=$(cfgval auto_update)
if [ "${AUTO:-false}" = "true" ]; then   # off by default: never self-update near a real utility network
  sudo systemctl enable --now waterworks-desk-update.timer >/dev/null
else
  sudo systemctl disable --now waterworks-desk-update.timer >/dev/null 2>&1 || true
fi
sudo systemctl restart waterworks-desk-web.service

say "5/5  Done"
PORT=$(cfgval port); PORT=${PORT:-8080}
HOST=$(hostname)
LANIP=$(ip -4 -o addr show wlan0 2>/dev/null | awk '{print $4}' | cut -d/ -f1); [ -z "$LANIP" ] && LANIP=$(ip -4 -o addr show eth0 2>/dev/null | awk '{print $4}' | cut -d/ -f1)
echo "  Console:  http://${HOST}.local:${PORT}${LANIP:+   (or http://${LANIP}:${PORT})}"
echo "  Sources are read every few minutes. To read now:  $ROOT/.venv/bin/python -m waterworks_desk.ingest --force"
echo "  Starts by itself at every boot. Automatic updates are OFF (the safe default near a real network)."
echo "  Update by hand after reviewing the changes: sudo $ROOT/deploy/update.sh --now"
echo "  Logs:     journalctl -u waterworks-desk-web -u waterworks-desk-ingest -u waterworks-desk-update -f"
