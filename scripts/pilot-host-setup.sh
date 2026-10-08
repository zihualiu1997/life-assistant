#!/usr/bin/env bash
# Run only inside a newly created pilot host. Never on the personal assistant host.
set -euo pipefail
[[ ${1:-} == --new-pilot-host ]] || { echo 'Explicit --new-pilot-host required'; exit 2; }
[[ $EUID == 0 ]] || { echo 'Root required'; exit 2; }
source /etc/os-release
case "$ID:$VERSION_ID" in ubuntu:24.04|debian:13) ;; *) echo 'Requires Ubuntu 24.04 or Debian 13'; exit 2;; esac
[[ ! -e /opt/life-assistant/current ]] || { echo 'Existing assistant detected; refusing'; exit 2; }
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl python3-venv restic
install -m 0755 -d /etc/apt/keyrings
curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/$ID
Suites: $VERSION_CODENAME
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
EOF
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
docker version
docker compose version
restic version
