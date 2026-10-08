"""Executed over Docker stdin by the fixture runner; contains fictional data only."""
import datetime
import io
import json
from pathlib import Path
import socket
import subprocess
import os
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from life_app.store import Store

name = PAYLOAD["name"]
origin = "https://" + name + ".fixture.invalid"


def request(path, body=None, cookie="", csrf="", method=None):
    req = urllib.request.Request("http://127.0.0.1:18932" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Origin": origin, "Cookie": cookie, "X-CSRF-Token": csrf, "Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return response.status, response.read(), response.headers
    except urllib.error.HTTPError as response:
        return response.code, response.read(), response.headers


if PAYLOAD["phase"] == "initialize":
    store = Store("/data")
    body = {"password": "fictional-password-only"}
    path = "/api/login"
    if not store.get("admin"):
        body["token"] = store.bootstrap()
        path = "/api/bootstrap"
    status, raw, headers = request(path, body)
    assert status == 200, (status, raw)
    cookie = headers["Set-Cookie"].split(";", 1)[0]
    csrf = json.loads(raw)["csrf"]
    status, raw, _ = request("/v1/journal", {"message_id": str(uuid.uuid4()), "date": datetime.date.today().isoformat(), "body": "FICTIONAL_OWNER_" + name}, cookie, csrf)
    assert status == 200, (status, raw)
    token = Path("/run/secrets/broker").read_text()
    model = urllib.request.Request("http://broker:18933/v1/chat/completions", data=json.dumps({"model": "fixture-model", "messages": [{"role": "user", "content": "fictional"}]}).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with urllib.request.urlopen(model, timeout=15) as response:
        assert json.loads(response.read())["choices"][0]["message"]["content"] == "FICTIONAL_REPLY"
    print(json.dumps({"cookie": cookie, "csrf": csrf, "model": "passed"}))
elif PAYLOAD["phase"] == "load":
    entry = {"message_id": str(uuid.uuid4()), "date": datetime.date.today().isoformat(), "body": "FICTIONAL_LOAD_" + name}
    status, raw, _ = request("/v1/journal", entry, PAYLOAD["cookie"], PAYLOAD["csrf"])
    assert status == 200 and json.loads(raw)["status"] == "recorded", (status, raw)
    status, raw, _ = request("/v1/journal", entry, PAYLOAD["cookie"], PAYLOAD["csrf"])
    assert status == 200 and json.loads(raw)["status"] == "already_recorded", (status, raw)
    token = Path("/run/secrets/broker").read_text()
    model = urllib.request.Request("http://broker:18933/v1/chat/completions", data=json.dumps({"model": "fixture-model", "messages": [{"role": "user", "content": "fictional load"}]}).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with urllib.request.urlopen(model, timeout=15) as response:
        assert json.loads(response.read())["choices"][0]["message"]["content"] == "FICTIONAL_REPLY"
    print(json.dumps({"model": "passed", "journal_replay": "deduplicated"}))
elif PAYLOAD["phase"] == "gateway-load":
    from life_assistant import atomic_write
    from life_app.integrations import gateway_env
    from life_app.store import digest
    store = Store("/data")
    native_id = str(uuid.uuid4())
    with urllib.request.urlopen("http://127.0.0.1:18789/health", timeout=5) as response:
        assert response.status == 200, "gateway_unavailable"
    body = "记一下：FICTIONAL_OWNER_" + name + "_" + native_id
    atomic_write(store.state / "fixture-inbound.json", json.dumps({"id": native_id, "body": body, "at": time.time()*1000}))
    started = time.monotonic()
    result = subprocess.run([os.environ["LIFE_OPENCLAW_BIN"], "agent", "--session-key", "agent:main:fixture-admission",
                             "--message", "Fictional source admission check", "--json", "--timeout", "60"],
                            env=gateway_env(store), capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, "fixture_gateway_agent_failed"
    assert "falling back" not in result.stderr.lower(), "local_fallback_is_not_gateway_validation"
    key = digest("wechat:agent:main:fixture-admission:" + native_id)
    with store.db() as db:
        row = db.execute("SELECT body,reply,status FROM messages WHERE id=?", (key,)).fetchone()
    assert row and row["body"] == body and row["status"] == "completed" and row["reply"] == "FICTIONAL_REPLY", "source_or_reply_mismatch"
    journals = "\n".join(p.read_text() for p in (store.root / "01_日常记录").rglob("*.md"))
    assert journals.count(body.removeprefix("记一下：")) == 1, "missing_or_duplicate_journal"
    for other in {f"fixture{i}" for i in range(5)} - {name}:
        assert "FICTIONAL_OWNER_" + other + "_" not in journals, "cross_owner_journal"
    native_dir = store.state / "openclaw/life-native-turns"
    assert not list(native_dir.glob("*.json")), "unconsumed_native_source"
    print(json.dumps({"gateway": "passed", "source_and_journal": "passed", "elapsed_seconds": time.monotonic()-started}))
elif PAYLOAD["phase"] == "isolation":
    status, _, _ = request("/api/export", cookie=PAYLOAD["other_cookie"])
    assert status == 401, status
    status, raw, _ = request("/api/export", cookie=PAYLOAD["cookie"])
    assert status == 200, status
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        text = "\n".join(archive.read(item).decode() for item in archive.namelist())
    assert "FICTIONAL_OWNER_" + name in text
    assert "FICTIONAL_OWNER_" + PAYLOAD["other"] not in text
    assert not Path("/var/run/docker.sock").exists()
    assert not Path("/operator").exists()
    try:
        with socket.create_connection((PAYLOAD["other_ip"], 18932), timeout=2):
            raise AssertionError("cross_container_network_access")
    except (socket.timeout, ConnectionRefusedError, OSError):
        pass
    print(json.dumps({"isolation": "passed", "data_preserved": True}))
