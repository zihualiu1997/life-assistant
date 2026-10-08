#!/usr/bin/env bash
# Real encryption/restore test with fictional records only; never points at user volumes.
set -euo pipefail
umask 077
fixture=$(mktemp -d /tmp/life-restic-fixture.XXXXXXXX)
mkdir "$fixture/source" "$fixture/restore"
printf 'FICTIONAL_USER_ONE\n' > "$fixture/source/note.txt"
head -c 48 /dev/urandom | base64 > "$fixture/password"
export RESTIC_REPOSITORY="$fixture/repository"
export RESTIC_PASSWORD_FILE="$fixture/password"
restic init >/dev/null
restic backup "$fixture/source" --tag tenant:fixture >/dev/null
restic check --read-data >/dev/null
restic restore "latest:$fixture/source" --target "$fixture/restore" --verify >/dev/null
cmp "$fixture/source/note.txt" "$fixture/restore/note.txt"
printf 'WRONG-FICTIONAL-PASSWORD\n' > "$fixture/wrong-password"
if RESTIC_PASSWORD_FILE="$fixture/wrong-password" restic snapshots >/dev/null 2>&1; then
  echo 'FAIL: wrong password opened backup'; exit 1
fi
echo 'PASS: fictional encrypted snapshot, full repository check, verified restore, wrong-password rejection'
echo "Fixture artifacts retained at $fixture"
