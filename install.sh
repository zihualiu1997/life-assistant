#!/usr/bin/env bash
set -euo pipefail
umask 077
[[ $EUID == 0 ]] || { echo 'Run: sudo bash install.sh'; exit 1; }
[[ $(uname -m) == x86_64 ]] || { echo 'Only x86-64 is supported.'; exit 1; }
source /etc/os-release
[[ ($ID == ubuntu && $VERSION_ID == 24.04) || ($ID == debian && $VERSION_ID == 13) ]] || { echo 'Supported: Ubuntu 24.04 / Debian 13'; exit 1; }
SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
read -r -p 'Domain pointing to this server (for example life.example.com): ' DOMAIN
[[ $DOMAIN =~ ^([a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}$ ]] || { echo 'Invalid domain.'; exit 1; }
getent ahosts "$DOMAIN" >/dev/null || { echo 'DNS lookup failed. Configure DNS first.'; exit 1; }
if [[ -e /etc/caddy/Caddyfile ]] && ! grep -q '^# Managed by life-assistant$' /etc/caddy/Caddyfile; then
  echo 'Existing Caddy configuration found; refusing to replace it. See docs/operations.md.'; exit 1
fi
# Re-running an interrupted install may find our own Caddy already listening.
# Verify every listening PID belongs to the expected service, rather than
# exempting all ports just because one application service is running.
for PORT in 80 443 18932 18789; do
  case "$PORT" in 80|443) UNIT=caddy.service ;; 18932) UNIT=life-app.service ;; 18789) UNIT=life-gateway.service ;; esac
  while IFS= read -r LISTENER; do
    [[ -n $LISTENER ]] || continue
    [[ $LISTENER =~ pid=([0-9]+) ]] || { echo "Cannot identify owner of port $PORT"; exit 1; }
    PID=${BASH_REMATCH[1]}
    grep -Fq "/$UNIT" "/proc/$PID/cgroup" || { echo "Port $PORT belongs to another service; refusing to replace it."; exit 1; }
  done < <(ss -ltnpH "sport = :$PORT")
done
GATEWAY_WAS_ACTIVE=0
if systemctl is-active --quiet life-gateway.service; then GATEWAY_WAS_ACTIVE=1; fi
apt-get update
apt-get install -y python3 python3-venv ca-certificates curl xz-utils sudo caddy
id life-assistant >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/life-assistant --shell /usr/sbin/nologin life-assistant
install -d -m 700 -o life-assistant -g life-assistant /var/lib/life-assistant
install -d -m 755 /opt/life-assistant/releases /opt/life-assistant/runtime
NODE_VERSION=24.19.0
NODE_NAME="node-v${NODE_VERSION}-linux-x64"
TEMP=$(mktemp -d)
trap 'rm -rf -- "$TEMP"' EXIT
curl --fail --location --proto '=https' --tlsv1.2 "https://nodejs.org/dist/v${NODE_VERSION}/${NODE_NAME}.tar.xz" -o "$TEMP/node.tar.xz"
curl --fail --location --proto '=https' --tlsv1.2 "https://nodejs.org/dist/v${NODE_VERSION}/SHASUMS256.txt" -o "$TEMP/sums"
EXPECTED=$(awk -v name="${NODE_NAME}.tar.xz" '$2==name {print $1}' "$TEMP/sums")
[[ $EXPECTED =~ ^[a-f0-9]{64}$ ]] || { echo 'Missing Node checksum.'; exit 1; }
echo "$EXPECTED  $TEMP/node.tar.xz" | sha256sum --check --status
if [[ ! -d /opt/life-assistant/runtime/$NODE_NAME ]]; then tar -xJf "$TEMP/node.tar.xz" -C /opt/life-assistant/runtime; fi
ln -sfn "/opt/life-assistant/runtime/$NODE_NAME" /opt/life-assistant/runtime/node
export PATH="/opt/life-assistant/runtime/node/bin:$PATH"
RELEASE="/opt/life-assistant/releases/$(date -u +%Y%m%dT%H%M%S)"
install -d -m 755 "$RELEASE"
cp -a "$SOURCE/server" "$SOURCE/openclaw-bridge" "$SOURCE/gateway" "$SOURCE/pyproject.toml" "$SOURCE/requirements.lock" "$RELEASE/"
python3 -m venv "$RELEASE/venv"
"$RELEASE/venv/bin/python" -m pip install --disable-pip-version-check -r "$RELEASE/requirements.lock"
"$RELEASE/venv/bin/python" -m pip install --disable-pip-version-check --no-deps "$RELEASE"
npm ci --prefix "$RELEASE/gateway" --omit=dev --ignore-scripts=false --audit=false --fund=false
chmod -R o+rX "$RELEASE" /opt/life-assistant/runtime
if [[ -L /opt/life-assistant/current ]]; then
  systemctl stop life-app.service life-gateway.service || true
  ln -sfn "$(readlink -f /opt/life-assistant/current)" /opt/life-assistant/previous
fi
ln -sfn "$RELEASE" /opt/life-assistant/current
install -d -m 755 /etc/life-assistant
cat > /etc/life-assistant/environment <<EOF
LIFE_DATA_DIR=/var/lib/life-assistant
LIFE_PUBLIC_ORIGIN=https://$DOMAIN
LIFE_OPENCLAW_BIN=/opt/life-assistant/current/gateway/node_modules/.bin/openclaw
LIFE_BRIDGE_DIR=/opt/life-assistant/current/openclaw-bridge
LIFE_WECHAT_PLUGIN_DIR=/opt/life-assistant/current/gateway/node_modules/@tencent-weixin/openclaw-weixin
OPENCLAW_STATE_DIR=/var/lib/life-assistant/.local/openclaw
OPENCLAW_CONFIG_PATH=/var/lib/life-assistant/.secrets/openclaw.json
PATH=/opt/life-assistant/runtime/node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
EOF
chmod 644 /etc/life-assistant/environment
for SERVICE in app gateway; do
  PRECHECK=''
  if [[ $SERVICE == app ]]; then COMMAND='/opt/life-assistant/current/venv/bin/life serve'; else COMMAND='/opt/life-assistant/current/gateway/node_modules/.bin/openclaw gateway run'; PRECHECK='ExecStartPre=/opt/life-assistant/current/venv/bin/life gateway-check'; fi
  cat > "/etc/systemd/system/life-${SERVICE}.service" <<EOF
[Unit]
Description=Life Assistant $SERVICE
After=network-online.target
Wants=network-online.target
[Service]
User=life-assistant
Group=life-assistant
EnvironmentFile=/etc/life-assistant/environment
WorkingDirectory=/var/lib/life-assistant
ExecStart=$COMMAND
$PRECHECK
Restart=on-failure
RestartSec=10
UMask=0077
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
StandardOutput=null
StandardError=null
[Install]
WantedBy=multi-user.target
EOF
done
echo 'life-assistant ALL=(root) NOPASSWD: /usr/bin/systemctl restart life-gateway.service' > /etc/sudoers.d/life-assistant
chmod 440 /etc/sudoers.d/life-assistant
visudo -cf /etc/sudoers.d/life-assistant >/dev/null
cat > /etc/caddy/Caddyfile <<EOF
# Managed by life-assistant
$DOMAIN {
  @internal path /internal/*
  respond @internal 404
  reverse_proxy 127.0.0.1:18932
}
EOF
chmod 644 /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile
install -m 755 "$SOURCE/scripts/life-admin" /usr/local/bin/life-admin
systemctl daemon-reload
systemctl enable --now life-app.service caddy.service
systemctl enable life-gateway.service
systemctl reload caddy.service
curl --fail --silent --retry 10 --retry-connrefused --retry-delay 1 http://127.0.0.1:18932/api/bootstrap >/dev/null || { echo 'Application health check failed; inspect systemctl status life-app before continuing.'; exit 1; }
if [[ $GATEWAY_WAS_ACTIVE == 1 ]]; then systemctl start life-gateway.service; fi
echo "Open https://$DOMAIN after its certificate is issued. Cloud firewall must allow TCP 80 and 443."
sudo -u life-assistant /opt/life-assistant/current/venv/bin/life bootstrap
