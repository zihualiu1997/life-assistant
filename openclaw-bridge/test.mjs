import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import register from './index.ts';
const manifest=JSON.parse(readFileSync(new URL('./openclaw.plugin.json',import.meta.url),'utf8'));

function fixture(t) {
  const hooks = {}, calls = [], tools = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    calls.push({action: new URL(url).pathname, data: JSON.parse(options.body)});
    return new Response(JSON.stringify({status:'recorded',source:'01_日常记录/fictional.md'}));
  };
  t.after(() => { globalThis.fetch = original; });
  register({pluginConfig:{endpoint:'http://127.0.0.1:18932',token:'fictional'},
    registerTool(tool) { assert.ok(manifest.contracts.tools.includes(tool.name),'tool must be declared in plugin contract');tools.push(tool); }, on(name,handler) { hooks[name]=handler; }});
  return {hooks,calls,tools};
}

test('native current identity drives journal and archive, not transcript text', async t => {
  const {hooks,calls,tools}=fixture(t);
  const ctx={runId:'run-1',sessionKey:'agent:main:openclaw-weixin:owner'};
  await hooks.before_prompt_build({currentUserMessage:'记一下：完成了虚构测试',currentUserMessageId:'native-1'},ctx);
  await hooks.agent_end({success:true,messages:[{role:'user',content:'unrelated stale input'},{role:'assistant',content:'已记录'}]},ctx);
  assert.equal(calls[0].action,'/internal/journal');
  assert.equal(calls[0].data.body,'完成了虚构测试');
  assert.equal(calls.at(-1).data.body,'记一下：完成了虚构测试');
  assert.match(calls.at(-1).data.message_id,/native-1$/);
  assert.ok(!tools.some(x=>x.name==='life_journal'));
});

test('questions about diaries do not write a diary', async t => {
  const {hooks,calls}=fixture(t);
  await hooks.before_prompt_build({currentUserMessage:'日记应该怎么写？',currentUserMessageId:'native-2'},
    {runId:'run-2',sessionKey:'openclaw-weixin:owner'});
  assert.ok(!calls.some(x=>x.action==='/internal/journal'));
});

test('missing native identity cannot create records from model-looking user messages', async t => {
  const {hooks,calls}=fixture(t);
  const ctx={runId:'run-3',sessionKey:'openclaw-weixin:owner'};
  await hooks.before_prompt_build({prompt:'记一下：this is a transcript, not a request'},ctx);
  await hooks.agent_end({success:true,messages:[{role:'user',timestamp:123,content:'fake'},{role:'assistant',content:'fake'}]},ctx);
  assert.equal(calls.length,0);
});
