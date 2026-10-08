"""Real broker upgrade/failure/rollback exercise on a new fictional project."""
import argparse
import json
import subprocess
import re
from pathlib import Path
from life_fleet.store import Fleet
from life_fleet.operations import Operations


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--failing-image", required=True)
    parser.add_argument("--backup-config", required=True, help="Fictional test repository configuration only")
    args = parser.parse_args()
    backup_source = Path(args.backup_config).resolve()
    if not (backup_source.parent / "FICTIONAL-TEST-ONLY").is_file(): raise ValueError("fictional_backup_configuration_required")
    backup_config = json.loads(backup_source.read_text())
    repository = Path(backup_config["repository"])
    if not repository.is_absolute(): raise ValueError("local_fixture_repository_required")
    root = Path(args.root).resolve()
    if not re.fullmatch(r"life-pilot-[a-z0-9-]{1,45}", root.name): raise ValueError("explicit_pilot_fixture_name_required")
    if root.exists(): raise ValueError("new_fixture_root_required")
    isolated_repository = repository.parent / (repository.name + "-broker-upgrade-" + root.name)
    if isolated_repository.exists(): raise ValueError("new_fixture_repository_required")
    backup_config["repository"] = str(isolated_repository)
    subprocess.run(["restic", "--repo", str(isolated_repository), "--password-file", backup_config["password_file"], "init"], check=True, capture_output=True)
    fleet = Fleet(root)
    (root / "FICTIONAL-TEST-ONLY").write_text("Broker release exercise only")
    (root / "operator.json").write_text(json.dumps({"base_url": "https://fixture.invalid/v1", "api_key": "fictional", "model": "fixture-model", "asr_model": "fixture-asr"}))
    (root / "backup.json").write_text(json.dumps(backup_config))
    for name in ("brokercheck0", "brokercheck1"): fleet.create(name)
    spec = fleet.compose(args.image, "fixture.invalid")
    spec["name"] = root.name
    for network in spec["networks"].values(): network["internal"] = True
    broker = spec["services"]["broker"]
    broker["entrypoint"] = ["python", "/fixture-broker.py"]
    broker["volumes"].append({"type": "bind", "source": str(Path(__file__).resolve().parents[1] / "tests/container_fixture_broker.py"), "target": "/fixture-broker.py", "read_only": True})
    (root / "compose.json").write_text(json.dumps(spec))
    ops = Operations(fleet)
    for name in ("brokercheck0", "brokercheck1"):
        ops.start(name)
        ops.health(name)
    ops.freeze("brokercheck1")
    manifest = {"image": args.image, "version": "fixture-broker-release", "data_format": 1, "compatible_from": [1], "broker_api_version": 1}
    success = ops.upgrade_broker(manifest)
    assert success["status"] == "broker_ready_users_frozen"
    assert success["previous_states"] == {"brokercheck0": "active", "brokercheck1": "frozen"}
    assert all(row["status"] == "frozen" for row in fleet.tenants())
    try: ops.upgrade_broker({**manifest, "image": args.failing_image})
    except RuntimeError: pass
    else: raise AssertionError("broken_broker_was_accepted")
    failure = json.loads((root / "upgrade-broker.json").read_text())
    assert failure["status"] == "failed_users_frozen_review_required"
    assert failure["operator_snapshot"]["snapshot"]
    assert not ops.run(ops.command("ps", "--status", "running", "-q")).strip()
    restored = ops.rollback_broker()
    assert restored["status"] == "broker_rolled_back_users_frozen"
    for name in ("brokercheck0", "brokercheck1"):
        assert not ops.run(ops.command("ps", "--status", "running", "-q", name)).strip()
    report = {"normal_broker_upgrade": "passed_users_frozen", "broken_broker_upgrade": "stopped_all",
              "rollback": "broker_ready_users_frozen", "operator_snapshot": failure["operator_snapshot"]["snapshot"],
              "real_supplier_and_mail_acceptance": False}
    (root / "broker-upgrade-check.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__": main()
