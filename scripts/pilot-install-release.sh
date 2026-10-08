#!/usr/bin/env bash
# Install the host-side operator tools only. Does not start or enable services.
set -euo pipefail
umask 077
[[ ${1:-} == --new-pilot-host && $# == 1 ]] || { echo 'Usage: bash scripts/pilot-install-release.sh --new-pilot-host'; exit 2; }
[[ $EUID == 0 ]] || { echo 'Root required'; exit 2; }
source /etc/os-release
case "$ID:$VERSION_ID" in ubuntu:24.04|debian:13) ;; *) echo 'Requires Ubuntu 24.04 or Debian 13'; exit 2;; esac
[[ ! -e /opt/life-assistant/current ]] || { echo 'Existing personal assistant detected; refusing'; exit 2; }
python3 -c 'import sys; assert sys.version_info >= (3,12), "Python 3.12 or newer required"'
docker info --format '{{.ServerVersion}}' >/dev/null
docker compose version --short >/dev/null
restic version >/dev/null
pilot_source=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
pilot_target=/opt/life-pilot
pilot_version=$(python3 -c 'import sys,tomllib; p=tomllib.load(open(sys.argv[1],"rb"))["project"]; assert p["name"]=="life-assistant"; print(p["version"])' "$pilot_source/pyproject.toml")
[[ $pilot_version =~ ^[0-9A-Za-z.+-]+$ ]] || { echo 'Invalid release version'; exit 2; }
[[ ! -L $pilot_target ]] || { echo 'Install target must not be a symlink'; exit 2; }
if [[ -e $pilot_target ]]; then
  [[ -f $pilot_target/.installing && ! -e $pilot_target/installed.json ]] || { echo 'Target already exists; this installer never overwrites an installed release'; exit 2; }
  [[ $(cat "$pilot_target/.installing") == "$pilot_version" ]] || { echo 'Incomplete installation belongs to another version'; exit 2; }
else
  mkdir -m 0700 "$pilot_target"
  printf '%s\n' "$pilot_version" > "$pilot_target/.installing"
fi
python3 -m venv "$pilot_target/venv"
"$pilot_target/venv/bin/python" -m pip install --disable-pip-version-check -c "$pilot_source/requirements.lock" "$pilot_source"
"$pilot_target/venv/bin/life-fleet" --help >/dev/null
install -d -m 0700 "$pilot_target/deploy" "$pilot_target/docs" "$pilot_target/bin"
install -m 0600 "$pilot_source"/deploy/life-pilot*.service "$pilot_source"/deploy/life-pilot*.timer "$pilot_target/deploy/"
install -m 0600 "$pilot_source/deploy/ingress-images.json" "$pilot_target/deploy/"
install -m 0600 "$pilot_source"/docs/pilot-*.md "$pilot_target/docs/"
install -m 0700 "$pilot_source/scripts/life-admin" "$pilot_target/bin/life-admin"
"$pilot_target/venv/bin/python" -c 'import importlib.metadata,json,platform,sys; from pathlib import Path; p=Path(sys.argv[1]); (p/"installed.json").write_text(json.dumps({"version":importlib.metadata.version("life-assistant"),"python":platform.python_version(),"services_enabled":False},indent=2)); (p/".installing").unlink()' "$pilot_target"
printf 'Installed operator tools: %s/venv/bin/life-fleet\nNo services enabled. Configure credentials locally before rendering deployment.\n' "$pilot_target"
