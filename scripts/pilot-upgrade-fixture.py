"""Exercise the real stopped-backup/stage/fail/rollback path on fictional users."""
import argparse
import json
from pathlib import Path
import subprocess
from life_fleet.store import Fleet
from life_fleet.operations import Operations


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--failing-image", required=True)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if not (root / "FICTIONAL-TEST-ONLY").is_file(): raise ValueError("fixture_marker_required")
    fleet = Fleet(root)
    operations = Operations(fleet)
    def started(name):
        cid = operations.run(operations.command("ps", "-q", name)).strip()
        return json.loads(subprocess.check_output(["docker", "inspect", cid], text=True))[0]["State"]["StartedAt"]
    before = started("fixture1")
    manifest = {"version": "fictional-intentional-failure", "data_format": 1, "compatible_from": [1], "image": args.failing_image}
    try:
        # This is the CLI's sequential release rule: no later user executes after a failure.
        for name in ("fixture0", "fixture1"):
            operations.upgrade(name, manifest)
    except RuntimeError:
        pass
    else:
        raise AssertionError("intentionally_broken_image_was_accepted")
    assert before == started("fixture1"), "later_user_was_restarted"
    assert next(row for row in fleet.tenants() if row["id"] == "fixture0")["status"] == "frozen"
    receipt = json.loads((root / "upgrade-fixture0.json").read_text())
    assert receipt["snapshot"]["snapshot"]
    operations.rollback("fixture0")
    operations.resume("fixture0")
    operations.health("fixture0")
    report = {"intentionally_failed_upgrade": "frozen", "later_user_unchanged": True,
              "pre_upgrade_snapshot": receipt["snapshot"]["snapshot"], "rollback_and_resume": "ready",
              "real_channel_acceptance": False}
    (root / "upgrade-check.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__": main()
