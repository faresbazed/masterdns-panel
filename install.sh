#!/usr/bin/env bash
# =============================================================================
# MasterDNS Panel — one-shot installer for a fresh VPS (Debian/Ubuntu, RHEL
# family works too). It:
#   1. asks for your base domain + nameserver host (or reads them from env)
#   2. installs all requirements (python3, curl, unzip, firewall tools)
#   3. downloads the official MasterDnsVPN server binary (upstream releases)
#   4. frees UDP/TCP port 53 (systemd-resolved stub listener is disabled)
#   5. installs the panel + a systemd service for the server and the panel
#   6. opens the firewall ports: 53/udp, 53/tcp and the panel port
#
# Non-interactive use:
#   MDV_BASE_DOMAIN=example.com MDV_NS_HOST=ns.example.com \
#   MDV_ADMIN_PASSWORD='choose-a-password' sudo -E bash install.sh
# Optional env: MDV_PANEL_PORT (default 8080), MDV_ENCRYPTION_METHOD (default 5)
# =============================================================================
set -euo pipefail

PANEL_PORT="${MDV_PANEL_PORT:-8080}"
ENC_METHOD="${MDV_ENCRYPTION_METHOD:-5}"
SERVER_DIR="/opt/masterdns"
PANEL_DIR="/opt/masterdns-panel"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say()  { echo -e "\033[1;34m[masterdns-panel]\033[0m $*"; }
ok()   { echo -e "\033[1;32m[done]\033[0m $*"; }
warn() { echo -e "\033[1;33m[warn]\033[0m $*"; }
die()  { echo -e "\033[1;31m[error]\033[0m $*" >&2; exit 1; }

[[ "$(id -u)" == "0" ]] || die "Run this installer as root (sudo bash install.sh)."

# ---------------------------------------------------------------- questions
BASE_DOMAIN="${MDV_BASE_DOMAIN:-}"
if [[ -z "$BASE_DOMAIN" ]]; then
  read -rp "Base domain that is on Cloudflare (e.g. example.com): " BASE_DOMAIN
fi
[[ -n "$BASE_DOMAIN" ]] || die "A base domain is required."

NS_HOST="${MDV_NS_HOST:-ns.$BASE_DOMAIN}"
if [[ -z "${MDV_NS_HOST:-}" && -t 0 ]]; then
  read -rp "Nameserver host [ns.$BASE_DOMAIN]: " _ns
  [[ -n "${_ns:-}" ]] && NS_HOST="$_ns"
fi

ADMIN_PASSWORD="${MDV_ADMIN_PASSWORD:-}"
if [[ -z "$ADMIN_PASSWORD" ]]; then
  if [[ -t 0 ]]; then
    read -rsp "Choose an admin password for the panel: " ADMIN_PASSWORD; echo
  fi
  if [[ -z "$ADMIN_PASSWORD" ]]; then
    ADMIN_PASSWORD="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' | head -c 16)"
    warn "No admin password given — generated one (shown once at the end): $ADMIN_PASSWORD"
  fi
fi

# If the requested panel port is already taken on this box (e.g. a leftover
# nginx/landing page on 8080), walk up to the next free port instead of
# crash-looping the panel service after install.
port_in_use() { ss -ltn 2>/dev/null | grep -q ":$1[[:space:]]"; }
if port_in_use "$PANEL_PORT"; then
  _orig="$PANEL_PORT"
  while port_in_use "$PANEL_PORT"; do PANEL_PORT=$((PANEL_PORT + 1)); done
  warn "Port $_orig is already in use on this server — the panel will use port $PANEL_PORT instead."
fi

PUBLIC_IP="$(curl -4 -s --max-time 8 https://api.ipify.org || true)"

# ---------------------------------------------------------------- packages
say "Installing requirements…"
if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq python3 curl unzip ca-certificates ufw >/dev/null || \
    apt-get install -y python3 curl unzip ca-certificates
elif command -v dnf >/dev/null; then
  dnf -y install python3 curl unzip ca-certificates >/dev/null
elif command -v yum >/dev/null; then
  yum -y install python3 curl unzip ca-certificates >/dev/null
else
  die "Unsupported OS: need apt, dnf or yum."
fi
ok "Requirements installed (python3, curl, unzip)."

# ---------------------------------------------------------------- server binary
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64)  PREFIX="MasterDnsVPN_Server_Linux_AMD64" ;;
  aarch64|arm64) PREFIX="MasterDnsVPN_Server_Linux_ARM64" ;;
  armv7l|armv7*)  PREFIX="MasterDnsVPN_Server_Linux_ARMV7" ;;
  i386|i686)     PREFIX="MasterDnsVPN_Server_Linux_X86" ;;
  *) die "Unsupported architecture: $ARCH" ;;
esac

say "Downloading the official MasterDnsVPN server ($PREFIX)…"
mkdir -p "$SERVER_DIR"
TMP="$(mktemp -d)"
curl -fLs -o "$TMP/server.zip" \
  "https://github.com/masterking32/MasterDnsVPN/releases/latest/download/${PREFIX}.zip" \
  || die "Could not download MasterDnsVPN server release."
unzip -o -q "$TMP/server.zip" -d "$TMP/server"
BIN="$(ls "$TMP"/server/${PREFIX}_v* | head -n1)"
install -m 0755 "$BIN" "$SERVER_DIR/masterdns-server"
rm -rf "$TMP"
ok "MasterDnsVPN server installed to $SERVER_DIR/masterdns-server"

# ---------------------------------------------------------------- free port 53
# Ubuntu/Debian run a systemd-resolved stub listener on 127.0.0.53:53 — that is
# on loopback only and does not block us, but some images bind 0.0.0.0:53 via
# the stub or another DNS daemon. Follow upstream's approach: disable the
# stub listener (backing up the config first) and stop known DNS daemons.
if systemctl is-active --quiet systemd-resolved 2>/dev/null; then
  say "Disabling systemd-resolved stub listener so port 53 is free…"
  if [[ -f /etc/systemd/resolved.conf && ! -f /etc/systemd/resolved.conf.bak ]]; then
    cp -a /etc/systemd/resolved.conf /etc/systemd/resolved.conf.bak
  fi
  if grep -q '^#\?DNSStubListener=' /etc/systemd/resolved.conf 2>/dev/null; then
    sed -i 's/^#\?DNSStubListener=.*/DNSStubListener=no/' /etc/systemd/resolved.conf
  else
    echo 'DNSStubListener=no' >> /etc/systemd/resolved.conf
  fi
  systemctl restart systemd-resolved || true
fi
for svc in bind9 named dnsmasq unbound pdns knot-resolver dnscrypt-proxy smartdns coredns; do
  if systemctl is-active --quiet "$svc" 2>/dev/null; then
    warn "$svc is using port 53 — stopping and disabling it."
    systemctl disable --now "$svc" || true
  fi
done

# ---------------------------------------------------------------- panel files
say "Installing the panel…"
mkdir -p "$PANEL_DIR"
install -m 0644 "$SCRIPT_DIR/panel/app.py" "$PANEL_DIR/app.py"
install -m 0644 "$SCRIPT_DIR/panel/server_config.template.toml" "$PANEL_DIR/server_config.template.toml"
# uninstall.sh is copied so the web panel's danger-zone Uninstall button can
# run it after install; menu.sh is copied so the terminal menu is available
# on the box too (sudo bash /opt/masterdns-panel/menu.sh).
install -m 0755 "$SCRIPT_DIR/uninstall.sh" "$PANEL_DIR/uninstall.sh"
install -m 0755 "$SCRIPT_DIR/menu.sh" "$PANEL_DIR/menu.sh"

MDV_HOME="$PANEL_DIR" MDV_SERVER_DIR="$SERVER_DIR" MDV_RESTART_CMD="" \
  python3 "$PANEL_DIR/app.py" init \
    --base-domain "$BASE_DOMAIN" \
    --ns-host "$NS_HOST" \
    --admin-password "$ADMIN_PASSWORD" \
    --server-ip "$PUBLIC_IP" \
    --encryption-method "$ENC_METHOD"
ok "Panel initialised (encryption method $ENC_METHOD, key generated)."

# ---------------------------------------------------------------- systemd
cat > /etc/systemd/system/masterdnsvpn.service <<EOF
[Unit]
Description=MasterDnsVPN Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$SERVER_DIR
ExecStart=$SERVER_DIR/masterdns-server
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/masterdns-panel.service <<EOF
[Unit]
Description=MasterDNS Panel
After=network-online.target masterdnsvpn.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$PANEL_DIR
Environment=MDV_HOME=$PANEL_DIR
Environment=MDV_SERVER_DIR=$SERVER_DIR
ExecStart=/usr/bin/python3 $PANEL_DIR/app.py serve --port $PANEL_PORT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now masterdnsvpn.service
systemctl enable --now masterdns-panel.service
ok "Services started: masterdnsvpn + masterdns-panel"

# ---------------------------------------------------------------- firewall
say "Opening firewall ports (53/udp, 53/tcp, $PANEL_PORT/tcp)…"
if command -v ufw >/dev/null; then
  ufw allow 22/tcp >/dev/null || true
  ufw allow 53/udp >/dev/null
  ufw allow 53/tcp >/dev/null
  ufw allow "$PANEL_PORT"/tcp >/dev/null
  ufw --force enable >/dev/null || true
elif command -v firewall-cmd >/dev/null; then
  firewall-cmd --permanent --add-port=53/udp >/dev/null
  firewall-cmd --permanent --add-port=53/tcp >/dev/null
  firewall-cmd --permanent --add-port="$PANEL_PORT"/tcp >/dev/null
  firewall-cmd --reload >/dev/null || true
else
  iptables -C INPUT -p udp --dport 53 -j ACCEPT 2>/dev/null || iptables -A INPUT -p udp --dport 53 -j ACCEPT
  iptables -C INPUT -p tcp --dport 53 -j ACCEPT 2>/dev/null || iptables -A INPUT -p tcp --dport 53 -j ACCEPT
  iptables -C INPUT -p tcp --dport "$PANEL_PORT" -j ACCEPT 2>/dev/null || iptables -A INPUT -p tcp --dport "$PANEL_PORT" -j ACCEPT
fi
ok "Firewall configured. NOTE: if your VPS provider has its own cloud firewall, open the same ports there too."

sleep 2
systemctl is-active --quiet masterdnsvpn.service && ok "MasterDnsVPN server is running." \
  || warn "MasterDnsVPN service is not active yet — check: journalctl -u masterdnsvpn -n 50"
systemctl is-active --quiet masterdns-panel.service && ok "Panel is running." \
  || warn "Panel service is not active yet — check: journalctl -u masterdns-panel -n 50"

cat <<EOF

================================================================================
  MasterDNS Panel is installed.

  Panel:        http://${PUBLIC_IP:-YOUR_VPS_IP}:$PANEL_PORT
  Admin pass:   (the one you chose / generated above)
  Nameserver:   $NS_HOST
  Base domain:  $BASE_DOMAIN

  Now create these records in Cloudflare (DNS settings of $BASE_DOMAIN):

    1.  $NS_HOST        A     ${PUBLIC_IP:-YOUR_VPS_IP}     DNS only (grey cloud!)
    2.  For every user you create in the panel, one NS record, e.g.:
        <user>.$BASE_DOMAIN   NS   $NS_HOST               DNS only

  The panel's home page lists the exact records for your users.
  Full instructions: see README.md in the GitHub repo.
================================================================================
EOF
