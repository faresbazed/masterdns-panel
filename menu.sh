#!/usr/bin/env bash
# =============================================================================
# MasterDNS Panel — terminal menu for day-to-day management on the VPS.
#
#   1) Install / reinstall   (runs install.sh)
#   2) Restart services      (masterdnsvpn + masterdns-panel)
#   3) Service status
#   4) Show panel info       (panel URL/port, base domain, nameserver, users)
#   5) Uninstall             (runs uninstall.sh, typed confirmation required)
#   0) Exit
#
# Run as root:  sudo bash menu.sh
# (install.sh also copies this menu to /opt/masterdns-panel/menu.sh, so after
#  installing you can run it from there too.)
# =============================================================================
set -uo pipefail

PANEL_DIR="/opt/masterdns-panel"
SERVER_DIR="/opt/masterdns"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say()  { echo -e "\033[1;34m[masterdns-panel]\033[0m $*"; }
ok()   { echo -e "\033[1;32m[done]\033[0m $*"; }
warn() { echo -e "\033[1;33m[warn]\033[0m $*"; }
die()  { echo -e "\033[1;31m[error]\033[0m $*" >&2; exit 1; }

[[ "$(id -u)" == "0" ]] || die "Run this menu as root (sudo bash menu.sh)."

pause() {
  read -rp "Press Enter to continue…" _ || true
  echo
}

# Echo the first existing copy of a repo script ($1): next to this menu, or
# in the installed panel dir (install.sh copies menu.sh + uninstall.sh there).
find_script() {
  if [[ -f "$SCRIPT_DIR/$1" ]]; then
    echo "$SCRIPT_DIR/$1"
  elif [[ -f "$PANEL_DIR/$1" ]]; then
    echo "$PANEL_DIR/$1"
  else
    echo ""
  fi
}

do_install() {
  local installer
  installer="$(find_script install.sh)"
  if [[ -z "$installer" ]]; then
    warn "install.sh was not found next to menu.sh or in $PANEL_DIR."
    warn "Run the menu from a git checkout of the repo to install/reinstall."
    return
  fi
  say "Running the installer ($installer)…"
  bash "$installer"
  ok "Installer finished."
}

do_restart() {
  say "Restarting masterdnsvpn + masterdns-panel…"
  systemctl restart masterdnsvpn.service \
    || warn "masterdnsvpn restart failed — check: journalctl -u masterdnsvpn -n 50"
  systemctl restart masterdns-panel.service \
    || warn "masterdns-panel restart failed — check: journalctl -u masterdns-panel -n 50"
  sleep 1
  if systemctl is-active --quiet masterdnsvpn.service; then
    ok "masterdnsvpn is active."
  else
    warn "masterdnsvpn is NOT active."
  fi
  if systemctl is-active --quiet masterdns-panel.service; then
    ok "masterdns-panel is active."
  else
    warn "masterdns-panel is NOT active."
  fi
}

do_status() {
  local svc state enabled
  for svc in masterdnsvpn.service masterdns-panel.service; do
    state="$(systemctl is-active "$svc" 2>/dev/null || true)"
    enabled="$(systemctl is-enabled "$svc" 2>/dev/null || true)"
    if [[ "$state" == "active" ]]; then
      ok "$svc: ${state} (${enabled:-unknown} at boot)"
    else
      warn "$svc: ${state:-unknown} (${enabled:-unknown} at boot)"
    fi
  done
  echo "Logs: journalctl -u masterdnsvpn -n 50 / journalctl -u masterdns-panel -n 50"
}

do_info() {
  local settings="$PANEL_DIR/settings.json" users="$PANEL_DIR/users.json"
  if [[ ! -f "$settings" ]]; then
    warn "No $settings — the panel does not look installed on this box yet."
    return
  fi
  local port="8080" found=""
  if [[ -f /etc/systemd/system/masterdns-panel.service ]]; then
    found="$(grep -oE -- '--port[= ][0-9]+' /etc/systemd/system/masterdns-panel.service \
             | grep -oE '[0-9]+' | head -n1 || true)"
    [[ -n "$found" ]] && port="$found"
  fi
  MDV_PANEL_PORT="$port" MDV_SETTINGS="$settings" MDV_USERS="$users" python3 - <<'PY'
import json, os

def load(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default

settings = load(os.environ["MDV_SETTINGS"], {})
users = load(os.environ["MDV_USERS"], [])
ip = settings.get("server_ip") or "YOUR_VPS_IP"
print(f"  Panel URL:    http://{ip}:{os.environ['MDV_PANEL_PORT']}")
print(f"  Base domain:  {settings.get('base_domain', '?')}")
print(f"  Nameserver:   {settings.get('ns_host', '?')}")
print(f"  Server IP:    {settings.get('server_ip', '?')}")
print(f"  Users:        {len(users)}")
PY
}

do_uninstall() {
  local uninstaller confirm
  uninstaller="$(find_script uninstall.sh)"
  if [[ -z "$uninstaller" ]]; then
    warn "uninstall.sh was not found next to menu.sh or in $PANEL_DIR — nothing to run."
    return
  fi
  warn "This stops and removes masterdnsvpn + masterdns-panel and deletes $PANEL_DIR and $SERVER_DIR."
  warn "A backup of your users/settings is kept under /root/ by the uninstaller."
  if ! read -rp "Type UNINSTALL to confirm: " confirm; then
    echo
    warn "Uninstall cancelled."
    return
  fi
  if [[ "$confirm" != "UNINSTALL" ]]; then
    warn "Confirmation did not match — uninstall cancelled."
    return
  fi
  say "Running the uninstaller ($uninstaller)…"
  bash "$uninstaller"
  ok "Uninstall finished — the panel is gone. Exiting the menu."
  exit 0
}

while true; do
  cat <<'EOF'

================ MasterDNS Panel menu ================
  1) Install / reinstall
  2) Restart services (masterdnsvpn + masterdns-panel)
  3) Service status
  4) Show panel info
  5) Uninstall
  0) Exit
=======================================================
EOF
  choice=""
  if ! read -rp "Choose an option: " choice; then
    echo
    exit 0
  fi
  case "$choice" in
    1) do_install ;;
    2) do_restart ;;
    3) do_status ;;
    4) do_info ;;
    5) do_uninstall ;;
    0) say "Bye."; exit 0 ;;
    *) warn "Invalid option: '$choice' — pick a number from the menu."; continue ;;
  esac
  pause
done
