import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import register from './index.mjs';
function fixture(){const hooks={},tools=[],calls=[];register({pluginConfig:{accountId:'a',ownerId:'o'},on:(n,h)=>hooks[n]=h,registerTool:f=>tools.push(f),registerService(){}},async(c,action,data)=>{calls.push({action,data});return {status:'created'};});return {hooks,tools,calls};}
const ctx={sessionKey:'s',deliveryContext:{to:'o',accountId:'a',channel:'openclaw-weixin'}};
test('knowledge is permitted and declared while arbitrary writes remain blocked',()=>{
 const f=fixture();assert.notEqual(f.hooks.before_tool_call({toolName:'life_knowledge_manage'})?.block,true);
 for(const toolName of ['write','edit','exec','apply_patch']) assert.equal(f.hooks.before_tool_call({toolName}).block,true);
 assert.ok(JSON.parse(readFileSync(new URL('./openclaw.plugin.json',import.meta.url))).contracts.tools.includes('life_knowledge_manage'));
});
test('knowledge tool binds owner and session',async()=>{
 const f=fixture(),tool=f.tools.map(x=>x(ctx)).find(x=>x?.name==='life_knowledge_manage');assert.ok(tool);
 await tool.execute('t',{action:'create',path:'04_知识与资源/笔记.md',content:'资料',session:'forged'});
 assert.equal(f.calls[0].action,'knowledge');assert.equal(f.calls[0].data.session,'s');
 assert.equal(f.tools.map(x=>x({...ctx,deliveryContext:{...ctx.deliveryContext,to:'other'}})).filter(Boolean).length,0);
});
