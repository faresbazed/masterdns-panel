#!/usr/bin/env bash
# Removes MasterDNS Panel + the MasterDnsVPN server it installed.
# Keeps a copy of your panel data (users/settings) in /root/masterdns-panel-backup-<date>.
set -euo pipefail
[[ "$(id -u)" == "0" ]] || { echo "Run as root."; exit 1; }

systemctl disable --now masterdns-panel.service 2>/dev/null || true
systemctl disable --now masterdnsvpn.service 2>/dev/null || true
rm -f /etc/systemd/system/masterdns-panel.service /etc/systemd/system/masterdnsvpn.service
systemctl daemon-reload

BACKUP="/root/masterdns-panel-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP"
cp -a /opt/masterdns-panel/settings.json /opt/masterdns-panel/users.json "$BACKUP/" 2>/dev/null || true
cp -a /opt/masterdns/server_config.toml /opt/masterdns/encrypt_key.txt "$BACKUP/" 2>/dev/null || true
rm -rf /opt/masterdns-panel /opt/masterdns

if command -v ufw >/dev/null; then
  ufw delete allow 53/udp >/dev/null 2>&1 || true
  ufw delete allow 53/tcp >/dev/null 2>&1 || true
fi
echo "Removed. Panel data backed up in $BACKUP"
echo "Note: Cloudflare DNS records and the systemd-resolved stub setting were left as they are."
