"""Host-side operations. These commands must never be mounted in a user container."""
import json
import os
from pathlib import Path
import subprocess
import time
import shutil
import sqlite3
import tempfile
from life_assistant import lock, atomic_write
from .store import tenant_id


class Operations:
    def __init__(self, fleet, run=None):
        self.fleet = fleet
        self.run = run or self._run
        self.compose = fleet.root / "compose.json"

    @staticmethod
    def _run(argv):
        result = subprocess.run(argv, text=True, capture_output=True, timeout=1800)
        if result.returncode: raise RuntimeError("operator_command_failed:" + argv[0])
        return result.stdout

    def command(self, *args):
        return ["docker", "compose", "-f", str(self.compose), "--profile", "pilot", *args]

    def project(self):
        import re
        name = json.loads(self.compose.read_text(encoding="utf-8")).get("name", "life-pilot")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", name): raise ValueError("invalid_compose_project")
        return name

    def volume_name(self, name):
        tenant_id(name)
        return self.project() + "_" + name

    def known(self, name):
        tenant_id(name)
        if name not in {row["id"] for row in self.fleet.tenants()}: raise ValueError("unknown_tenant")
        if not self.compose.exists(): raise ValueError("render_compose_first")

    def start(self, name):
        self.known(name)
        with lock(self.fleet.root, "fleet-operations"):
            if (self.fleet.root / "PAUSED").exists(): raise ValueError("host_paused_review_migration_before_start")
            spec = json.loads(self.compose.read_text(encoding="utf-8"))
            if spec.get("services", {}).get(name, {}).get("environment", {}).get("LIFE_MAINTENANCE") == "1":
                raise ValueError("maintenance_requires_explicit_resume")
            if name in spec.get("services", {}):
                spec["services"][name]["profiles"] = ["pilot"]
                atomic_write(self.compose, json.dumps(spec, indent=2))
            self.run(self.command("up", "-d", name))
            self.fleet.state(name, "active")

    def freeze(self, name):
        self.known(name)
        with lock(self.fleet.root, "fleet-operations"):
            self.fleet.state(name, "frozen")
            spec = json.loads(self.compose.read_text(encoding="utf-8"))
            if name in spec.get("services", {}):
                spec["services"][name]["profiles"] = ["frozen"]
                atomic_write(self.compose, json.dumps(spec, indent=2))
            self.run(self.command("stop", "--timeout", "120", name))

    def backup(self, name, resume=True):
        self.known(name)
        config = json.loads((self.fleet.root / "backup.json").read_text(encoding="utf-8"))
        password = Path(config["password_file"])
        if not password.is_file(): raise ValueError("backup_password_missing")
        if os.name != "nt" and password.stat().st_mode & 0o077: raise ValueError("backup_password_permissions")
        with lock(self.fleet.root, "fleet-operations"):
            running = bool(self.run(self.command("ps", "--status", "running", "-q", name)).strip())
            prior = next(row["status"] for row in self.fleet.tenants() if row["id"] == name)
            self.fleet.state(name, "frozen")
            self.run(self.command("stop", "--timeout", "120", name))
            try:
                deadline = time.monotonic() + 60
                while True:
                    with self.fleet.db() as db:
                        pending = db.execute("SELECT count(*) FROM usage WHERE tenant=? AND status='in_flight'", (name,)).fetchone()[0]
                        has_mail = db.execute("SELECT 1 FROM sqlite_master WHERE name='mail_receipts'").fetchone()
                        if has_mail: pending += db.execute("SELECT count(*) FROM mail_receipts WHERE tenant=? AND status='sending'", (name,)).fetchone()[0]
                    if not pending: break
                    if time.monotonic() > deadline: raise ValueError("in_flight_results_require_reconciliation")
                    time.sleep(1)
                volume = json.loads(self.run(["docker", "volume", "inspect", self.volume_name(name)]))[0]
                directory = volume["Mountpoint"]
                if not Path(directory).is_absolute() or not Path(directory).is_dir(): raise ValueError("invalid_volume_mount")
                if Path(directory).is_dir():
                    snapshot = {"tenant": name, "schema": 1, "usage": self.fleet.usage(name)}
                    with self.fleet.db() as db:
                        for table in ("mail_receipts", "recipients"):
                            if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                                snapshot[table] = [dict(row) for row in db.execute(f"SELECT * FROM {table} WHERE tenant=?", (name,))]
                    atomic_write(Path(directory) / ".local/relay-snapshot.json", json.dumps(snapshot, ensure_ascii=False))
                args = ["restic", "--repo", config["repository"], "--password-file", str(password)]
                raw = self.run([*args, "backup", directory, "--tag", "tenant:" + name, "--json"])
                summaries = [json.loads(line) for line in raw.splitlines() if line.strip()]
                summary = next((item for item in reversed(summaries) if item.get("message_type") == "summary" and item.get("snapshot_id")), None)
                if not summary: raise ValueError("backup_snapshot_not_confirmed")
                # Retention is allowed only after a successful, identified snapshot.
                self.run([*args, "forget", "--tag", "tenant:" + name, "--group-by", "tags", "--keep-daily", "7", "--keep-weekly", "4", "--prune"])
                return {"snapshot": summary["snapshot_id"], "tenant": name, "encrypted": True}
            finally:
                if resume:
                    self.fleet.state(name, prior)
                    if running: self.run(self.command("start", name))

    def invite(self, name):
        self.known(name)
        if not os.isatty(1): raise ValueError("interactive_terminal_required")
        # A TTY is required by life bootstrap; secret output is never captured in logs.
        result = subprocess.run(self.command("exec", name, "life", "--data", "/data", "bootstrap"))
        if result.returncode: raise RuntimeError("invite_failed")

    def restore(self, name, snapshot, target):
        self.known(name)
        import re
        if not re.fullmatch(r"[a-f0-9]{8,64}", snapshot): raise ValueError("explicit_snapshot_required")
        target = Path(target).resolve()
        if target.exists() and any(target.iterdir()): raise ValueError("restore_requires_empty_target")
        if target.is_relative_to(self.fleet.root): raise ValueError("restore_requires_separate_target")
        config = json.loads((self.fleet.root / "backup.json").read_text(encoding="utf-8"))
        args = ["restic", "--repo", config["repository"], "--password-file", config["password_file"]]
        with lock(self.fleet.root, "fleet-operations"):
            if self.run(self.command("ps", "--status", "running", "-q", name)).strip():
                raise ValueError("stop_source_before_restore")
            snapshots = json.loads(self.run([*args, "snapshots", snapshot, "--json"]))
            if len(snapshots) != 1 or "tenant:" + name not in snapshots[0].get("tags", []): raise ValueError("snapshot_owner_mismatch")
            paths = snapshots[0].get("paths", [])
            if len(paths) != 1 or not Path(paths[0]).is_absolute(): raise ValueError("snapshot_path_invalid")
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.run([*args, "restore", snapshots[0]["id"] + ":" + paths[0], "--target", str(target), "--verify"])
            return {"restored_to": str(target), "content_verified": True, "started": False,
                    "remaining": ["rotate_instance_credentials", "verify_schema_and_mail_receipts", "switch_entrypoint", "start_one_sender_only"]}

    def health(self, name):
        # Readiness is deliberately narrower than real WeChat or inbox acceptance.
        code = "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18932/health/ready',timeout=3).read()"
        for attempt in range(15):
            try:
                self.run(self.command("exec", "-T", name, "python", "-c", code))
                return
            except RuntimeError:
                if attempt == 14: raise
                time.sleep(2)

    def upgrade(self, name, manifest):
        self.known(name)
        if manifest.get("data_format") != 1 or manifest.get("compatible_from") != [1] or not manifest.get("version"):
            raise ValueError("schema_migration_not_supported_requires_review")
        self.fleet.compose(manifest["image"], "validation.example")  # Immutable image validation.
        with lock(self.fleet.root, "fleet-operations"):
            if (self.fleet.root / "PAUSED").exists(): raise ValueError("host_paused_review_migration_before_start")
            prior_status = next(row["status"] for row in self.fleet.tenants() if row["id"] == name)
            if prior_status not in {"active", "frozen"}: raise ValueError("upgrade_requires_provisioned_tenant")
            previous = json.loads(self.compose.read_text(encoding="utf-8"))
            snapshot = self.backup(name, resume=False)
            receipt = {"previous_image": previous["services"][name]["image"], "snapshot": snapshot,
                       "target": manifest, "status": "staging", "data_format": 1, "previous_status": prior_status}
            receipt_path = self.fleet.root / ("upgrade-" + name + ".json")
            atomic_write(receipt_path, json.dumps(receipt))
            staged = json.loads(json.dumps(previous))
            staged["services"][name]["image"] = manifest["image"]
            staged["services"][name].setdefault("environment", {})["LIFE_MAINTENANCE"] = "1"
            atomic_write(self.compose, json.dumps(staged, indent=2))
            try:
                self.run(self.command("up", "-d", "--no-deps", "--force-recreate", name))
                self.health(name)
                if prior_status == "frozen":
                    staged["services"][name]["profiles"] = ["frozen"]
                    atomic_write(self.compose, json.dumps(staged, indent=2))
                    self.fleet.state(name, "frozen")
                    self.run(self.command("stop", "--timeout", "120", name))
                    receipt.update(status="upgraded_frozen", sending_enabled=False)
                else:
                    staged["services"][name]["environment"].pop("LIFE_MAINTENANCE")
                    staged["services"][name]["profiles"] = ["pilot"]
                    atomic_write(self.compose, json.dumps(staged, indent=2))
                    self.fleet.state(name, "active")
                    self.run(self.command("up", "-d", "--no-deps", "--force-recreate", name))
                    self.health(name)
                    receipt["status"] = "runtime_ready_real_acceptance_pending"
            except Exception:
                self.fleet.state(name, "frozen")
                staged["services"][name]["profiles"] = ["frozen"]
                atomic_write(self.compose, json.dumps(staged, indent=2))
                receipt["status"] = "failed_frozen_review_before_rollback"
                atomic_write(receipt_path, json.dumps(receipt))
                self.run(self.command("stop", "--timeout", "120", name))
                raise
            atomic_write(receipt_path, json.dumps(receipt))
            return receipt

    def rollback(self, name):
        self.known(name)
        with lock(self.fleet.root, "fleet-operations"):
            receipt = json.loads((self.fleet.root / ("upgrade-" + name + ".json")).read_text(encoding="utf-8"))
            if receipt.get("data_format") != 1 or receipt["target"].get("data_format") != 1:
                raise ValueError("rollback_requires_snapshot_restore")
            self.fleet.state(name, "frozen")
            self.run(self.command("stop", "--timeout", "120", name))
            spec = json.loads(self.compose.read_text(encoding="utf-8"))
            spec["services"][name]["image"] = receipt["previous_image"]
            spec["services"][name].setdefault("environment", {})["LIFE_MAINTENANCE"] = "1"
            atomic_write(self.compose, json.dumps(spec, indent=2))
            self.run(self.command("up", "-d", "--no-deps", "--force-recreate", name))
            self.health(name)
            return {"status": "rolled_back_in_maintenance", "data_preserved": True, "sending_enabled": False}

    def broker_health(self):
        # The established authenticated route is also present in early pilot images.
        # A 401 proves only local API readiness, never supplier or inbox acceptance.
        code = "import urllib.request,urllib.error\ntry:\n urllib.request.urlopen('http://127.0.0.1:18933/v1/usage',timeout=3)\n raise RuntimeError('broker_auth_missing')\nexcept urllib.error.HTTPError as e:\n assert e.code==401\n"
        for attempt in range(15):
            try:
                self.run(self.command("exec", "-T", "broker", "python", "-c", code))
                return
            except RuntimeError:
                if attempt == 14: raise
                time.sleep(2)

    def upgrade_broker(self, manifest):
        if manifest.get("data_format") != 1 or manifest.get("compatible_from") != [1] or manifest.get("broker_api_version") != 1 or not manifest.get("version"):
            raise ValueError("broker_release_compatibility_required")
        self.fleet.compose(manifest["image"], "validation.example")
        with lock(self.fleet.root, "fleet-operations"):
            if (self.fleet.root / "PAUSED").exists(): raise ValueError("host_paused_review_migration_before_start")
            previous = json.loads(self.compose.read_text(encoding="utf-8"))
            if "broker" not in previous["services"]: raise ValueError("broker_not_configured")
            rows = self.fleet.tenants()
            if any(row["status"] not in {"active", "frozen"} for row in rows): raise ValueError("finish_provisioning_before_broker_upgrade")
            receipt = {"status": "quiescing", "previous_image": previous["services"]["broker"]["image"],
                       "previous_states": {row["id"]: row["status"] for row in rows}, "target": manifest,
                       "data_format": 1, "tenant_snapshots": [], "sending_enabled": False}
            receipt_path = self.fleet.root / "upgrade-broker.json"
            atomic_write(receipt_path, json.dumps(receipt))
            try:
                for row in rows: self.freeze(row["id"])
                for row in rows:
                    receipt["tenant_snapshots"].append(self.backup(row["id"], resume=False))
                    atomic_write(receipt_path, json.dumps(receipt))
                receipt["operator_snapshot"] = self.backup_operator()
                receipt["status"] = "staging"
                atomic_write(receipt_path, json.dumps(receipt))
                staged = json.loads(self.compose.read_text(encoding="utf-8"))
                staged["services"]["broker"]["image"] = manifest["image"]
                atomic_write(self.compose, json.dumps(staged, indent=2))
                self.run(self.command("up", "-d", "--no-deps", "--force-recreate", "broker"))
                self.broker_health()
                receipt["status"] = "broker_ready_users_frozen"
            except Exception:
                receipt["status"] = "failed_users_frozen_review_required"
                for row in rows: self.fleet.state(row["id"], "frozen")
                stopped = json.loads(self.compose.read_text(encoding="utf-8"))
                for row in rows: stopped["services"][row["id"]]["profiles"] = ["frozen"]
                atomic_write(self.compose, json.dumps(stopped, indent=2))
                try: self.run(self.command("stop", "--timeout", "120", *(row["id"] for row in rows), "broker"))
                except Exception: receipt["stop_requires_manual_check"] = True
                atomic_write(receipt_path, json.dumps(receipt))
                raise
            atomic_write(receipt_path, json.dumps(receipt))
            return receipt

    def rollback_broker(self):
        with lock(self.fleet.root, "fleet-operations"):
            if (self.fleet.root / "PAUSED").exists(): raise ValueError("host_paused_review_migration_before_start")
            receipt = json.loads((self.fleet.root / "upgrade-broker.json").read_text(encoding="utf-8"))
            if receipt.get("data_format") != 1 or not receipt.get("operator_snapshot"):
                raise ValueError("broker_rollback_requires_reviewed_snapshot")
            for row in self.fleet.tenants(): self.freeze(row["id"])
            spec = json.loads(self.compose.read_text(encoding="utf-8"))
            spec["services"]["broker"]["image"] = receipt["previous_image"]
            atomic_write(self.compose, json.dumps(spec, indent=2))
            self.run(self.command("up", "-d", "--no-deps", "--force-recreate", "broker"))
            self.broker_health()
            receipt["status"] = "broker_rolled_back_users_frozen"
            atomic_write(self.fleet.root / "upgrade-broker.json", json.dumps(receipt))
            return {"status": receipt["status"], "sending_enabled": False, "unknown_receipts_remain_unreconciled": True}

    def resume(self, name):
        self.known(name)
        with lock(self.fleet.root, "fleet-operations"):
            if (self.fleet.root / "PAUSED").exists(): raise ValueError("host_paused_review_migration_before_start")
            spec = json.loads(self.compose.read_text(encoding="utf-8"))
            spec["services"][name]["profiles"] = ["pilot"]
            spec["services"][name].setdefault("environment", {}).pop("LIFE_MAINTENANCE", None)
            atomic_write(self.compose, json.dumps(spec, indent=2))
            self.run(self.command("up", "-d", "--no-deps", "--force-recreate", name))
            self.health(name)
            self.fleet.state(name, "active")
            return {"status": "resumed", "unknown_receipts_remain_unreconciled": True}

    def backup_operator(self):
        config = json.loads((self.fleet.root / "backup.json").read_text(encoding="utf-8"))
        args = ["restic", "--repo", config["repository"], "--password-file", config["password_file"]]
        with lock(self.fleet.root, "fleet-operations"), tempfile.TemporaryDirectory(prefix="life-operator-backup-") as temporary:
            staging = Path(temporary)
            if os.name != "nt": staging.chmod(0o700)
            source = sqlite3.connect(self.fleet.path)
            target = sqlite3.connect(staging / "fleet.sqlite3")
            try: source.backup(target)
            finally: target.close(); source.close()
            for filename in ("operator.json", "prices.json", "backup.json", "compose.json", "Caddyfile"):
                path = self.fleet.root / filename
                if path.is_file(): shutil.copyfile(path, staging / filename)
            operator_path = staging / "operator.json"
            if operator_path.exists():
                operator = json.loads(operator_path.read_text(encoding="utf-8"))
                if operator.get("ingress"):
                    token_path = Path(operator["ingress"]["token_file"])
                    if not token_path.is_absolute() or not token_path.is_file():
                        raise ValueError("tunnel_token_missing_backup_incomplete")
                    atomic_write(staging / "tunnel-token", token_path.read_text(encoding="utf-8"))
            for row in self.fleet.tenants():
                relative = Path("instances") / row["id"] / "broker-token"
                destination = staging / relative
                destination.parent.mkdir(parents=True, mode=0o700)
                shutil.copyfile(self.fleet.root / relative, destination)
            raw = self.run([*args, "backup", str(staging), "--tag", "operator", "--json"])
            summaries = [json.loads(line) for line in raw.splitlines() if line.strip()]
            result = next((item for item in summaries if item.get("message_type") == "summary" and item.get("snapshot_id")), None)
            if not result: raise ValueError("operator_backup_not_confirmed")
            self.run([*args, "forget", "--tag", "operator", "--group-by", "tags", "--keep-daily", "7", "--keep-weekly", "4", "--prune"])
            return {"snapshot": result["snapshot_id"], "encrypted": True}

    def daily_backup(self):
        results = []
        for row in self.fleet.tenants():
            if row["status"] == "provisioning": continue
            try: results.append(self.backup(row["id"]))
            except Exception: results.append({"tenant": row["id"], "status": "failed"})
        try: results.append({"operator": self.backup_operator()})
        except Exception: results.append({"operator": "failed"})
        atomic_write(self.fleet.root / "last-backup.json", json.dumps(results))
        if any(item.get("status") == "failed" or item.get("operator") == "failed" for item in results):
            raise RuntimeError("backup_failed_check_last_backup")
        return results

    def prepare_migration(self):
        with lock(self.fleet.root, "fleet-operations"):
            atomic_write(self.fleet.root / "PAUSED", "Migration: keep source senders stopped.\n")
            snapshots = []
            for row in self.fleet.tenants():
                if row["status"] == "provisioning": raise ValueError("finish_or_review_provisioning_before_migration")
                self.freeze(row["id"])
                snapshots.append(self.backup(row["id"], resume=False))
            public = self.backup_operator()
            self.run(self.command("stop", "--timeout", "120"))
            manifest = {"data_format": 1, "source_stopped": True, "operator_snapshot": public["snapshot"], "tenants": snapshots}
            atomic_write(self.fleet.root / "migration.json", json.dumps(manifest, indent=2))
            return manifest

    def restore_migration(self, manifest, target):
        """Restore to an empty host directory; never start senders or overwrite live volumes."""
        if manifest.get("data_format") != 1 or manifest.get("source_stopped") is not True: raise ValueError("invalid_migration_manifest")
        import re
        target = Path(target).resolve()
        if target.exists() and any(target.iterdir()): raise ValueError("migration_requires_empty_target")
        if target.is_relative_to(self.fleet.root): raise ValueError("migration_requires_separate_target")
        config = json.loads((self.fleet.root / "backup.json").read_text(encoding="utf-8"))
        args = ["restic", "--repo", config["repository"], "--password-file", config["password_file"]]

        def restore_snapshot(snapshot, tag, destination):
            if not re.fullmatch(r"[a-f0-9]{8,64}", snapshot): raise ValueError("explicit_snapshot_required")
            rows = json.loads(self.run([*args, "snapshots", snapshot, "--json"]))
            if len(rows) != 1 or tag not in rows[0].get("tags", []) or len(rows[0].get("paths", [])) != 1:
                raise ValueError("migration_snapshot_identity_mismatch")
            self.run([*args, "restore", rows[0]["id"] + ":" + rows[0]["paths"][0], "--target", str(destination), "--verify"])

        with lock(self.fleet.root, "fleet-operations"):
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
            restore_snapshot(manifest["operator_snapshot"], "operator", target)
            if not (target / "fleet.sqlite3").is_file(): raise ValueError("operator_database_missing")
            atomic_write(target / "PAUSED", "Target restored but not accepted. Do not enable two senders.\n")
            operator_path = target / "operator.json"
            if operator_path.exists():
                operator = json.loads(operator_path.read_text(encoding="utf-8"))
                if operator.get("ingress"):
                    token_path = target / "tunnel-token"
                    if token_path.is_symlink() or not token_path.is_file():
                        raise ValueError("restored_tunnel_token_missing_configure_locally")
                    if os.name != "nt": token_path.chmod(0o600)
                    operator["ingress"]["token_file"] = str(token_path)
                    atomic_write(operator_path, json.dumps(operator, indent=2))
            from .store import Fleet
            restored = Fleet(target)
            rows = restored.tenants()
            names = [item["tenant"] for item in manifest["tenants"]]
            if len(set(names)) != len(names) or set(names) != {row["id"] for row in rows}: raise ValueError("migration_tenant_mismatch")
            for row in rows: restored.state(row["id"], "frozen")
            atomic_write(target / "PAUSED", "Target restored but not accepted. Do not enable two senders.\n")
            atomic_write(target / "backup.json", json.dumps(config))
            for item in manifest["tenants"]:
                name = tenant_id(item["tenant"])
                data = target / "restored-data" / name
                data.mkdir(parents=True, mode=0o700)
                restore_snapshot(item["snapshot"], "tenant:" + name, data)
                if not (data / ".local/state.sqlite3").is_file(): raise ValueError("user_database_missing")
                with sqlite3.connect(data / ".local/state.sqlite3") as db:
                    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok": raise ValueError("restored_database_invalid")
                    for table in ("kv", "messages", "tokens"):
                        if not db.execute("SELECT 1 FROM sqlite_master WHERE name=? AND type='table'", (table,)).fetchone(): raise ValueError("restored_schema_invalid")
                restored.rotate(name)
            return {"target": str(target), "restored": names, "credentials_rotated": True, "sending_enabled": False,
                    "next": "Render target paths and domain; install empty volumes; verify real channels before enabling each user."}

    def install_restored_volume(self, name):
        self.known(name)
        if os.name == "nt": raise ValueError("linux_host_required")
        if not (self.fleet.root / "PAUSED").exists(): raise ValueError("migration_pause_required")
        source = self.fleet.root / "restored-data" / name
        if not (source / ".local/state.sqlite3").is_file(): raise ValueError("restored_data_missing")
        with lock(self.fleet.root, "fleet-operations"):
            self.run(["docker", "volume", "create", "--label", "com.docker.compose.project=" + self.project(), "--label", "com.docker.compose.volume=" + name, self.volume_name(name)])
            metadata = json.loads(self.run(["docker", "volume", "inspect", self.volume_name(name)]))[0]
            destination = Path(metadata["Mountpoint"]).resolve()
            if destination.name != "_data" or destination.parent.name != self.volume_name(name): raise ValueError("unexpected_volume_path")
            if any(destination.iterdir()): raise ValueError("target_volume_not_empty")
            shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=True)
            for path in [destination, *destination.rglob("*")]:
                if not path.is_symlink(): os.chown(path, 10001, 10001)
            return {"installed": name, "sending_enabled": False}
