import datetime as dt
import hashlib
import json
import subprocess
import sys
import time

from . import context, mail, model
from .core import ServiceError, atomic_write, delivery_state, endpoint, in_window, inside, load_config, lock, occurrence, read_json, report_path, secret, write_json

TERMINAL = {"sent", "already_sent", "needs_review", "failed", "expired"}


def sync_worker(c, source):
    if source == "weather":
        import openweather_sync as weather
        credential = secret(c, c["weather"]["secret"])
        if credential.startswith("{"):
            credential = json.loads(credential)["key"]  # Existing encrypted JSON format.
        result = weather.sync(c["root"], {"key": credential}, location=c["weather"], timezone=c["tz"])
        return result["status"]
    return "disabled"


def sync_source(c, source):
    try:
        p = subprocess.run([sys.executable, "-m", "life_service", "--config", str(c["config_path"]), "sync-worker", "--source", source], capture_output=True, timeout=180, encoding="utf-8", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        if p.returncode == 0:
            result = json.loads(p.stdout)
            if result.get("status") in ("synced", "cached"):
                return result["status"]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return "unavailable"


class Runner:
    def __init__(self, c, clock=None):
        self.c = c
        self.clock = clock or (lambda: dt.datetime.now(c["tz"]))

    def persist(self, path, state):
        state["updated_at"] = self.clock().isoformat()
        write_json(path, state)
        # One small current record per occurrence; never log prompt, response or exception text.
        write_json(self.c["root"] / ".local/life-service/last-result.json", state)
        return state.copy()

    def run(self, kind):
        c = self.c
        day, target = occurrence(c, kind, self.clock())
        if not c["enabled"]:
            return {"status": "disabled", "kind": kind}
        if not in_window(c, kind, day, self.clock()):
            return {"status": "outside_window", "kind": kind}
        name = f"{kind}-{day.isoformat()}"
        with lock(c["root"], "life-service-" + name):
            path = c["root"] / ".local/life-service/runs" / (name + ".json")
            state = read_json(path, {"kind": kind, "run_date": day.isoformat(), "target_date": target.isoformat(), "status": "pending", "attempts": 0, "sync": {}})
            receipt = delivery_state(c, kind, target)
            if receipt:
                state["status"] = receipt
                return self.persist(path, state)
            if state["status"] in TERMINAL:
                return state
            if state.get("next_attempt_at") and self.clock() < dt.datetime.fromisoformat(state["next_attempt_at"]):
                return state
            try:
                mail.check_mail(c, kind)
                endpoint(c)
                from life_app import managed
                if not managed.enabled():
                    secret(c, c["model"]["secret"])
                    secret(c, c["mail"]["secret"])
                if not c["approved"]["model_context"]:
                    raise ServiceError("model_context_not_approved")
                if kind == "morning":
                    for source in ("weather",):
                        if not in_window(c, kind, day, self.clock()):
                            state["status"] = "expired"
                            return self.persist(path, state)
                        if source not in state["sync"]:
                            state["sync"][source] = "attempted" if c[source]["enabled"] else "disabled"
                            self.persist(path, state)  # Persist BEFORE network/refresh-token rotation.
                            if c[source]["enabled"]:
                                state["sync"][source] = sync_source(c, source)
                                self.persist(path, state)
                if not in_window(c, kind, day, self.clock()):
                    state["status"] = "expired"
                    return self.persist(path, state)
                ctx = context.collect(c, kind, target, self.clock(), state["sync"])
                if state.get("error"):
                    ctx["validation_feedback"] = state["error"]
                archive = report_path(c, kind, target)
                if state.get("body_sha256") and archive.exists():
                    body = archive.read_text(encoding="utf-8")
                    if hashlib.sha256(body.encode()).hexdigest() != state["body_sha256"]:
                        raise ServiceError("archived_body_changed")
                    model.validate(body, kind, ctx)
                else:
                    if archive.exists():
                        raise ServiceError("existing_untracked_report_review")
                    if state["attempts"] >= 3:
                        state["status"] = "failed"
                        return self.persist(path, state)
                    state["attempts"] += 1
                    state["status"] = "generating"
                    delay = 60 if state["attempts"] == 1 else 300
                    state["next_attempt_at"] = (self.clock() + dt.timedelta(seconds=delay)).isoformat()
                    self.persist(path, state)  # Crash still consumes an attempt.
                    body = model.generate(c, kind, ctx)
                # Same lock name as legacy sender; protect archive and SMTP together.
                with lock(c["root"], "mail"), lock(c["root"], "notes"):
                    receipt = delivery_state(c, kind, target)
                    if receipt:
                        state["status"] = receipt
                    elif not in_window(c, kind, day, self.clock()):
                        state["status"] = "expired"
                    else:
                        if not archive.exists():
                            atomic_write(archive, body)
                        elif archive.read_text(encoding="utf-8") != body:
                            raise ServiceError("archive_concurrently_changed")
                        state["body_sha256"] = hashlib.sha256(body.encode()).hexdigest()
                        state["status"] = "archived"
                        self.persist(path, state)
                        state["status"] = mail.send(c, kind, day, target, self.clock)
                state.pop("next_attempt_at", None)
                state.pop("error", None)
                return self.persist(path, state)
            except ServiceError as exc:
                state["error"] = exc.code
                if exc.retryable and state["attempts"] < 3 and in_window(c, kind, day, self.clock()):
                    state["status"] = "retry_wait"
                    delay = 60 if state["attempts"] == 1 else 300
                    state["next_attempt_at"] = (self.clock() + dt.timedelta(seconds=delay)).isoformat()
                else:
                    state["status"] = "failed"
                return self.persist(path, state)
            except Exception:
                state.update(status="failed", error="local_operation_failed")
                return self.persist(path, state)

    def serve(self):
        with lock(self.c["root"], "life-service-daemon"):
            while True:
                refreshed = load_config(self.c["config_path"])
                if refreshed["root"] != self.c["root"]:
                    raise ServiceError("workspace_changed_restart_service")
                self.c = refreshed
                if not self.c["enabled"]:
                    return
                for kind in ("morning", "evening"):
                    self.run(kind)
                write_json(self.c["root"] / ".local/life-service/heartbeat.json", {"at": self.clock().isoformat()})
                time.sleep(30)


def status(c):
    now = dt.datetime.now(c["tz"])
    next_runs = {}
    for kind in ("morning", "evening"):
        from .core import minute
        start = minute(c["schedule"][kind][0])
        stamp = now.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)
        day, target = occurrence(c, kind, now)
        path = c["root"] / ".local/life-service/runs" / f"{kind}-{day}.json"
        record = read_json(path, {})
        if in_window(c, kind, day, now) and not delivery_state(c, kind, target) and record.get("status") not in TERMINAL:
            stamp = max(now, dt.datetime.fromisoformat(record.get("next_attempt_at", now.isoformat())))
            if not in_window(c, kind, day, stamp):
                stamp = now.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0) + dt.timedelta(days=1)
        elif stamp <= now:
            stamp += dt.timedelta(days=1)
        next_runs[kind] = stamp.isoformat() if c["enabled"] else None
    return {"enabled": c["enabled"], "next_runs": next_runs, "last_result": read_json(c["root"] / ".local/life-service/last-result.json", {}), "heartbeat": read_json(c["root"] / ".local/life-service/heartbeat.json", {})}
