#!/usr/bin/env bash
set -Eeuo pipefail

DOMAIN=""
SERVER_ADDRESS=""
FRP_PORT="7000"
REMOTE_PORT="18766"
FRP_VERSION="0.61.1"
FRP_SHA256=""
DRY_RUN="false"

usage() {
  cat <<'EOF'
Usage: sudo bash setup-relay-server.sh \
  --domain console.example.com \
  --server-address 203.0.113.10 \
  [--frp-port 7000] [--remote-port 18766] [--frp-version 0.61.1]

Installs frps, Nginx and a Let's Encrypt certificate on Ubuntu/Debian.
The script is idempotent: existing credentials are preserved and changed
configuration files are backed up before replacement.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    --server-address) SERVER_ADDRESS="${2:-}"; shift 2 ;;
    --frp-port) FRP_PORT="${2:-}"; shift 2 ;;
    --remote-port) REMOTE_PORT="${2:-}"; shift 2 ;;
    --frp-version) FRP_VERSION="${2:-}"; shift 2 ;;
    --frp-sha256) FRP_SHA256="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN="true"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

fail() { echo "[LPC] ERROR: $*" >&2; exit 1; }
log() { echo "[LPC] $*"; }

[[ $EUID -eq 0 ]] || fail "Run this script with sudo."
[[ "$DOMAIN" =~ ^[A-Za-z0-9.-]+$ && "$DOMAIN" == *.* ]] || fail "Invalid domain."
[[ "$SERVER_ADDRESS" =~ ^[A-Za-z0-9:.-]+$ ]] || fail "Invalid server address."
[[ "$FRP_PORT" =~ ^[0-9]+$ && "$FRP_PORT" -ge 1 && "$FRP_PORT" -le 65535 ]] || fail "Invalid FRP port."
[[ "$REMOTE_PORT" =~ ^[0-9]+$ && "$REMOTE_PORT" -ge 1 && "$REMOTE_PORT" -le 65535 ]] || fail "Invalid remote port."
[[ "$FRP_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([+-][A-Za-z0-9.-]+)?$ ]] || fail "Invalid FRP version."

case "$(uname -m)" in
  x86_64|amd64) FRP_ARCH="amd64" ;;
  aarch64|arm64) FRP_ARCH="arm64" ;;
  *) fail "Unsupported CPU architecture: $(uname -m)" ;;
esac

if [[ "$DRY_RUN" == "true" ]]; then
  log "Dry run passed."
  log "Domain: $DOMAIN"
  log "Server: $SERVER_ADDRESS"
  log "FRP: v$FRP_VERSION / $FRP_ARCH / port $FRP_PORT"
  log "HTTPS upstream: 127.0.0.1:$REMOTE_PORT"
  exit 0
fi

command -v apt-get >/dev/null 2>&1 || fail "This installer currently supports Ubuntu and Debian."
export DEBIAN_FRONTEND=noninteractive
log "Installing operating-system packages..."
apt-get update -qq
apt-get install -y -qq ca-certificates curl nginx certbot python3 python3-certbot-nginx openssl tar

install -d -m 0755 /opt/frp /etc/frp /var/lib/frp
if ! id -u frp >/dev/null 2>&1; then
  useradd --system --home /var/lib/frp --shell /usr/sbin/nologin frp
fi
chown frp:frp /var/lib/frp

asset_name="frp_${FRP_VERSION}_linux_${FRP_ARCH}.tar.gz"
release_api="https://api.github.com/repos/fatedier/frp/releases/tags/v${FRP_VERSION}"
temporary_dir="$(mktemp -d)"
trap 'rm -rf -- "$temporary_dir"' EXIT

log "Resolving the official FRP release asset..."
curl --proto '=https' --tlsv1.2 -fsSL "$release_api" -o "$temporary_dir/release.json"
readarray -t asset_data < <(
  python3 - "$temporary_dir/release.json" "$asset_name" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    release = json.load(handle)
asset = next((item for item in release.get("assets", []) if item.get("name") == sys.argv[2]), None)
if asset is None:
    raise SystemExit("release asset not found")
print(asset.get("browser_download_url", ""))
print(asset.get("digest", ""))
PY
)
asset_url="${asset_data[0]:-}"
asset_digest="${asset_data[1]:-}"
[[ "$asset_url" == https://github.com/* ]] || fail "The FRP release asset URL is invalid."
curl --proto '=https' --tlsv1.2 -fL "$asset_url" -o "$temporary_dir/$asset_name"

expected_sha="$FRP_SHA256"
if [[ -z "$expected_sha" && "$asset_digest" == sha256:* ]]; then
  expected_sha="${asset_digest#sha256:}"
fi
[[ "$expected_sha" =~ ^[A-Fa-f0-9]{64}$ ]] || fail "The release did not provide a SHA-256 digest. Pass --frp-sha256 explicitly."
actual_sha="$(sha256sum "$temporary_dir/$asset_name" | awk '{print $1}')"
[[ "${actual_sha,,}" == "${expected_sha,,}" ]] || fail "FRP download checksum mismatch."

tar -xzf "$temporary_dir/$asset_name" -C "$temporary_dir"
install -m 0755 "$temporary_dir/frp_${FRP_VERSION}_linux_${FRP_ARCH}/frps" /opt/frp/frps

existing_token=""
if [[ -f /etc/frp/frps.toml ]]; then
  existing_token="$(sed -n 's/^[[:space:]]*token[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' /etc/frp/frps.toml | head -n 1)"
fi
frp_token="${existing_token:-$(openssl rand -hex 32)}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"

backup_if_present() {
  local target="$1"
  if [[ -f "$target" ]]; then
    cp -a -- "$target" "${target}.${timestamp}.bak"
  fi
}

backup_if_present /etc/frp/frps.toml
cat >/etc/frp/frps.toml <<EOF
bindAddr = "0.0.0.0"
bindPort = ${FRP_PORT}
proxyBindAddr = "127.0.0.1"

[auth]
method = "token"
token = "${frp_token}"

[transport.tls]
force = true
EOF
chmod 0600 /etc/frp/frps.toml
chown root:frp /etc/frp/frps.toml

backup_if_present /etc/systemd/system/lpc-frps.service
cat >/etc/systemd/system/lpc-frps.service <<'EOF'
[Unit]
Description=FRP relay for Local Project Console
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=frp
Group=frp
ExecStart=/opt/frp/frps -c /etc/frp/frps.toml
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/frp

[Install]
WantedBy=multi-user.target
EOF

nginx_site="/etc/nginx/sites-available/local-project-console"
backup_if_present "$nginx_site"
cat >"$nginx_site" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};

    location / {
        proxy_pass http://127.0.0.1:${REMOTE_PORT};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 60s;
        proxy_send_timeout 60s;
    }
}
EOF
ln -sfn "$nginx_site" /etc/nginx/sites-enabled/local-project-console
nginx -t

systemctl daemon-reload
systemctl enable --now lpc-frps.service
systemctl enable --now nginx.service

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q '^Status: active'; then
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
  ufw allow "${FRP_PORT}/tcp" >/dev/null
fi

log "Requesting or renewing the HTTPS certificate..."
certbot --nginx --non-interactive --agree-tos --register-unsafely-without-email \
  --redirect --keep-until-expiring -d "$DOMAIN"

systemctl is-active --quiet lpc-frps.service || fail "frps did not become active."
systemctl is-active --quiet nginx.service || fail "Nginx did not become active."

bundle="$({
  LPC_SERVER_ADDRESS="$SERVER_ADDRESS" \
  LPC_FRP_PORT="$FRP_PORT" \
  LPC_REMOTE_PORT="$REMOTE_PORT" \
  LPC_FRP_VERSION="$FRP_VERSION" \
  LPC_PUBLIC_URL="https://${DOMAIN}" \
  LPC_FRP_TOKEN="$frp_token" \
  python3 - <<'PY'
import base64
import json
import os

payload = {
    "format": "lpc-frp-v1",
    "serverAddr": os.environ["LPC_SERVER_ADDRESS"],
    "serverPort": int(os.environ["LPC_FRP_PORT"]),
    "remotePort": int(os.environ["LPC_REMOTE_PORT"]),
    "frpVersion": os.environ["LPC_FRP_VERSION"],
    "publicUrl": os.environ["LPC_PUBLIC_URL"],
    "token": os.environ["LPC_FRP_TOKEN"],
}
encoded = base64.urlsafe_b64encode(
    json.dumps(payload, separators=(",", ":")).encode("utf-8")
).decode("ascii").rstrip("=")
print(encoded)
PY
} )"

echo
log "Relay installation completed."
echo "LPC_CONFIG_BUNDLE=${bundle}"
echo "Copy only the value after LPC_CONFIG_BUNDLE= back to the desktop wizard."
