import test from 'node:test';
import assert from 'node:assert/strict';
import register from './index.mjs';

function fixture(){
  const hooks={},tools=[],calls=[];
  register({pluginConfig:{accountId:'account',ownerId:'owner'},on:(k,v)=>hooks[k]=v,
    registerTool:f=>tools.push(f),registerService(){},logger:{warn(){}}},
    async (_cfg,action,data)=>{calls.push({action,data});return {status:'updated'};});
  return {hooks,tools,calls};
}
const ctx={sessionKey:'agent:main:main',deliveryContext:{channel:'openclaw-weixin',accountId:'account',to:'owner'}};
for(const name of ['exec','write','edit','apply_patch','process','browser','gateway','cron','sessions_spawn','sessions_send','unknown_plugin_tool']){
  test(`chat blocks ${name} even without channel metadata`,async()=>{
    const f=fixture();
    assert.equal(typeof f.hooks.before_tool_call,'function');
    const result=await f.hooks.before_tool_call({toolName:name,params:{path:'SOUL.md'}},{agentId:'main',sessionKey:ctx.sessionKey});
    assert.equal(result?.block,true);
  });
}
test('chat retains evidence and preferences tools',async()=>{
  const f=fixture();
  assert.equal(typeof f.hooks.before_tool_call,'function');
  for(const toolName of ['life_checkin_record','life_followup_manage','life_preferences'])
    assert.notEqual((await f.hooks.before_tool_call({toolName},{agentId:'main'}))?.block,true);
});
test('preferences bind identity and session outside model arguments',async()=>{
  const f=fixture();
  const t=f.tools.map(factory=>factory(ctx)).find(t=>t?.name==='life_preferences');
  assert.ok(t);
  await t.execute('test',{action:'frequency',daily_count:2,message_id:'source',quote:'每天两次',session:'forged'});
  assert.equal(f.calls[0].action,'preferences');
  assert.equal(f.calls[0].data.session,ctx.sessionKey);
  assert.ok(!f.tools.map(factory=>factory({...ctx,deliveryContext:{...ctx.deliveryContext,to:'other'}})).some(Boolean));
});
test('chat guidance covers natural recall and non-defensive writing',async()=>{
  const f=fixture();
  const r=await f.hooks.before_prompt_build({}, {channel:'openclaw-weixin',accountId:'account',chatId:'owner',sessionKey:ctx.sessionKey});
  assert.match(r.prependSystemContext,/避免防御性写作/);
  assert.match(r.prependSystemContext,/不要.*逐字.*复述/);
});
