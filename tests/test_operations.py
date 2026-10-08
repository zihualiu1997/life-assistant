import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from life_fleet.store import Fleet
from life_fleet.operations import Operations


class OperationsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "volume").mkdir()
        self.fleet = Fleet(self.root / "operator")
        self.fleet.create("alice")
        (self.fleet.root / "compose.json").write_text("{}")
        password = self.root / "backup-password"
        password.write_text("fictional-test-password")
        password.chmod(0o600)
        (self.fleet.root / "backup.json").write_text(json.dumps({"repository": str(self.root / "backups"), "password_file": str(password)}))

    def test_failed_backup_keeps_old_snapshots_and_restarts(self):
        calls=[]
        def run(command):
            calls.append(command)
            if "ps" in command: return "fixture-container"
            if command[:3] == ["docker", "volume", "inspect"]: return json.dumps([{"Mountpoint": str(self.root / "volume")}])
            if "backup" in command: raise RuntimeError("disk_full")
            return ""
        with self.assertRaises(RuntimeError): Operations(self.fleet, run).backup("alice")
        self.assertFalse(any("forget" in call for call in calls))
        self.assertEqual(calls[-1][-2:], ["start", "alice"])

    def test_operator_backup_includes_external_tunnel_secret_and_missing_blocks_pruning(self):
        secret = self.root / "external-token"
        secret.write_text("fictional-tunnel-token")
        config = {"ingress": {"token_file": str(secret.resolve())}}
        (self.fleet.root / "operator.json").write_text(json.dumps(config))
        calls = []
        def run(command):
            calls.append(command)
            if "backup" in command:
                staging = Path(command[command.index("backup") + 1])
                self.assertEqual((staging / "tunnel-token").read_text(), "fictional-tunnel-token")
                return json.dumps({"message_type": "summary", "snapshot_id": "a"*64})
            return ""
        operation = Operations(self.fleet, run)
        self.assertTrue(operation.backup_operator()["encrypted"])
        secret.unlink()
        calls.clear()
        with self.assertRaisesRegex(ValueError, "tunnel_token_missing"):
            operation.backup_operator()
        self.assertEqual(calls, [])

    def test_migration_pause_blocks_manual_start_and_resume(self):
        (self.fleet.root / "PAUSED").write_text("migration")
        operation = Operations(self.fleet, lambda args: self.fail("No sender may start"))
        for method in (operation.start, operation.resume):
            with self.assertRaisesRegex(ValueError, "host_paused"):
                method("alice")

    def test_freeze_survives_compose_restart_and_start_restores_profile(self):
        spec = self.fleet.compose("registry.example/life@sha256:" + "a" * 64, "pilot.example")
        (self.fleet.root / "compose.json").write_text(json.dumps(spec))
        operation = Operations(self.fleet, lambda args: "")
        operation.freeze("alice")
        self.assertEqual(json.loads(operation.compose.read_text())["services"]["alice"]["profiles"], ["frozen"])
        operation.start("alice")
        self.assertEqual(json.loads(operation.compose.read_text())["services"]["alice"]["profiles"], ["pilot"])

    def test_restore_refuses_running_source_and_wrong_owner(self):
        def run(command):
            if "ps" in command: return "running"
            return ""
        with self.assertRaisesRegex(ValueError, "stop_source"):
            Operations(self.fleet, run).restore("alice", "a"*64, self.root / "restore")
        def wrong_owner(command):
            if "snapshots" in command: return json.dumps([{"id": "a"*64, "tags": ["tenant:bob"], "paths": [str(self.root / "volume")]}])
            return ""
        with self.assertRaisesRegex(ValueError, "owner"):
            Operations(self.fleet, wrong_owner).restore("alice", "a"*64, self.root / "restore")

    def test_upgrade_failure_stops_user_and_preserves_snapshot_receipt(self):
        self.fleet.state("alice", "active")
        image = "registry.example/life@sha256:" + "a"*64
        spec = self.fleet.compose(image, "pilot.example")
        (self.fleet.root / "compose.json").write_text(json.dumps(spec))
        calls = []
        operation = Operations(self.fleet, lambda args: calls.append(args) or "")
        manifest = {"image": "registry.example/life@sha256:" + "b"*64, "version": "fixture", "data_format": 1, "compatible_from": [1]}
        with patch.object(operation, "backup", return_value={"snapshot": "c"*64}), patch.object(operation, "health", side_effect=RuntimeError("failed")):
            with self.assertRaises(RuntimeError): operation.upgrade("alice", manifest)
        self.assertEqual(self.fleet.tenants()[0]["status"], "frozen")
        receipt = json.loads((self.fleet.root / "upgrade-alice.json").read_text())
        self.assertEqual(receipt["snapshot"]["snapshot"], "c"*64)
        self.assertEqual(receipt["previous_image"], image)
        self.assertEqual(calls[-1][-4:], ["stop", "--timeout", "120", "alice"])

    def test_frozen_upgrade_never_enables_sending(self):
        self.fleet.state("alice", "frozen")
        image = "registry.example/life@sha256:" + "a"*64
        (self.fleet.root / "compose.json").write_text(json.dumps(self.fleet.compose(image, "pilot.example")))
        calls = []
        operation = Operations(self.fleet, lambda args: calls.append(args) or "")
        manifest = {"image": "registry.example/life@sha256:" + "b"*64, "version": "fixture", "data_format": 1, "compatible_from": [1]}
        with patch.object(operation, "backup", return_value={"snapshot": "c"*64}), patch.object(operation, "health"):
            receipt = operation.upgrade("alice", manifest)
        self.assertEqual(receipt["status"], "upgraded_frozen")
        self.assertFalse(receipt["sending_enabled"])
        self.assertEqual(self.fleet.tenants()[0]["status"], "frozen")
        service = json.loads(operation.compose.read_text())["services"]["alice"]
        self.assertEqual(service["environment"]["LIFE_MAINTENANCE"], "1")
        self.assertEqual(service["profiles"], ["frozen"])
        self.assertEqual(len([call for call in calls if "up" in call]), 1)
        self.assertEqual(calls[-1][-4:], ["stop", "--timeout", "120", "alice"])

    def test_paused_source_cannot_be_started_resumed_or_upgraded(self):
        image = "registry.example/life@sha256:" + "a"*64
        (self.fleet.root / "compose.json").write_text(json.dumps(self.fleet.compose(image, "pilot.example")))
        before = (self.fleet.root / "compose.json").read_bytes()
        (self.fleet.root / "PAUSED").write_text("migration source stopped")
        operation = Operations(self.fleet, lambda args: self.fail("Paused source must not run external commands"))
        manifest = {"image": image, "version": "fixture", "data_format": 1, "compatible_from": [1]}
        for call in (lambda: operation.start("alice"), lambda: operation.resume("alice"), lambda: operation.upgrade("alice", manifest)):
            with self.assertRaisesRegex(ValueError, "host_paused"): call()
        self.assertEqual((self.fleet.root / "compose.json").read_bytes(), before)

    def test_unknown_schema_and_nonempty_migration_target_are_rejected(self):
        operation = Operations(self.fleet, lambda args: self.fail("Must reject before external commands"))
        with self.assertRaisesRegex(ValueError, "schema"):
            operation.upgrade("alice", {"data_format": 2})
        target = self.root / "existing"
        target.mkdir()
        (target / "keep.txt").write_text("keep")
        with self.assertRaisesRegex(ValueError, "empty"):
            operation.restore_migration({"data_format": 1, "source_stopped": True}, target)
        self.assertEqual((target / "keep.txt").read_text(), "keep")

    def test_shared_broker_upgrade_and_rollback_leave_every_user_frozen(self):
        image = "registry.example/life@sha256:" + "a"*64
        (self.fleet.root / "operator.json").write_text(json.dumps({"model": "fixture", "asr_model": "fixture-asr"}))
        self.fleet.state("alice", "active")
        self.fleet.create("bob")
        self.fleet.state("bob", "frozen")
        (self.fleet.root / "compose.json").write_text(json.dumps(self.fleet.compose(image, "pilot.example")))
        calls = []
        operation = Operations(self.fleet, lambda args: calls.append(args) or "")
        manifest = {"image": "registry.example/life@sha256:" + "b"*64, "version": "fixture", "data_format": 1, "compatible_from": [1], "broker_api_version": 1}
        def backup(name, resume):
            self.assertFalse(resume)
            self.assertTrue(all(row["status"] == "frozen" for row in self.fleet.tenants()))
            return {"tenant": name, "snapshot": "c"*64}
        with patch.object(operation, "backup", side_effect=backup), patch.object(operation, "backup_operator", return_value={"snapshot": "d"*64}), patch.object(operation, "broker_health"):
            receipt = operation.upgrade_broker(manifest)
            self.assertEqual(receipt["status"], "broker_ready_users_frozen")
            self.assertEqual(receipt["previous_states"], {"alice": "active", "bob": "frozen"})
            result = operation.rollback_broker()
            self.assertFalse(result["sending_enabled"])
        self.assertTrue(all(row["status"] == "frozen" for row in self.fleet.tenants()))
        self.assertTrue(all(call[-1] == "broker" for call in calls if "up" in call))
        spec = json.loads(operation.compose.read_text())
        self.assertEqual(spec["services"]["broker"]["image"], image)
        self.assertEqual(spec["services"]["alice"]["profiles"], ["frozen"])

    def test_shared_broker_failure_stops_all_and_keeps_snapshot_receipt(self):
        image = "registry.example/life@sha256:" + "a"*64
        (self.fleet.root / "operator.json").write_text(json.dumps({"model": "fixture", "asr_model": "fixture-asr"}))
        self.fleet.state("alice", "active")
        (self.fleet.root / "compose.json").write_text(json.dumps(self.fleet.compose(image, "pilot.example")))
        calls = []
        operation = Operations(self.fleet, lambda args: calls.append(args) or "")
        manifest = {"image": image, "version": "fixture", "data_format": 1, "compatible_from": [1], "broker_api_version": 1}
        with patch.object(operation, "backup", return_value={"snapshot": "c"*64}), patch.object(operation, "backup_operator", return_value={"snapshot": "d"*64}), patch.object(operation, "broker_health", side_effect=RuntimeError("failed")):
            with self.assertRaises(RuntimeError): operation.upgrade_broker(manifest)
        receipt = json.loads((self.fleet.root / "upgrade-broker.json").read_text())
        self.assertEqual(receipt["status"], "failed_users_frozen_review_required")
        self.assertEqual(receipt["operator_snapshot"]["snapshot"], "d"*64)
        self.assertEqual(calls[-1][-5:], ["stop", "--timeout", "120", "alice", "broker"])
        (self.fleet.root / "PAUSED").write_text("migration")
        before = len(calls)
        with self.assertRaisesRegex(ValueError, "host_paused"): operation.upgrade_broker(manifest)
        with self.assertRaisesRegex(ValueError, "host_paused"): operation.rollback_broker()
        self.assertEqual(len(calls), before)
