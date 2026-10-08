"""Actual encrypted backup/restore exercise, restricted to marked fictional data."""
import argparse
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import subprocess
import tempfile
from life_fleet.store import Fleet
from life_fleet.operations import Operations


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--repository", required=True)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if not (root / "FICTIONAL-TEST-ONLY").is_file(): raise ValueError("fixture_marker_required")
    repository = Path(args.repository).resolve()
    if repository.name != "fixture": raise ValueError("fixture_repository_required")
    password = root / "restic-password"
    if not password.exists():
        password.write_text(secrets.token_urlsafe(48))
        password.chmod(0o600)
    repository.mkdir(parents=True, exist_ok=True)
    restic = ["restic", "--repo", str(repository), "--password-file", str(password)]
    if not (repository / "config").exists(): subprocess.run([*restic, "init"], check=True, capture_output=True)
    (root / "backup.json").write_text(json.dumps({"repository": str(repository), "password_file": str(password)}))
    fleet = Fleet(root)
    operation = Operations(fleet)
    receipt = operation.backup("fixture0")
    operator_receipt = operation.backup_operator()
    operation.freeze("fixture0")
    target = Path(tempfile.mkdtemp(prefix="life-fixture-restored-"))
    result = operation.restore("fixture0", receipt["snapshot"], target)
    with sqlite3.connect(target / ".local/state.sqlite3") as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    relay = json.loads((target / ".local/relay-snapshot.json").read_text())
    assert relay["tenant"] == "fixture0"
    assert relay["usage"] and all(item["tenant"] == "fixture0" for item in relay["usage"])
    metadata = json.loads(subprocess.check_output(["docker", "volume", "inspect", operation.volume_name("fixture0")], text=True))[0]
    source = Path(metadata["Mountpoint"])
    verified = 0
    for path in source.rglob("*"):
        if path.is_file() and path.suffix == ".md":
            assert hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256((target / path.relative_to(source)).read_bytes()).digest()
            verified += 1
    assert verified > 0
    subprocess.run([*restic, "check", "--read-data"], check=True, capture_output=True)
    operation.start("fixture0")
    report = {"user_snapshot": receipt, "operator_snapshot": operator_receipt,
              "restored_database_integrity": "ok", "markdown_files_compared": verified,
              "per_user_relay_records_preserved": True, "encrypted_repository_checked": True,
              "restored_to": str(target), "real_account_restoration": False}
    (root / "backup-check.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__": main()
