import contextlib
import datetime as dt
import hashlib
import os
import tempfile
import time
import threading
from pathlib import Path
from zoneinfo import ZoneInfo
ROOT = Path(os.environ.get("LIFE_DATA_DIR", "data")).resolve()
TZ = ZoneInfo("Asia/Shanghai")
_held_locks = threading.local()
def dpapi(*args, **kwargs):
    raise RuntimeError("Windows credentials are not portable; configure server secrets")

def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextlib.contextmanager
def lock(root, name):
    """OS lock is released on exit/crash; lock files may remain harmlessly."""
    key = (str(root.resolve()), name)
    held = getattr(_held_locks, 'keys', None)
    if held is None:
        held = _held_locks.keys = set()
    if key in held:
        yield
        return
    path = root / ".local" / "locks" / (name + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + 15
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise RuntimeError("文件正在被另一入口更新，请稍后重试")
                time.sleep(0.1)
        try:
            held.add(key)
            yield
        finally:
            held.discard(key)
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def valid_date(value):
    result = dt.date.fromisoformat(value)
    if result.isoformat() != value:
        raise ValueError("日期必须为 YYYY-MM-DD")
    return result


def journal(root, date, source, message_id, body, timezone=None):
    valid_date(date)
    if source not in {"codex", "wechat", "manual", "desktop"} or not message_id.strip() or not body.strip():
        raise ValueError("来源、消息编号和原文不能为空或无效")
    key = hashlib.sha256((source + "\0" + message_id).encode()).hexdigest()
    marker = f"<!-- entry:{key} -->"
    fingerprint = hashlib.sha256((date + "\0" + body).encode()).hexdigest()
    request_marker = f"<!-- desktop-request:{fingerprint} -->"
    path = root / "01_日常记录" / date[:4] / (date + ".md")
    with lock(root, "notes"):
        if not path.resolve().is_relative_to(root.resolve() / "01_日常记录"):
            raise ValueError("journal_path_outside_workspace")
        # Scan canonical records, so a retry with a changed date cannot duplicate an entry.
        for existing in (root / "01_日常记录").glob("????/????-??-??.md"):
            if not existing.resolve().is_relative_to(root.resolve() / "01_日常记录"):
                raise ValueError("journal_path_outside_workspace")
            lines = existing.read_text(encoding="utf-8").splitlines()
            if marker in lines:
                if source in {"desktop", "wechat", "manual"} and (lines.index(marker) + 1 >= len(lines) or lines[lines.index(marker) + 1] != request_marker):
                    raise ValueError("message_id_conflict")
                return {"status": "already_recorded", "path": str(existing)}
        old = path.read_text(encoding="utf-8") if path.exists() else f"# {date}\n"
        stamp = dt.datetime.now(timezone or TZ).isoformat(timespec="seconds")
        quoted = "\n".join("> " + line for line in body.splitlines())
        metadata = marker + ("\n" + request_marker if source in {"desktop", "wechat", "manual"} else "")
        entry = f"\n\n{metadata}\n## 随手记录 · {stamp}\n\n- 来源：{source}\n- 发生日期：{date}\n\n### 用户原文\n\n{quoted}\n"
        atomic_write(path, old.rstrip() + entry)
    return {"status": "recorded", "path": str(path)}


