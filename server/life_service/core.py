"""Configuration, durable storage, dates and credential boundary."""
import base64
import datetime as dt
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from life_assistant import atomic_write, dpapi, lock


class ServiceError(Exception):
    """Only fixed, non-sensitive diagnostic codes may be exposed."""
    def __init__(self, code, retryable=False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def read_json(path, default=None):
    if not path.exists() and default is not None:
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise ServiceError("invalid_or_missing_json") from None


def write_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def inside(root, value):
    candidate = (root / value).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise ServiceError("path_outside_workspace")
    return candidate


def minute(value):
    if value == "24:00":
        return 1440
    try:
        stamp = dt.datetime.strptime(value, "%H:%M")
        if stamp.strftime("%H:%M") != value:
            raise ValueError()
        return stamp.hour * 60 + stamp.minute
    except (ValueError, TypeError):
        raise ServiceError("invalid_schedule_time") from None


def load_config(path):
    c = read_json(path)
    c["config_path"] = path.resolve()
    try:
        c["root"] = (path.resolve().parent / c["workspace"]).resolve()
        c["tz"] = ZoneInfo(c["timezone"])
        if c["version"] != 1 or not c["root"].is_dir():
            raise ValueError()
        for key in ("enabled",):
            if not isinstance(c[key], bool):
                raise ValueError()
        for kind in ("morning", "evening"):
            start, end = map(minute, c["schedule"][kind])
            if not 0 <= start < end <= 1440 or not isinstance(c["approved"][kind], bool):
                raise ValueError()
        if not isinstance(c["approved"]["model_context"], bool):
            raise ValueError()
        if not 1 <= c["model"]["timeout"] <= 180:
            raise ValueError()
        if "enable_thinking" in c["model"] and not isinstance(c["model"]["enable_thinking"], bool):
            raise ValueError()
        if not 1000 <= c["context"]["max_chars"] <= 100000:
            raise ValueError()
        for key in ("mail", "weather", "whoop"):
            if not isinstance(c[key], dict):
                raise ValueError()
    except (KeyError, ValueError, TypeError):
        raise ServiceError("invalid_config") from None
    return c


def endpoint(c):
    base = c["model"]["base_url"].rstrip("/")
    p = urlsplit(base)
    if p.username or p.password or p.query or p.fragment:
        raise ServiceError("invalid_model_endpoint")
    if p.scheme != "https" and not (p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1", "::1")):
        raise ServiceError("model_requires_https_or_loopback")
    if not p.hostname or not c["model"]["name"].strip():
        raise ServiceError("model_not_configured")
    return base + "/chat/completions"


def secret(c, ref):
    try:
        method, name = ref.split(":", 1)
        if method == "env":
            value = os.environ.get(name, "")
        elif method == "file":
            value = inside(c["root"], name).read_text(encoding="utf-8")
        elif method == "dpapi":
            value = dpapi(inside(c["root"], name).read_bytes(), decrypt=True).decode("utf-8")
        else:
            raise ValueError()
        if not value.strip():
            raise ValueError()
        return value
    except Exception:
        raise ServiceError("credential_unavailable") from None


def save_secret(c, ref, value):
    method, name = ref.split(":", 1)
    if method != "dpapi" or not value.strip():
        raise ServiceError("setup_requires_dpapi_reference")
    # Keep the existing raw DPAPI format; use an atomic replace for binary data.
    import tempfile
    target = inside(c["root"], name)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = dpapi(value.encode("utf-8"))
    fd, temp = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, target)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def occurrence(c, kind, now):
    if kind not in ("morning", "evening"):
        raise ServiceError("invalid_kind")
    day = now.astimezone(c["tz"]).date()
    return day, day + dt.timedelta(days=kind == "evening")


def in_window(c, kind, run_day, now):
    local = now.astimezone(c["tz"])
    start, end = map(minute, c["schedule"][kind])
    return local.date() == run_day and start <= local.hour * 60 + local.minute < end


def receipt_path(c, kind, target):
    return c["root"] / ".local/mail" / (("evening-" if kind == "evening" else "") + target.isoformat() + ".json")


def delivery_state(c, kind, target):
    p = receipt_path(c, kind, target)
    if not p.exists():
        return None
    state = read_json(p).get("status")
    return "already_sent" if state == "sent" else "needs_review"


def report_path(c, kind, target):
    return c["root"] / "05_复盘与计划" / ("晨报" if kind == "morning" else "明日安排") / str(target.year) / (target.isoformat() + ".md")
