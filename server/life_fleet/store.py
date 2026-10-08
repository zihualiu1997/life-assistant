import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time


def tenant_id(value):
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", value) or value in {"broker", "caddy", "tunnel", "operator", "default"}:
        raise ValueError("invalid_tenant_id")
    return value


class Fleet:
    """Separate registry: never points at the existing personal workspace."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        if (self.root / ".local/state.sqlite3").exists() or (self.root / "AGENTS.md").exists():
            raise ValueError("fleet_requires_separate_operator_directory")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "fleet.sqlite3"
        with self.db() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS tenants (
                id TEXT PRIMARY KEY, status TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE, created REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS usage (
                id TEXT PRIMARY KEY, tenant TEXT NOT NULL REFERENCES tenants(id),
                purpose TEXT NOT NULL, model TEXT NOT NULL, created REAL NOT NULL,
                status TEXT NOT NULL, input_tokens INTEGER, output_tokens INTEGER,
                audio_seconds REAL, price_version TEXT, estimated_cost REAL);
            ''')
        if os.name != "nt":
            self.path.chmod(0o600)

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def create(self, name):
        name = tenant_id(name)
        with self.db() as db:
            existing = db.execute("SELECT status FROM tenants WHERE id=?", (name,)).fetchone()
            if existing:
                return {"id": name, "status": existing[0], "created": False}
            # Frozen accounts still occupy their slot and retain their identity.
            if db.execute("SELECT count(*) FROM tenants").fetchone()[0] >= 5:
                raise ValueError("pilot_capacity_reached")
            token = secrets.token_urlsafe(48)
            db.execute("INSERT INTO tenants VALUES (?,?,?,?)",
                       (name, "provisioning", hashlib.sha256(token.encode()).hexdigest(), time.time()))
            directory = self.root / "instances" / name
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            target = directory / "broker-token"
            # Secrets stay outside user data and are mounted read-only by the operator.
            target.write_text(token, encoding="utf-8")
            # Compose file-backed secrets preserve host mode; parent directories are 0700.
            if os.name != "nt": target.chmod(0o444)
        return {"id": name, "status": "provisioning", "created": True}

    def authenticate(self, token):
        with self.db() as db:
            row = db.execute("SELECT id FROM tenants WHERE token_hash=? AND status='active'",
                             (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        return row[0] if row else None

    def rotate(self, name):
        name = tenant_id(name)
        token = secrets.token_urlsafe(48)
        with self.db() as db:
            row = db.execute("SELECT status FROM tenants WHERE id=?", (name,)).fetchone()
            if not row or row[0] != "frozen": raise ValueError("rotation_requires_frozen_tenant")
            target = self.root / "instances" / name / "broker-token"
            temporary = target.with_suffix(".new")
            temporary.write_text(token, encoding="utf-8")
            if os.name != "nt": temporary.chmod(0o444)
            os.replace(temporary, target)
            db.execute("UPDATE tenants SET token_hash=? WHERE id=?", (hashlib.sha256(token.encode()).hexdigest(), name))
        return {"id": name, "rotated": True, "status": "frozen"}

    def state(self, name, status):
        if status not in {"active", "frozen", "provisioning"}: raise ValueError("invalid_status")
        with self.db() as db:
            if db.execute("UPDATE tenants SET status=? WHERE id=?", (status, tenant_id(name))).rowcount != 1:
                raise ValueError("unknown_tenant")

    def tenants(self):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT id,status,created FROM tenants ORDER BY id")]

    def usage(self, name):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM usage WHERE tenant=? ORDER BY created", (name,))]

    def record(self, request_id, name, purpose, model, status, usage=None):
        usage = usage if isinstance(usage, dict) else {}
        def number(key):
            value = usage.get(key)
            return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None
        incoming, outgoing, seconds = number("prompt_tokens"), number("completion_tokens"), number("seconds")
        if status == "measured" and (incoming is None or outgoing is None): status = "pending_reconciliation"
        cost, version = None, None
        price_file = self.root / "prices.json"
        if price_file.exists():
            try:
                raw = price_file.read_bytes()
                prices = json.loads(raw)
                price = prices["models"][model]
                version = prices["version"] + ":" + hashlib.sha256(raw).hexdigest()[:16]
                if prices["currency"] != "CNY": raise ValueError()
                if price["unit"] == "tokens" and incoming is not None and outgoing is not None:
                    cost = (incoming * price["input_per_million"] + outgoing * price["output_per_million"]) / 1_000_000
                elif price["unit"] == "audio_seconds" and seconds is not None:
                    cost = seconds * price["per_second"]
                if cost is not None and (not isinstance(cost, (float, int)) or cost < 0): raise ValueError()
            except (ValueError, KeyError, TypeError): cost, version = None, None
        with self.db() as db:
            if status == "in_flight":
                active = db.execute("SELECT 1 FROM tenants WHERE id=? AND status='active'", (name,)).fetchone()
                if not active: raise ValueError("tenant_not_active")
            db.execute("INSERT INTO usage VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,input_tokens=excluded.input_tokens,output_tokens=excluded.output_tokens,audio_seconds=excluded.audio_seconds,price_version=excluded.price_version,estimated_cost=excluded.estimated_cost",
                       (request_id, name, purpose, model, time.time(), status,
                        incoming, outgoing, seconds, version, cost))

    def compose(self, image, domain):
        # Immutable digest only: never silently follow a mutable image tag.
        if not re.fullmatch(r"[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}", image):
            raise ValueError("image_digest_required")
        if not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}", domain):
            raise ValueError("invalid_domain")
        services, networks, volumes, secret_mounts = {}, {}, {}, {}
        operator_path = self.root / "operator.json"
        operator = json.loads(operator_path.read_text(encoding="utf-8")) if operator_path.exists() else None
        for row in self.tenants():
            name = row["id"]
            networks[name] = {"driver": "bridge"}
            volumes[name] = {}
            services[name] = {
                "image": image, "init": True, "restart": "unless-stopped",
                "mem_limit": "2g", "cpus": 2, "read_only": True,
                "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
                "pids_limit": 256, "tmpfs": ["/tmp:size=256m,mode=1777"],
                "volumes": [f"{name}:/data"], "networks": [name],
                "environment": {"LIFE_DATA_DIR": "/data", "LIFE_PUBLIC_ORIGIN": f"https://{name}.{domain}"},
                "profiles": ["frozen"] if row["status"] == "frozen" else ["pilot"],
            }
            if operator:
                secret_name = name + "-broker"
                secret_mounts[secret_name] = {"file": str(self.root / "instances" / name / "broker-token")}
                services[name]["secrets"] = [{"source": secret_name, "target": "broker"}]
                services[name]["depends_on"] = ["broker"]
                services[name]["environment"].update(LIFE_MANAGED="1", LIFE_BROKER_TOKEN_FILE="/run/secrets/broker", LIFE_OPERATOR_MODEL=operator["model"], LIFE_OPERATOR_ASR_MODEL=operator["asr_model"])
        if operator:
            services["broker"] = {"image": image, "user": "0:0", "entrypoint": ["python", "-m", "life_fleet.serve"],
                                  "restart": "unless-stopped", "read_only": True, "cap_drop": ["ALL"],
                                  "security_opt": ["no-new-privileges:true"], "mem_limit": "1g", "cpus": 2,
                                  "tmpfs": ["/tmp:size=64m,mode=1777"], "networks": list(networks),
                                  "volumes": [{"type": "bind", "source": str(self.root), "target": "/operator"}], "profiles": ["pilot"]}
            ingress = operator.get("ingress")
            if ingress:
                for key in ("caddy_image", "tunnel_image"):
                    if not re.fullmatch(r"[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}", ingress[key]): raise ValueError("ingress_digest_required")
                networks["entry"] = {"driver": "bridge"}
                services["caddy"] = {"image": ingress["caddy_image"], "restart": "unless-stopped", "networks": list(networks),
                                     # The official binary has this file capability; dropping it
                                     # from the bounding set prevents exec even on port 8080.
                                     "mem_limit": "256m", "read_only": True, "cap_drop": ["ALL"], "cap_add": ["NET_BIND_SERVICE"],
                                     "security_opt": ["no-new-privileges:true"], "tmpfs": ["/config", "/data", "/tmp"],
                                     "volumes": [{"type": "bind", "source": str(self.root / "Caddyfile"), "target": "/etc/caddy/Caddyfile", "read_only": True}], "profiles": ["pilot"]}
                secret_mounts["tunnel"] = {"file": ingress["token_file"]}
                services["tunnel"] = {"image": ingress["tunnel_image"], "restart": "unless-stopped", "networks": ["entry"],
                                      # Operator files are root-owned 0600. File-backed Compose
                                      # secrets retain that ownership; cloudflared defaults to 65532.
                                      "user": "0:0", "mem_limit": "256m", "read_only": True, "cap_drop": ["ALL"],
                                      "security_opt": ["no-new-privileges:true"], "secrets": ["tunnel"],
                                      "command": ["tunnel", "--no-autoupdate", "run", "--token-file", "/run/secrets/tunnel"], "profiles": ["pilot"]}
        return {"name": "life-pilot", "services": services, "networks": networks, "volumes": volumes, "secrets": secret_mounts}

    def caddyfile(self, domain):
        if not re.fullmatch(r"(?:[a-z0-9-]+\.)+[a-z]{2,63}", domain): raise ValueError("invalid_domain")
        lines = ["{", "  admin off", "  auto_https off", "}", ":8080 {", "  @private path /internal /internal/* /v1/pair /api/pairing /api/devices*"]
        tenants = self.tenants()
        for row in tenants:
            lines.append(f"  @{row['id']} host {row['id']}.{domain}")
        # Caddy normally sorts handle before respond. route preserves this order
        # so a private path is denied before any tenant's reverse proxy executes.
        lines.extend(["  route {", "    respond @private 404"])
        for row in tenants:
            name = row["id"]
            lines.extend([f"    handle @{name} {{", f"      reverse_proxy {name}:18932", "    }"])
        return "\n".join([*lines, "    handle {", "      respond 404", "    }", "  }", "}", ""])
