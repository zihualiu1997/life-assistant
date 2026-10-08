"""Cut over an already restored fictional fixture; not a clean-OS migration test."""
import argparse
import json
from pathlib import Path
import subprocess
from life_fleet.store import Fleet
from life_fleet.operations import Operations


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    source, target = Path(args.source).resolve(), Path(args.target).resolve()
    if not (source / "FICTIONAL-TEST-ONLY").is_file() or not (source / "PAUSED").is_file():
        raise ValueError("source_fixture_must_remain_paused")
    if not (target / "PAUSED").is_file() or not (target / "restored-data").is_dir():
        raise ValueError("restore_to_target_first")
    old = Operations(Fleet(source))
    assert not old.run(old.command("ps", "--status", "running", "-q")).strip()
    fleet = Fleet(target)
    spec = fleet.compose(args.image, "fixture.invalid")
    spec["name"] = "life-pilot-recovery"
    fixture = Path(__file__).resolve().parents[1] / "tests/container_fixture_broker.py"
    spec["services"]["broker"]["entrypoint"] = ["python", "/fixture-broker.py"]
    spec["services"]["broker"]["volumes"].append({"type": "bind", "source": str(fixture), "target": "/fixture-broker.py", "read_only": True})
    (target / "compose.json").write_text(json.dumps(spec, indent=2))
    (target / "FICTIONAL-TEST-ONLY").write_text("Restored fictional data; no real users.\n")
    operations = Operations(fleet)
    for row in fleet.tenants():
        name = row["id"]
        assert (source / "instances" / name / "broker-token").read_bytes() != (target / "instances" / name / "broker-token").read_bytes()
        operations.install_restored_volume(name)
    operations.run(operations.command("up", "-d", "broker"))
    # The target pause is cleared only after the source is proven stopped.
    assert not old.run(old.command("ps", "--status", "running", "-q")).strip()
    (target / "PAUSED").unlink()
    for row in fleet.tenants(): operations.resume(row["id"])
    subprocess.run(["python3", str(Path(__file__).with_name("pilot-container-check.py")), "--root", str(target)], check=True)
    assert (source / "PAUSED").is_file()
    assert not old.run(old.command("ps", "--status", "running", "-q")).strip()
    report = {"source_stopped": True, "source_pause_retained": True, "five_targets_ready": True,
              "internal_credentials_rotated": True, "same_host_empty_volume_recovery": True,
              "clean_linux_migration": False, "real_wechat_reauthorization": False}
    (target / "migration-check.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__": main()
