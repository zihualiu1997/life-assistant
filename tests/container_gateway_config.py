"""Check real installed OpenClaw configuration without contacting WeChat."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.request
import urllib.error
import io
import threading
import uvicorn
from life_app.store import Store
from life_app.integrations import write_gateway, gateway_env, verify_wechat_owner
from life_app.app import create_app
from life_fleet.store import Fleet
from life_fleet.broker import create_broker


with tempfile.TemporaryDirectory(prefix="gateway-config-", dir="/tmp") as folder:
    store = Store(folder)
    store.initialize()
    (store.root / "SOUL.md").write_text("## 语气\nFICTIONAL_PERSONA_PROJECTION_CHECK\n")
    os.environ["LIFE_MANAGED"] = "1"
    os.environ["LIFE_OPERATOR_MODEL"] = "fixture-model"
    os.environ["LIFE_OPERATOR_ASR_MODEL"] = "fixture-asr"
    fleet = Fleet(Path(folder) / "operator")
    fleet.create("fixture")
    fleet.state("fixture", "active")
    token_file = fleet.root / "instances/fixture/broker-token"
    os.environ["LIFE_BROKER_TOKEN_FILE"] = str(token_file)
    def upstream(request, timeout):
        payload = json.loads(request.data)
        assert "FICTIONAL_PERSONA_PROJECTION_CHECK" in json.dumps(payload["messages"]), "Active persona projection was lost"
        usage = {"prompt_tokens": 12, "completion_tokens": 4}
        if payload.get("stream"):
            chunks = [
                {"id": "fixture", "object": "chat.completion.chunk", "created": 1, "model": "fixture-model", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "FICTIONAL_GATEWAY_REPLY"}, "finish_reason": None}]},
                {"id": "fixture", "object": "chat.completion.chunk", "created": 1, "model": "fixture-model", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage}]
            return io.BytesIO(("".join("data: " + json.dumps(item) + "\n\n" for item in chunks) + "data: [DONE]\n\n").encode())
        return io.BytesIO(json.dumps({"id": "fixture", "object": "chat.completion", "created": 1, "model": "fixture-model", "choices": [{"index": 0, "message": {"role": "assistant", "content": "FICTIONAL_GATEWAY_REPLY"}, "finish_reason": "stop"}], "usage": usage}).encode())
    servers = []
    for app, port in ((create_app(folder, background=False), 18932),
                      (create_broker(fleet, "https://fixture.invalid/v1", "fictional", {"fixture-model"}, opener=upstream), 18933)):
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False, log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()
        servers.append(server)
    root = store.state / "openclaw/openclaw-weixin"
    (root / "accounts").mkdir(parents=True)
    (root / "accounts.json").write_text(json.dumps(["fictional-account"]))
    (root / "accounts/fictional-account.json").write_text(json.dumps({"userId": "fictional-owner", "token": "fictional-invalid-token"}))
    verify_wechat_owner(store)
    write_gateway(store)
    result = subprocess.run([os.environ["LIFE_OPENCLAW_BIN"], "config", "validate"], env=gateway_env(store), capture_output=True, text=True, timeout=90)
    # Output is from a disposable fixture config, never a real account.
    print(result.stdout)
    print(result.stderr)
    if "blocked plugin" in result.stdout + result.stderr or "plugin present but blocked" in result.stdout + result.stderr:
        raise SystemExit("OpenClaw refused a required plugin")
    if result.returncode: raise SystemExit(result.returncode)
    result = subprocess.run([os.environ["LIFE_OPENCLAW_BIN"], "plugins", "list", "--json", "--enabled"], env=gateway_env(store), capture_output=True, text=True, timeout=90)
    if result.returncode: raise SystemExit(result.returncode)
    plugins = {item["id"]: item for item in json.loads(result.stdout)["plugins"]}
    for name in ("life-bridge", "life-proactive", "openclaw-weixin"):
        assert plugins[name]["status"] == "loaded", name
    assert "memory-core" not in plugins, "Uncontrolled memory pipeline was loaded"
    assert {"life_checkin_record", "life_followup_manage"}.issubset(plugins["life-proactive"]["toolNames"])
    print(json.dumps({name: item["status"] for name, item in plugins.items()}))
    # Network is disabled by the container runner; disable channel polling as well.
    path = store.secrets / "openclaw.json"
    config = json.loads(path.read_text())
    config["channels"]["openclaw-weixin"]["enabled"] = False
    # A fixture-only admission hook supplies an SDK envelope for a CLI run.
    # This exercises SDK admission -> real gateway hooks -> application storage;
    # it does not emulate successful platform delivery or replace phone testing.
    admission = Path(folder) / "fixture-native-admission"
    admission.mkdir()
    (admission / "openclaw.plugin.json").write_text(json.dumps({"id": "fixture-native-admission", "name": "Fictional admission", "activation": {"onStartup": True}, "configSchema": {"type": "object", "additionalProperties": False, "properties": {}}}))
    (admission / "index.mjs").write_text('''
import {recordNativeTurn} from '/opt/life/gateway/node_modules/@tencent-weixin/openclaw-weixin/dist/src/messaging/life-batching-adapter.mjs';
export default function register(api) {
  const admitted = new Set();
  api.on('before_prompt_build', (_event, ctx) => {
    if (ctx.sessionKey !== 'agent:main:fixture-admission' || admitted.has(ctx.runId)) return;
    recordNativeTurn({from_user_id:'fictional-owner'}, {SessionKey:ctx.sessionKey, MessageSid:'fixture-sdk-native-id',
      Body:'记一下：FICTIONAL_SDK_ADMITTED_JOURNAL'}, {accountId:'fictional-account',config:api.config},ctx.runId);
    admitted.add(ctx.runId);
  }, {priority:1000});
}
''')
    config["plugins"]["allow"].append("fixture-native-admission")
    config["plugins"]["load"]["paths"].append(str(admission))
    config["plugins"]["entries"]["fixture-native-admission"] = {"enabled": True, "hooks": {"allowConversationAccess": True}}
    path.write_text(json.dumps(config))
    with tempfile.TemporaryFile(mode="w+t") as log:
        process = subprocess.Popen([os.environ["LIFE_OPENCLAW_BIN"], "gateway", "run"], env=gateway_env(store), stdout=log, stderr=log)
        try:
            for attempt in range(45):
                try:
                    with urllib.request.urlopen("http://127.0.0.1:18789/health", timeout=1) as response:
                        assert response.status == 200
                        if b"[gateway] ready" in os.pread(log.fileno(), 1000000, 0):
                            print("GATEWAY_READY_NETWORK_DISABLED")
                            assert (store.state / "openclaw/agent/SOUL.md").read_text() == (store.root / "SOUL.md").read_text()
                            break
                except (urllib.error.URLError, TimeoutError):
                    if process.poll() is not None: raise RuntimeError("gateway_exited")
                    if attempt == 44: raise RuntimeError("gateway_not_ready")
                if attempt == 44: raise RuntimeError("gateway_startup_not_finished")
                time.sleep(1)
            request = urllib.request.Request("http://127.0.0.1:18789/v1/chat/completions",
                data=json.dumps({"model": "openclaw/default", "user": "fictional-session", "stream": False,
                                 "messages": [{"role": "user", "content": "Reply briefly."}]}).encode(),
                headers={"Authorization": "Bearer " + store.secret("gateway"), "Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=60) as response:
                result = json.loads(response.read())
            assert result["choices"][0]["message"]["content"] == "FICTIONAL_GATEWAY_REPLY", result
            entries = fleet.usage("fixture")
            assert entries and all(row["status"] == "measured" for row in entries), entries
            print("GATEWAY_TO_PRIVATE_BROKER_AND_ACCOUNTING_OK")
            for content in ("记一下：FICTIONAL_NATIVE_JOURNAL", "记一下：只聊不记，FICTIONAL_NOT_SAVED"):
                request = urllib.request.Request("http://127.0.0.1:18789/v1/chat/completions",
                    data=json.dumps({"model": "openclaw/default", "user": "fictional-session", "stream": False,
                                     "messages": [{"role": "user", "content": content}]}).encode(),
                    headers={"Authorization": "Bearer " + store.secret("gateway"), "Content-Type": "application/json"})
                with urllib.request.urlopen(request, timeout=60) as response:
                    assert response.status == 200
            journals = "\n".join(path.read_text() for path in (store.root / "01_日常记录").rglob("*.md"))
            # This legacy HTTP endpoint omits native admission fields in 2026.9.5.
            # Never infer source identity from its prompt or accumulated history.
            assert "FICTIONAL_NATIVE_JOURNAL" not in journals, "Missing native identity was fabricated"
            assert "FICTIONAL_NOT_SAVED" not in journals, "No-save escaped the guard"
            print("LEGACY_HTTP_WITHOUT_NATIVE_ID_DOES_NOT_WRITE")
            result = subprocess.run([os.environ["LIFE_OPENCLAW_BIN"], "agent", "--session-key", "agent:main:fixture-admission",
                                     "--message", "Fictional admission check", "--json", "--timeout", "60"],
                                    env=gateway_env(store), capture_output=True, text=True, timeout=90)
            assert result.returncode == 0, result.stdout + result.stderr
            journals = "\n".join(p.read_text() for p in (store.root / "01_日常记录").rglob("*.md"))
            assert "FICTIONAL_SDK_ADMITTED_JOURNAL" in journals, "SDK admission did not reach journal writer"
            with store.db() as db:
                row = db.execute("SELECT body FROM messages WHERE owner='wechat' AND conversation='agent:main:fixture-admission'").fetchone()
            assert row and row[0] == "记一下：FICTIONAL_SDK_ADMITTED_JOURNAL", "Reply was not bound to its admitted source"
            print("SDK_ADMISSION_TO_REAL_GATEWAY_JOURNAL_AND_CAPTURE_OK")
        finally:
            process.terminate()
            try: process.wait(15)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
            log.seek(0)
            output = log.read()
            print(output[-5000:])
            assert "managed dreaming cron job" not in output
            for server in servers: server.should_exit = True
