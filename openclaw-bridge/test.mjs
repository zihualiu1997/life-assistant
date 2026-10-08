import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
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
  register({pluginConfig:{endpoint:'http://127.0.0.1:18932',token:'fictional',accountId:'fictional-account',ownerId:'fictional-owner'},
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

test('native inbound envelope binds journal and capture to the same run and session', async t => {
  const {hooks,calls}=fixture(t);
  const session='agent:main:openclaw-weixin:fictional-owner';
  await hooks.message_received({from:'fictional-owner',content:'记一下：原生虚构消息',messageId:'platform-id'},
    {channelId:'openclaw-weixin',accountId:'fictional-account',runId:'native-run',sessionKey:session});
  await hooks.before_prompt_build({prompt:'changed prompt, not source'}, {runId:'native-run',sessionKey:session});
  assert.equal(calls[0].data.body,'原生虚构消息');
  await hooks.agent_end({success:true,messages:[{role:'assistant',content:'fictional reply'}]}, {runId:'native-run',sessionKey:session});
  assert.equal(calls.at(-1).data.body,'记一下：原生虚构消息');
  assert.match(calls.at(-1).data.message_id,/platform-id$/);
  assert.equal(calls.at(-1).data.source_kind,'user_text');
});

test('conflicting native envelope quarantines the run instead of recording stale text', async t => {
  const {hooks,calls}=fixture(t);
  const ctx={channelId:'openclaw-weixin',accountId:'fictional-account',runId:'collision',sessionKey:'openclaw-weixin:one'};
  await hooks.message_received({from:'fictional-owner',content:'记一下：first',messageId:'id'},ctx);
  await hooks.message_received({from:'fictional-owner',content:'记一下：changed',messageId:'id'},ctx);
  await hooks.before_prompt_build({currentUserMessage:'记一下：fallback',currentUserMessageId:'id'},ctx);
  await hooks.agent_end({success:true,messages:[{role:'assistant',content:'reply'}]},ctx);
  assert.equal(calls.length,0);
});

test('native media stays unconfirmed and failed journal returns an explicit failure context', async t => {
  const {hooks,calls}=fixture(t);
  const ctx={channelId:'openclaw-weixin',accountId:'fictional-account',runId:'media',sessionKey:'openclaw-weixin:one'};
  await hooks.message_received({from:'fictional-owner',content:'图片资料',messageId:'media-id',media:[{}]},ctx);
  await hooks.before_prompt_build({},ctx);
  await hooks.agent_end({success:true,messages:[{role:'assistant',content:'reply'}]},ctx);
  assert.equal(calls.at(-1).data.source_kind,'unclassified');
  const original=globalThis.fetch;
  globalThis.fetch=async (url,opts) => new URL(url).pathname==='/internal/journal' ? new Response('',{status:500}) : original(url,opts);
  const result=await hooks.before_prompt_build({currentUserMessage:'记一下：fictional failure',currentUserMessageId:'failure'}, {...ctx,runId:'failure'});
  assert.match(result.prependContext,/记录请求未获得成功回执/);
});

test('other owner and cross-session reuse cannot enter the native bridge', async t => {
  const {hooks,calls}=fixture(t);
  const ctx={channelId:'openclaw-weixin',accountId:'fictional-account',runId:'run',sessionKey:'openclaw-weixin:one'};
  await hooks.message_received({from:'someone-else',content:'记一下：wrong owner',messageId:'id'},ctx);
  await hooks.before_prompt_build({prompt:'not authority'},ctx);
  assert.equal(calls.length,0);
  await hooks.message_received({from:'fictional-owner',content:'记一下：only first session',messageId:'id'},ctx);
  const other={...ctx,sessionKey:'openclaw-weixin:two'};
  await hooks.before_prompt_build({prompt:'not authority'},other);
  await hooks.agent_end({success:true,messages:[{role:'assistant',content:'reply'}]},other);
  assert.equal(calls.length,0);
});

test('SDK admission bridges legacy hooks without guessing from prompts or session names', async t => {
  const {hooks,calls}=fixture(t);
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'life-native-test-'));
  const old=process.env.OPENCLAW_STATE_DIR;
  process.env.OPENCLAW_STATE_DIR=dir;
  t.after(()=>{if(old===undefined)delete process.env.OPENCLAW_STATE_DIR;else process.env.OPENCLAW_STATE_DIR=old;fs.rmSync(dir,{recursive:true,force:true});});
  fs.mkdirSync(path.join(dir,'life-native-turns'));
  const runId=randomUUID(), sessionKey='agent:main:main';
  const file=path.join(dir,'life-native-turns',runId+'.json');
  const source={id:'sdk-native-id',body:'记一下：虚构 SDK 原生消息',session:sessionKey,ownerId:'fictional-owner',accountId:'fictional-account',native:true,sourceKind:'user_text',at:Date.now()};
  fs.writeFileSync(file,JSON.stringify(source));
  await hooks.before_prompt_build({prompt:'not a source'}, {runId,sessionKey:'another-session'});
  assert.equal(calls.length,0);
  await hooks.before_prompt_build({prompt:'not a source'}, {runId,sessionKey});
  assert.equal(calls[0].data.body,'虚构 SDK 原生消息');
  await hooks.agent_end({success:true,messages:[{role:'assistant',content:'reply'}]}, {runId,sessionKey});
  assert.equal(calls.at(-1).action,'/internal/capture');
  assert.equal(calls.at(-1).data.source_kind,'user_text');
  assert.equal(fs.existsSync(file),false);
});
