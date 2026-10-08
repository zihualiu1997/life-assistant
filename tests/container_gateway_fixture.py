"""Fixture-only startup: real gateway, fictional admission, no platform polling."""
import json
import os
from pathlib import Path
import runpy
from life_app.store import Store
from life_app.config import settings
from life_app.memory import Preferences
from life_app.integrations import verify_wechat_owner, write_gateway

name = os.environ["LIFE_FIXTURE_NAME"]
if name not in {f"fixture{i}" for i in range(5)}: raise ValueError("fictional_identity_required")
store = Store("/data")
store.initialize()
store.put("preferences", Preferences(cloud_processing=True).model_dump())
s = settings(store)
s.model_context_approved = True
store.put("settings", s.model_dump())
root = store.state / "openclaw/openclaw-weixin"
(root / "accounts").mkdir(parents=True, exist_ok=True)
(root / "accounts.json").write_text(json.dumps([name]))
(root / f"accounts/{name}.json").write_text(json.dumps({"userId": name + "-owner", "token": "fictional-invalid-token"}))
verify_wechat_owner(store)
write_gateway(store)
plugin = store.state / "fixture-native-admission"
plugin.mkdir(exist_ok=True)
(plugin / "openclaw.plugin.json").write_text(json.dumps({"id": "fixture-native-admission", "name": "Fictional admission", "activation": {"onStartup": True}, "configSchema": {"type": "object", "additionalProperties": False, "properties": {}}}))
(plugin / "index.mjs").write_text('''
import fs from 'node:fs';
import {recordNativeTurn} from '/opt/life/gateway/node_modules/@tencent-weixin/openclaw-weixin/dist/src/messaging/life-batching-adapter.mjs';
export default function register(api) {
  const admitted = new Map();
  api.on('before_prompt_build', (_event, ctx) => {
    if (ctx.sessionKey !== 'agent:main:fixture-admission' || admitted.has(ctx.runId)) return;
    const source=JSON.parse(fs.readFileSync('/data/.local/fixture-inbound.json','utf8'));
    const name=process.env.LIFE_FIXTURE_NAME;
    if (!source.body.startsWith('记一下：FICTIONAL_OWNER_'+name+'_') || Date.now()-source.at>60000) throw Error('invalid_fixture_source');
    recordNativeTurn({from_user_id:name+'-owner'}, {SessionKey:ctx.sessionKey,MessageSid:source.id,Body:source.body},
      {accountId:name,config:api.config},ctx.runId);
    for(const [id,at] of admitted)if(Date.now()-at>600000)admitted.delete(id);
    admitted.set(ctx.runId,Date.now());
  },{priority:1000});
}
''')
path = store.secrets / "openclaw.json"
config = json.loads(path.read_text())
config["channels"]["openclaw-weixin"]["enabled"] = False
config["plugins"]["allow"].append("fixture-native-admission")
config["plugins"]["load"]["paths"].append(str(plugin))
config["plugins"]["entries"]["fixture-native-admission"] = {"enabled": True, "hooks": {"allowConversationAccess": True}}
path.write_text(json.dumps(config))
store.put("gateway_enabled", True)
runpy.run_module("life_fleet.runtime", run_name="__main__")
