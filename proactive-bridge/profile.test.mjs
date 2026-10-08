import test from 'node:test';
import assert from 'node:assert/strict';
import register from './index.mjs';

test('profile tool is owner-bound, pathless and session cannot be forged', async()=>{
  const hooks={},tools=[],calls=[];
  register({pluginConfig:{accountId:'a',ownerId:'o'},on:(k,v)=>hooks[k]=v,
    registerTool:f=>tools.push(f),registerService(){}},async(_c,action,data)=>{calls.push({action,data});return {status:'saved'};});
  const ctx={sessionKey:'real',deliveryContext:{to:'o',channel:'openclaw-weixin',accountId:'a'}};
  const tool=tools.map(f=>f(ctx)).find(t=>t?.name==='life_profile_manage');
  assert.ok(tool); assert.equal(tool.parameters.properties.path,undefined);
  await tool.execute('id',{action:'status',session:'fake'});
  assert.equal(calls[0].action,'profile'); assert.equal(calls[0].data.session,'real');
  assert.notEqual(hooks.before_tool_call({toolName:'life_profile_manage'})?.block,true);
  assert.equal(hooks.before_tool_call({toolName:'exec'}).block,true);
  assert.ok(tools.every(f=>f({...ctx,deliveryContext:{...ctx.deliveryContext,to:'stranger'}})===null));
});
