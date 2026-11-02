#!/usr/bin/env bash
# Waterworks Desk self-update. Run by waterworks-desk-update.timer (a few minutes after boot, then
# nightly) only when auto_update = true, or by hand after review:   sudo ./deploy/update.sh --now
#
# Waterworks Desk keeps this OFF by default (auto_update = false): near a real utility network,
# updates are pulled by hand after review. When run by hand it still does everything below.
#
# Pulls new commits from GitHub only when that's a clean fast-forward, installs any new Python
# packages, refreshes the systemd units if they changed, restarts the console, and checks it
# answers. If anything fails it rolls back to the version that was running.
# It never touches config.toml, secrets/ or data/. Set auto_update = false under [updates] in
# config.toml to stop it (checks still run with --check). With auto_update off, --now applies an update.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="$(stat -c %U "$ROOT")"
STATUS="$ROOT/data/update.json"
cd "$ROOT"

as_user() { if [ "$(id -u)" -eq 0 ]; then runuser -u "$USER_NAME" -- "$@"; else "$@"; fi; }
cfgval() { awk -F= -v k="$1" '$1 ~ "^"k"[ ]*$" {sub(/#.*/,"",$2); gsub(/[ "]/,"",$2); print $2; exit}' config.toml 2>/dev/null; }
status() {   # status <result> <message> [from] [to]
  mkdir -p "$ROOT/data"
  printf '{"t_ms":%s,"result":"%s","message":"%s","from":"%s","to":"%s","branch":"%s"}\n' \
    "$(date +%s%3N)" "$1" "$(printf '%s' "$2" | tr '"\\' "''" | tr -d '\000-\037')" "${3:-}" "${4:-}" "$BRANCH" > "$STATUS"
  chown "$USER_NAME" "$STATUS" 2>/dev/null || true
  echo "$2"
}

BRANCH="$(as_user git rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)"
AUTO="$(cfgval auto_update)"; AUTO="${AUTO:-false}"   # off unless config.toml says auto_update = true

if ! as_user git fetch -q origin "$BRANCH" 2>/dev/null; then
  status offline "Couldn't reach GitHub to check for updates; will try again later."
  exit 0
fi
LOCAL="$(as_user git rev-parse --short HEAD)"
REMOTE="$(as_user git rev-parse --short "origin/$BRANCH")"
if [ "$LOCAL" = "$REMOTE" ]; then
  status current "Up to date (version $(as_user git log -1 --format=%cs) $LOCAL)." "$LOCAL" "$LOCAL"
  exit 0
fi
if ! as_user git merge-base --is-ancestor HEAD "origin/$BRANCH"; then
  status held "Not updating: this Pi has its own commits. Merge by hand." "$LOCAL" "$REMOTE"
  exit 0
fi
if ! as_user git diff --quiet || ! as_user git diff --cached --quiet; then
  status held "Not updating: files in the repository were edited on this Pi ($(as_user git diff --name-only | tr '\n' ' ')). Commit or undo them, then it updates." "$LOCAL" "$REMOTE"
  exit 0
fi
if { [ "$AUTO" = "false" ] && [ "${1:-}" != "--now" ]; } || [ "${1:-}" = "--check" ]; then
  status available "Update available: $LOCAL → $REMOTE ($(as_user git rev-list --count HEAD..origin/$BRANCH) commits). Review it, then: sudo deploy/update.sh --now" "$LOCAL" "$REMOTE"
  exit 0
fi

CHANGED="$(as_user git diff --name-only HEAD "origin/$BRANCH")"

# Don't swap the code under a source read that's running: take the same lock the ingest job uses
# (made by the app's user first, so the ingest job can still open it afterwards).
mkdir -p "$ROOT/data" && chown "$USER_NAME" "$ROOT/data" 2>/dev/null
as_user touch "$ROOT/data/ingest.lock"
exec 9>>"$ROOT/data/ingest.lock"
if ! flock -w 900 9; then
  status held "Not updating right now: a source read has been running for 15 minutes. Will try again later." "$LOCAL" "$REMOTE"
  exit 0
fi

install_units() {
  for f in deploy/*.service deploy/*.timer; do
    sed -e "s#@ROOT@#${ROOT}#g" -e "s#@USER@#${USER_NAME}#g" "$f" > "/etc/systemd/system/$(basename "$f")"
  done
  POLL="$(cfgval poll_minutes)"; POLL="${POLL%%.*}"
  [[ "$POLL" =~ ^[0-9]+$ ]] || POLL=5
  [ "$POLL" -lt 1 ] && POLL=1
  sed -i "s#^OnUnitActiveSec=.*#OnUnitActiveSec=${POLL}min#" /etc/systemd/system/waterworks-desk-ingest.timer
  systemctl daemon-reload
}

rollback() {
  as_user git reset -q --hard "$LOCAL"
  if grep -q '^requirements.txt$' <<<"$CHANGED"; then   # best effort: back to the old packages
    as_user .venv/bin/pip install -q -r requirements.txt >/dev/null 2>&1
  fi
  if [ "$(id -u)" -eq 0 ]; then
    grep -qE '^deploy/.*\.(service|timer)$' <<<"$CHANGED" && install_units
    systemctl restart waterworks-desk-web 2>/dev/null
  fi
  status failed "Update to $REMOTE failed ($1); rolled back to $LOCAL." "$LOCAL" "$REMOTE"
  exit 1
}

as_user git merge -q --ff-only "origin/$BRANCH" || rollback "git merge"
if grep -q '^requirements.txt$' <<<"$CHANGED"; then
  as_user .venv/bin/pip install -q -r requirements.txt || rollback "installing Python packages"
fi
as_user .venv/bin/python -c "import waterworks_desk.server, waterworks_desk.ingest" || rollback "the new code doesn't load"

if [ "$(id -u)" -eq 0 ]; then
  if grep -qE '^deploy/.*\.(service|timer)$' <<<"$CHANGED"; then
    install_units
    systemctl enable waterworks-desk-web.service >/dev/null 2>&1
    # timers start now, not at the next reboot (a new nightly check would otherwise sit idle)
    systemctl enable --now waterworks-desk-ingest.timer >/dev/null 2>&1
  fi
  systemctl restart waterworks-desk-web
  PORT="$(cfgval port)"; PORT="${PORT:-8080}"
  ok=""
  for _ in $(seq 1 20); do
    sleep 3
    if as_user .venv/bin/python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:${PORT}/api/health', timeout=5)" 2>/dev/null; then ok=1; break; fi
  done
  [ -n "$ok" ] || rollback "the console didn't come back after restarting"
fi
status updated "Updated $LOCAL → $REMOTE: $(as_user git log --format=%s -1)." "$LOCAL" "$REMOTE"
