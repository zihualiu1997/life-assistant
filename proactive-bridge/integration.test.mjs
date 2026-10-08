import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import register from './index.mjs';

test('real Node hook to Python to durable journal and prompt receipt, with synthetic local data',async t=>{
  const workspace=fileURLToPath(new URL('..',import.meta.url));
  const toolsDir=path.join(workspace,'server');
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'life-checkin-integration-'));
  t.after(()=>{
    const actual=fs.realpathSync(root),base=fs.realpathSync(os.tmpdir());
    const relative=path.relative(base,actual);
    assert.ok(!relative.startsWith('..')&&!path.isAbsolute(relative)&&path.basename(actual).startsWith('life-checkin-integration-'));
    fs.rmSync(actual,{recursive:true,force:true});
  });
  const config=JSON.parse(fs.readFileSync(path.join(toolsDir,'life_proactive/config.example.json'),'utf8'));
  Object.assign(config,{workspace:'.',enabled:false,followups:{enabled:true},account_id:'test-account',owner_id:'test-owner'});
  const configPath=path.join(root,'config.json');fs.writeFileSync(configPath,JSON.stringify(config));
  const hooks={},tools=[];
  register({pluginConfig:{python:process.env.LIFE_TEST_PYTHON || (process.platform==='win32'?path.join(workspace,'.venv/Scripts/python.exe'):'python3'),
    entrypoint:path.join(toolsDir,'life_proactive/entrypoint.py'),config:configPath,accountId:'test-account',ownerId:'test-owner'},
    on:(n,h)=>hooks[n]=h,registerTool:f=>tools.push(f),registerService(){},logger:{warn(message){throw new Error(message);}}});
  const session='agent:main:main';
  await hooks.message_received({from:'test-owner',messageId:'local-only-1',timestamp:Date.now(),content:'午餐：一碗面'},
    {channelId:'openclaw-weixin',accountId:'test-account',sessionKey:session});
  const prompt=await hooks.before_prompt_build({}, {channel:'openclaw-weixin',accountId:'test-account',chatId:'test-owner',sessionKey:session});
  const data=JSON.parse(prompt.prependContext.split('\n').slice(1).join('\n'));
  const receipt=JSON.parse(data.messages[0].receipt);
  assert.equal(receipt.status,'recorded');
  const journal=fs.readFileSync(path.join(root,receipt.paths[0]),'utf8');
  assert.match(journal,/> 午餐：一碗面/);
  await hooks.message_received({from:'test-owner',messageId:'local-only-2',timestamp:Date.now(),content:'我有点累，不过今天挺开心，明天打算看展'},
    {channelId:'openclaw-weixin',accountId:'test-account',sessionKey:session});
  const natural=await hooks.before_prompt_build({}, {channel:'openclaw-weixin',accountId:'test-account',chatId:'test-owner',sessionKey:session});
  const evidence=JSON.parse(natural.prependContext.split('\n').slice(1).join('\n')).messages.find(m=>m.text.startsWith('我有点累'));
  const sourceReceipt=JSON.parse(evidence.receipt);
  const tool=tools[0]({sessionKey:session,deliveryContext:{to:'test-owner',channel:'openclaw-weixin',accountId:'test-account'}});
  const itemSchema=tool.parameters.properties.items.items.properties;
  assert.ok(itemSchema.kind.enum.includes('mood')&&itemSchema.kind.enum.includes('state')&&itemSchema.kind.enum.includes('plan'));
  const result=await tool.execute('local-record', {message_id:evidence.id,items:[
    {day:sourceReceipt.date,kind:'state',name:'energy',state:'reported',quote:'我有点累'},
    {day:sourceReceipt.date,kind:'mood',name:'feeling',state:'reported',quote:'今天挺开心'},
    {day:sourceReceipt.date,kind:'plan',name:'tomorrow',state:'planned',quote:'明天打算看展'}]});
  assert.equal(JSON.parse(result.content[0].text).status,'recorded');
  const after=await hooks.before_prompt_build({}, {channel:'openclaw-weixin',accountId:'test-account',chatId:'test-owner',sessionKey:session});
  const facts=JSON.parse(after.prependContext.split('\n').slice(1).join('\n')).facts_today;
  assert.equal(facts.length,4);
  assert.equal(facts.find(f=>f.kind==='plan').state,'planned');
  assert.equal((fs.readFileSync(path.join(root,receipt.paths[0]),'utf8').match(/<!-- entry:/g)||[]).length,2);
  await hooks.message_received({from:'test-owner',messageId:'local-only-3',timestamp:Date.now(),content:'今天别问了'},
    {channelId:'openclaw-weixin',accountId:'test-account',sessionKey:session});
  const paused=await hooks.before_prompt_build({}, {channel:'openclaw-weixin',accountId:'test-account',chatId:'test-owner',sessionKey:session});
  assert.match(paused.prependContext,/paused_today/);
  assert.doesNotMatch(fs.readFileSync(path.join(root,receipt.paths[0]),'utf8'),/今天别问了/);
  // New event extraction uses the same native envelope and persists without
  // manufacturing a diary entry or sending any actual WeChat message.
  await hooks.message_received({from:'test-owner',messageId:'local-followup',timestamp:Date.now(),content:'我在调一个渲染问题'},
    {channelId:'openclaw-weixin',accountId:'test-account',sessionKey:session});
  const beforeFollowup=await hooks.before_prompt_build({}, {channel:'openclaw-weixin',accountId:'test-account',chatId:'test-owner',sessionKey:session});
  const followEvidence=JSON.parse(beforeFollowup.prependContext.split('\n').slice(1).join('\n'));
  const source=followEvidence.messages.find(m=>m.text==='我在调一个渲染问题');
  const followTool=tools[1]({sessionKey:session,deliveryContext:{to:'test-owner',channel:'openclaw-weixin',accountId:'test-account'}});
  const scheduled=await followTool.execute('schedule',{message_id:source.id,action:'upsert',category:'work',subject:'渲染问题',quote:source.text});
  assert.equal(JSON.parse(scheduled.content[0].text).status,'scheduled');
  const check=await hooks.before_prompt_build({}, {channel:'openclaw-weixin',accountId:'test-account',chatId:'test-owner',sessionKey:session});
  const queued=JSON.parse(check.prependContext.split('\n').slice(1).join('\n')).followups;
  assert.ok(queued.some(f=>f.subject==='渲染问题'&&f.status==='pending'));
  assert.doesNotMatch(fs.readFileSync(path.join(root,receipt.paths[0]),'utf8'),/渲染问题/);
  await hooks.message_received({from:'test-owner',messageId:'local-preference',timestamp:Date.now(),content:'以后每天问两次，语气简洁一点'},
    {channelId:'openclaw-weixin',accountId:'test-account',sessionKey:session});
  const preferencePrompt=await hooks.before_prompt_build({}, {channel:'openclaw-weixin',accountId:'test-account',chatId:'test-owner',sessionKey:session});
  const preferenceSource=JSON.parse(preferencePrompt.prependContext.split('\n').slice(1).join('\n')).messages.find(m=>m.text==='以后每天问两次，语气简洁一点');
  const preferenceTool=tools[2]({sessionKey:session,deliveryContext:{to:'test-owner',channel:'openclaw-weixin',accountId:'test-account'}});
  const changed=await preferenceTool.execute('frequency',{action:'frequency',daily_count:2,message_id:preferenceSource.id,quote:'以后每天问两次'});
  assert.equal(JSON.parse(changed.content[0].text).status,'2_windows');
  await preferenceTool.execute('tone',{action:'tone',tone:'简洁亲切',message_id:preferenceSource.id,quote:'语气简洁一点'});
  assert.match(fs.readFileSync(path.join(root,'SOUL.md'),'utf8'),/简洁亲切/);
  assert.equal(hooks.before_tool_call({toolName:'exec',params:{command:'pretend to edit'}}).block,true);
  // Actual Node -> Python knowledge writes in the temporary fixture only.
  await hooks.message_received({from:'test-owner',messageId:'local-knowledge',timestamp:Date.now(),content:'帮我建同事档案：测试同事喜欢茶饮'},
    {channelId:'openclaw-weixin',accountId:'test-account',sessionKey:session});
  const kp=await hooks.before_prompt_build({}, {channel:'openclaw-weixin',accountId:'test-account',chatId:'test-owner',sessionKey:session});
  const ks=JSON.parse(kp.prependContext.split('\n').slice(1).join('\n')).messages.find(m=>m.text==='帮我建同事档案：测试同事喜欢茶饮');
  const kt=tools.map(f=>f({sessionKey:session,deliveryContext:{to:'test-owner',channel:'openclaw-weixin',accountId:'test-account'}})).find(t=>t?.name==='life_knowledge_manage');
  const notePath='02_生活领域/08_关系与情感/同事与朋友/测试同事.md';
  const note={action:'create',path:notePath,content:'# 测试同事\n喜欢茶饮。',message_id:ks.id,quote:ks.text};
  assert.equal(JSON.parse((await kt.execute('create',note)).content[0].text).status,'created');
  assert.match(fs.readFileSync(path.join(root,notePath),'utf8'),/喜欢茶饮/);
  assert.equal(JSON.parse((await kt.execute('retry',note)).content[0].text).status,'already_saved');
  const loaded=JSON.parse((await kt.execute('read',{action:'read',path:notePath})).content[0].text);
  assert.ok(loaded.sha256);
  assert.equal(JSON.parse((await kt.execute('update',{...note,action:'replace',content:'# 测试同事\n喜欢茶饮，信息待补充。',expected_sha256:loaded.sha256})).content[0].text).status,'updated');
  await assert.rejects(kt.execute('blocked',{...note,path:'99_系统与模板/工具/no.py'}));
  // The profile is opt-in and uses the same trusted envelope, fixed paths and
  // durable projection, without adding a diary or sending a network message.
  await hooks.message_received({from:'test-owner',messageId:'profile-enable',timestamp:Date.now(),content:'开启资料更新'},
    {channelId:'openclaw-weixin',accountId:'test-account',sessionKey:session});
  await hooks.message_received({from:'test-owner',messageId:'profile-fact',timestamp:Date.now(),content:'我习惯午休时散步'},
    {channelId:'openclaw-weixin',accountId:'test-account',sessionKey:session});
  const pp=await hooks.before_prompt_build({}, {channel:'openclaw-weixin',accountId:'test-account',chatId:'test-owner',sessionKey:session});
  const ps=JSON.parse(pp.prependContext.split('\n').slice(1).join('\n')).messages.find(m=>m.text==='我习惯午休时散步');
  const pt=tools.map(f=>f({sessionKey:session,deliveryContext:{to:'test-owner',channel:'openclaw-weixin',accountId:'test-account'}})).find(t=>t?.name==='life_profile_manage');
  assert.ok(pt);
  const observation={action:'observe',message_id:ps.id,field:'habits',kind:'current',quote:ps.text,value:ps.text};
  const saved=JSON.parse((await pt.execute('profile-save',observation)).content[0].text);
  assert.equal(saved.status,'saved');
  assert.match(fs.readFileSync(path.join(root,saved.path),'utf8'),/我习惯午休时散步/);
  assert.equal(JSON.parse((await pt.execute('profile-retry',observation)).content[0].text).status,'already_saved');
  assert.equal(JSON.parse((await pt.execute('profile-status',{action:'status'})).content[0].text).current.habits.value,ps.text);
  await assert.rejects(pt.execute('profile-forged',{...observation,message_id:'invented'}));
});
