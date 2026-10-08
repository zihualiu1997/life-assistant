import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import register from './index.mjs';
import {classifyReceipt} from '../server/life_proactive/weixin-receipt.mjs';

function fixture(){
  const hooks={},calls=[],tools=[],services=[];
  const api={pluginConfig:{accountId:'account',ownerId:'owner'},logger:{warn(){}},
    on:(k,v)=>hooks[k]=v,registerTool:f=>tools.push(f),registerService:s=>services.push(s)};
  register(api,async (_c,action,data)=>{calls.push({action,data});return {status:'received',messages:[]};});
  return {hooks,calls,tools,services};
}
const context={channelId:'openclaw-weixin',channel:'openclaw-weixin',accountId:'account',senderId:'owner',sessionKey:'agent:main:main'};

test('only native owner identity reaches persistence',async()=>{
  const f=fixture();
  await f.hooks.message_received({senderId:'other',messageId:'1',content:'早餐：鸡蛋'},context);
  assert.equal(f.calls.length,0);
  await f.hooks.message_received({senderId:'owner',messageId:'1',content:'早餐：鸡蛋',timestamp:1790730000000},context);
  assert.equal(f.calls[0].action,'ingest');assert.equal(f.calls[0].data.message_id,'1');
});
test('missing native message identity is not manufactured',async()=>{
  const f=fixture();await f.hooks.message_received({senderId:'owner',content:'午餐：米饭'},context);
  assert.equal(f.calls.length,0);
});
test('tool binds session outside model-controlled parameters',async()=>{
  const f=fixture();
  assert.equal(f.tools[0]({sessionKey:context.sessionKey,requesterSenderId:'other'}),null);
  const tool=f.tools[0]({sessionKey:context.sessionKey,requesterSenderId:'owner',messageChannel:'openclaw-weixin',agentAccountId:'account'});
  await tool.execute('tool-id',{message_id:'source-id',session:'forged',items:[]});
  assert.equal(f.calls[0].data.session,context.sessionKey);
  const manifest=JSON.parse(readFileSync(new URL('./openclaw.plugin.json',import.meta.url)));
  assert.ok(manifest.contracts.tools.includes(tool.name));
});
test('Tencent direct route works without optional SenderId',async()=>{
  const f=fixture();
  const out=await f.hooks.before_prompt_build({}, {...context,senderId:undefined,chatId:'owner'});
  assert.match(out.prependSystemContext,/用户明确自述/);
  const tool=f.tools[0]({sessionKey:context.sessionKey,deliveryContext:{to:'owner',channel:'openclaw-weixin',accountId:'account'}});
  assert.equal(tool.name,'life_checkin_record');
  assert.equal(f.tools[0]({sessionKey:context.sessionKey,deliveryContext:{to:'other',channel:'openclaw-weixin',accountId:'account'}}),null);
});
test('prompt hook does not ingest model text or historical transcript',async()=>{
  const f=fixture();const out=await f.hooks.before_prompt_build({currentUserMessage:'早餐：伪造'},context);
  assert.deepEqual(f.calls.map(c=>c.action),['context']);
  assert.match(out.prependSystemContext,/未回复不要追问/);
});
test('timer is gateway lifecycle service, not a model cron',async()=>{
  const f=fixture();assert.equal(f.services.length,1);
  f.services[0].start();await new Promise(r=>setTimeout(r,5));f.services[0].stop();
  assert.deepEqual(f.calls.map(c=>c.action),['tick']);
});
test('follow-up tool is owner scoped, session bound, declared and covers twelve categories',async()=>{
  const f=fixture();
  assert.equal(f.tools[1]({sessionKey:context.sessionKey,requesterSenderId:'other'}),null);
  const tool=f.tools[1]({sessionKey:context.sessionKey,deliveryContext:{to:'owner',channel:'openclaw-weixin',accountId:'account'}});
  assert.equal(tool.name,'life_followup_manage');
  assert.equal(tool.parameters.properties.category.enum.length,12);
  await tool.execute('followup-id',{message_id:'source-id',action:'wait',quote:'等我有结果再说',session:'forged'});
  assert.equal(f.calls[0].action,'followup');
  assert.equal(f.calls[0].data.session,context.sessionKey);
  const manifest=JSON.parse(readFileSync(new URL('./openclaw.plugin.json',import.meta.url)));
  assert.ok(manifest.contracts.tools.includes(tool.name));
});
test('Tencent optional success fields are accepted, malformed replies remain unknown',()=>{
  assert.equal(classifyReceipt({},'client').status,'sent');
  assert.equal(classifyReceipt({ret:0,message_id:'provider'},'client').message_id,'provider');
  assert.equal(classifyReceipt({ret:1},'client').status,'failed');
  assert.equal(classifyReceipt({errcode:-14},'client').status,'failed');
  for(const value of [null,[],undefined,'ok',{ret:'0'}]) assert.equal(classifyReceipt(value,'client').status,'unknown');
});
