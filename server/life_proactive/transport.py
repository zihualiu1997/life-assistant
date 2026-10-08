"""Use the installed Tencent transport without reimplementing its wire protocol."""
import json
import os
from pathlib import Path
import subprocess

from life_service.core import inside


class WeChatSender:
    def __init__(self, config):
        self.c = config

    def call(self, action, **data):
        adapter = Path(__file__).parent / "wechat.mjs"
        request = {"action": action, "root": str(self.c["root"]),
                   "state": str(inside(self.c["root"], self.c["openclaw_state"])),
                   "package": os.environ["LIFE_WECHAT_PLUGIN_DIR"] if self.c.get("managed") else str(inside(self.c["root"], self.c["weixin_package"])),
                   "account": self.c["account_id"], "owner": self.c["owner_id"], **data}
        result = subprocess.run([self.c["node"], str(adapter)], input=json.dumps(request, ensure_ascii=False),
                                capture_output=True, text=True, encoding="utf-8", timeout=40,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        lines = [s.removeprefix("__LIFE_RESULT__") for s in result.stdout.splitlines() if s.startswith("__LIFE_RESULT__")]
        if not lines:
            return {"status": "unknown", "error": "transport_no_receipt"}
        return json.loads(lines[-1])

    def check(self):
        try:
            return self.call("check").get("status")
        except Exception:
            return "not_ready"

    def send(self, key, body):
        return self.call("send", key=key, text=body)
