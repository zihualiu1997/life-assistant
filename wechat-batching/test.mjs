import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';
import {spawnSync} from 'node:child_process';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {createRequire} from 'node:module';
import {createBatcher, prepareInboundBatch, rememberInboundBatch} from './batcher.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..');
const sdk = process.env.LIFE_TEST_WECHAT_PACKAGE || path.join(root, 'gateway/node_modules/@tencent-weixin/openclaw-weixin');
const python = process.env.LIFE_TEST_PYTHON || (process.platform === 'win32' ? path.join(root, '.venv/Scripts/python.exe') : 'python3');
const msg = (id, items, sender = 'owner') => ({message_id: String(id), from_user_id: sender,
  to_user_id: 'bot', context_token: `token-${id}`, create_time_ms: 1000 + Number(id), item_list: items});
const picture = id => ({type: 2, image_item: {media: {full_url: `https://fixture.invalid/${id}`}}});
const words = text => ({type: 1, text_item: {text}});
const fileItem = name => ({type: 4, file_item: {file_name: name, media: {full_url: 'https://fixture.invalid/file'}}});
const voice = text => ({type: 3, voice_item: {media: {full_url: 'https://fixture.invalid/voice'}, ...(text ? {text} : {})}});
const video = {type: 5, video_item: {media: {full_url: 'https://fixture.invalid/video'}}};

test('installed WeChat decoder converts real SILK bytes to private WAV', async () => {
  const require = createRequire(path.join(sdk, 'package.json'));
  const {encode, isSilk} = require('silk-wasm');
  const pcm = Buffer.alloc(24000 * 2 / 5);
  for (let i = 0; i < pcm.length / 2; i++) pcm.writeInt16LE(Math.round(2000 * Math.sin(i * 0.1)), i * 2);
  const silk = await encode(pcm, 24000);
  assert.equal(isSilk(silk.data), true);
  const {silkToWav} = await import(pathToFileURL(path.join(sdk, 'dist/src/media/silk-transcode.js')));
  const wav = await silkToWav(Buffer.from(silk.data));
  assert.ok(wav && wav.length > 44);
  assert.equal(wav.toString('ascii', 0, 4), 'RIFF');
  assert.equal(wav.readUInt32LE(24), 24000);
  assert.equal(wav.readUInt16LE(22), 1);
});

test('native admission persists only the bound owner and SDK run in private instance state', async t => {
  const dir=temp(t), oldState=process.env.OPENCLAW_STATE_DIR, oldManaged=process.env.LIFE_MANAGED;
  process.env.OPENCLAW_STATE_DIR=dir;process.env.LIFE_MANAGED='1';
  t.after(()=>{for(const [key,value] of [['OPENCLAW_STATE_DIR',oldState],['LIFE_MANAGED',oldManaged]]) {
    if(value===undefined)delete process.env[key];else process.env[key]=value;
  }});
  const source=fs.readFileSync(path.join(here,'adapter.mjs'),'utf8');
  const module=new vm.SourceTextModule(source);
  await module.link(async specifier=>{
    const builtin=specifier.startsWith('node:')?await import(specifier):null;
    const imports=[...source.matchAll(/import\s+([^;]+?)\s+from\s+["']([^"']+)["'];/g)].filter(m=>m[2]===specifier);
    const names=imports.flatMap(m=>m[1].startsWith('{')?m[1].replace(/[{}]/g,'').split(',').map(n=>n.trim().split(' as ')[0]):['default']);
    return new vm.SyntheticModule([...new Set(names)],function(){for(const name of names)this.setExport(name,builtin?builtin[name]:name==='loadWeixinAccount'?()=>({userId:'owner'}):()=>{});});
  });
  await module.evaluate();
  const runId='00000000-0000-4000-8000-000000000001';
  const deps={accountId:'account',config:{plugins:{entries:{'life-bridge':{config:{accountId:'account',ownerId:'owner'}}}}}};
  const ctx={MessageSid:'native-id',SessionKey:'agent:main:main',Body:'原生虚构资料',media:[{kind:'image'}]};
  assert.throws(()=>module.namespace.recordNativeTurn({from_user_id:'other'},ctx,deps,runId),/owner_mismatch/);
  assert.equal(fs.existsSync(path.join(dir,'life-native-turns')),false);
  module.namespace.recordNativeTurn({from_user_id:'owner'},ctx,deps,runId);
  const file=path.join(dir,'life-native-turns',runId+'.json'), saved=JSON.parse(fs.readFileSync(file));
  assert.equal(saved.id,'native-id');assert.equal(saved.session,ctx.SessionKey);assert.equal(saved.sourceKind,'unclassified');
  if(process.platform!=='win32')assert.equal(fs.statSync(file).mode&0o777,0o600);
  assert.throws(()=>module.namespace.recordNativeTurn({from_user_id:'owner'},ctx,deps,runId),/EEXIST/);
});
function temp(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'wechat-batch-test-'));
  t.after(() => {
    const real = fs.realpathSync(dir), parent = fs.realpathSync(os.tmpdir());
    assert.equal(path.dirname(real), parent);
    assert.ok(path.basename(real).startsWith('wechat-batch-test-'));
    fs.rmSync(real, {recursive: true, force: true});
  });
  return dir;
}
function clock() {
  let time = 100000, id = 0;
  const tasks = new Map();
  return {now: () => time, timer: (fn, delay) => {tasks.set(++id, {fn, at: time + delay}); return id;},
    cancel: id => tasks.delete(id), async advance(ms) {
      time += ms;
      for (let i = 0; i < 10; i++) {
        const ready = [...tasks].filter(([, task]) => task.at <= time);
        if (!ready.length) break;
        for (const [id, task] of ready) {tasks.delete(id); task.fn();}
        await new Promise(resolve => setImmediate(resolve));
      }
    }};
}
function setup(t, overrides = {}) {
  const file = path.join(temp(t), 'queue.json'), time = clock(), sent = [], errors = [];
  const options = {accountId: 'fixture', file, ...time, allowed: m => ['owner', 'other'].includes(m.from_user_id),
    dispatch: async m => sent.push(m), onError: e => errors.push(e), ...overrides};
  const batcher = createBatcher(options);
  t.after(() => batcher.close());
  return {batcher, options, time, sent, errors, file};
}

test('nine photos plus question make one turn; every arrival resets the 20-second window', async t => {
  const {batcher, time, sent} = setup(t);
  for (let i = 1; i <= 9; i++) {batcher.push(msg(i, [picture(i)])); await time.advance(1000);}
  batcher.push(msg(10, [words('这九张图对应的提示词是什么？')]));
  await time.advance(19999); assert.equal(sent.length, 0);
  await time.advance(1); assert.equal(sent.length, 1);
  assert.equal(sent[0].lifeBatch.length, 10);
  assert.equal(sent[0].context_token, 'token-10');
  assert.equal(sent[0].lifeBatch.flatMap(m => m.item_list).filter(i => i.type === 2).length, 9);
});
test('a single image automatically dispatches after 20 seconds', async t => {
  const {batcher, time, sent} = setup(t);
  batcher.push(msg(1, [picture(1)])); await time.advance(19999); assert.equal(sent.length, 0);
  await time.advance(1); assert.equal(sent.length, 1);
});
for (const [name, item] of [['ZIP', fileItem('素材.zip')], ['PDF', fileItem('文档.pdf')],
  ['voice', voice()], ['transcribed voice', voice('请看这些文件')], ['video', video]]) {
  test(`${name} and a later question share the 20-second media window`, async t => {
    const {batcher, time, sent} = setup(t);
    batcher.push(msg(1, [item])); await time.advance(15000); assert.equal(sent.length, 0);
    batcher.push(msg(2, [words('结合刚才的材料分析')])); await time.advance(19999); assert.equal(sent.length, 0);
    await time.advance(1); assert.equal(sent.length, 1); assert.equal(sent[0].lifeBatch.length, 2);
  });
}
test('ZIP plus spoken instructions plus images remain one request', async t => {
  const {batcher, time, sent} = setup(t);
  batcher.push(msg(1, [fileItem('项目.zip')])); await time.advance(10000);
  batcher.push(msg(2, [voice('按这个截图修改压缩包里的网页')])); await time.advance(10000);
  batcher.push(msg(3, [picture(3)])); await time.advance(20000);
  assert.equal(sent.length, 1); assert.equal(sent[0].lifeBatch.length, 3);
  assert.equal(sent[0].lifeBatch[0].item_list[0].file_item.file_name, '项目.zip');
});
test('explicit collection holds mixed materials until done, without forwarding controls', async t => {
  const {batcher, time, sent} = setup(t);
  batcher.push(msg(1, [words('等我发完。')])); await time.advance(60000);
  batcher.push(msg(2, [fileItem('资料.zip')])); await time.advance(90000);
  batcher.push(msg(3, [voice('帮我结合文档和截图分析')])); await time.advance(90000);
  batcher.push(msg(4, [picture(4)])); await time.advance(3600000);
  assert.equal(sent.length, 0);
  batcher.push(msg(5, [words('发完了，开始分析。')])); await time.advance(0);
  assert.equal(sent.length, 1); assert.equal(sent[0].lifeBatchReleased, true);
  assert.deepEqual(sent[0].lifeBatch.map(m => m.message_id), ['2', '3', '4']);
  assert.equal(sent[0].context_token, 'token-5');
  assert.equal(batcher.snapshot().pending.length, 0);
});
test('collect can hold an existing automatic batch before its timer fires', async t => {
  const {batcher, time, sent} = setup(t);
  batcher.push(msg(1, [picture(1)])); await time.advance(19000);
  batcher.push(msg(2, [words('等我发完')])); await time.advance(120000);
  assert.equal(sent.length, 0);
  batcher.push(msg(3, [words('请分析构图')])); batcher.push(msg(4, [words('发完了')]));
  await time.advance(0); assert.equal(sent.length, 1); assert.equal(sent[0].lifeBatch.length, 2);
});
test('collection survives restart and duplicate controls; automatic mode resumes after done', async t => {
  const {batcher, options, time, sent} = setup(t);
  const begin = msg(1, [words('等我发完')]);
  batcher.push(begin); batcher.push(msg(2, [fileItem('说明.pdf')])); batcher.close();
  const restored = createBatcher(options); t.after(() => restored.close());
  await time.advance(180000); assert.equal(sent.length, 0);
  assert.equal(restored.push(begin), false);
  const end = msg(3, [words('发完了，开始分析')]);
  restored.push(end); assert.equal(restored.push(end), false);
  await time.advance(0); assert.equal(sent.length, 1);
  restored.push(msg(4, [words('新的普通聊天')])); await time.advance(3000);
  assert.equal(sent.length, 2); assert.equal(sent[1].lifeBatchReleased, false);
});
test('cancel preserves pending originals locally without analysis and exits collection', async t => {
  const {batcher, time, sent} = setup(t);
  batcher.push(msg(1, [words('等我发完')])); batcher.push(msg(2, [fileItem('取消的资料.zip')]));
  batcher.push(msg(3, [words('取消这批')])); await time.advance(120000);
  assert.equal(sent.length, 0); assert.equal(batcher.snapshot().pending.length, 0);
  assert.equal(batcher.snapshot().cancelled[0].messages[0].message_id, '2');
  assert.equal(batcher.push(msg(2, [fileItem('取消的资料.zip')])), false);
  batcher.push(msg(4, [words('重新聊一下')])); await time.advance(3000); assert.equal(sent.length, 1);
});
test('quoted control phrases, prose and transcribed voice are ordinary material', async t => {
  const {batcher, time, sent} = setup(t);
  batcher.push(msg(1, [words('等我发完')]));
  batcher.push(msg(2, [words('文章里有一句“发完了，开始分析”')]));
  batcher.push(msg(3, [{...words('发完了'), ref_msg: {title: 'quoted'}}]));
  batcher.push(msg(4, [voice('发完了')]));
  batcher.push(msg(5, [words('取消这批是什么意思？')]));
  await time.advance(120000); assert.equal(sent.length, 0);
  batcher.push(msg(6, [words('发完了')])); await time.advance(0);
  assert.equal(sent.length, 1); assert.equal(sent[0].lifeBatch.length, 4);
});
test('done seals the batch even if new messages arrive before dispatch or during another turn', async t => {
  let release;
  const received = [];
  const {batcher, time} = setup(t, {dispatch: async m => {
    received.push(m); if (received.length === 1) await new Promise(resolve => {release = resolve;});
  }});
  batcher.push(msg(1, [words('previous')])); await time.advance(3000);
  batcher.push(msg(2, [words('等我发完')])); batcher.push(msg(3, [picture(3)]));
  batcher.push(msg(4, [words('发完了')]));
  batcher.push(msg(5, [words('等我发完')])); batcher.push(msg(6, [fileItem('另一批.zip')]));
  batcher.push(msg(7, [words('发完了')])); batcher.push(msg(8, [words('第三批普通文字')]));
  await time.advance(30000); assert.equal(received.length, 1);
  release(); await new Promise(resolve => setImmediate(resolve)); await time.advance(0);
  assert.deepEqual(received.map(m => m.lifeBatch.map(n => n.message_id)), [['1'], ['3'], ['6'], ['8']]);
  assert.equal(batcher.snapshot().active.length, 0);
});
test('empty done and cancel are harmless; repeated collect keeps one group', async t => {
  const {batcher, time, sent} = setup(t);
  batcher.push(msg(1, [words('发完了')])); batcher.push(msg(2, [words('取消这批')]));
  batcher.push(msg(3, [words('等我发完')])); batcher.push(msg(4, [words('等我发完')]));
  assert.equal(batcher.snapshot().pending.length, 1);
  batcher.push(msg(5, [words('发完了')])); await time.advance(60000);
  assert.equal(sent.length, 0); assert.equal(batcher.snapshot().pending.length, 0);
});
test('slash aliases collect and release locally; other commands still bypass collection', async t => {
  const {batcher, time, sent} = setup(t);
  batcher.push(msg(1, [words('/collect')])); batcher.push(msg(2, [fileItem('文件.zip')]));
  batcher.push(msg(3, [words('/status')])); await time.advance(0);
  assert.equal(sent.length, 1); assert.equal(sent[0].message_id, '3');
  await time.advance(120000); assert.equal(sent.length, 1);
  batcher.push(msg(4, [words('/done')])); await time.advance(0);
  assert.equal(sent.length, 2); assert.equal(sent[1].message_id, '2');
  batcher.push(msg(5, [words('/collect')])); batcher.push(msg(6, [picture(6)]));
  batcher.push(msg(7, [words('/cancelbatch')])); await time.advance(60000); assert.equal(sent.length, 2);
});
test('one peer cannot release another peer collection', async t => {
  const {batcher, time, sent} = setup(t);
  batcher.push(msg(1, [words('等我发完')])); batcher.push(msg(2, [picture(2)]));
  batcher.push(msg(3, [words('发完了')], 'other')); await time.advance(120000); assert.equal(sent.length, 0);
  batcher.push(msg(4, [words('发完了')])); await time.advance(0); assert.equal(sent.length, 1);
});
test('text followed by photos and split text all remain ordered', async t => {
  const {batcher, time, sent} = setup(t);
  batcher.push(msg(1, [words('分析下面的图片')])); await time.advance(2000);
  batcher.push(msg(2, [picture(2)])); await time.advance(19000);
  batcher.push(msg(3, [words('重点看构图')])); await time.advance(20000);
  assert.deepEqual(sent[0].lifeBatch.map(m => m.message_id), ['1', '2', '3']);
  assert.equal(sent.length, 1);
});
test('text-only waits 3 seconds; peers stay separate; unauthorized messages are excluded', async t => {
  const {batcher, time, sent} = setup(t);
  assert.equal(batcher.push(msg(0, [words('x')], 'stranger')), false);
  batcher.push(msg(1, [words('第一句')])); batcher.push(msg(2, [words('第二句')]));
  batcher.push(msg(3, [words('独立会话')], 'other'));
  await time.advance(2999); assert.equal(sent.length, 0);
  await time.advance(1); assert.equal(sent.length, 2);
  assert.equal(sent[0].lifeBatch.length, 2); assert.equal(sent[1].lifeBatch.length, 1);
});
test('duplicate messages stay deduplicated across restart; pending images survive', async t => {
  const {batcher, options, time, sent} = setup(t);
  const image = msg(1, [picture(1)]);
  batcher.push(image); assert.equal(batcher.push(image), false); batcher.close();
  const restored = createBatcher(options); t.after(() => restored.close());
  assert.equal(restored.push(image), false); restored.push(msg(2, [words('解释这张图')]));
  await time.advance(20000); assert.equal(sent.length, 1); restored.close();
  const again = createBatcher(options); t.after(() => again.close());
  assert.equal(again.push(image), false); await time.advance(20000); assert.equal(sent.length, 1);
});
test('polling admits a second batch while a model turn runs, then runs it once in order', async t => {
  let release;
  const seen = [];
  const {batcher, time} = setup(t, {dispatch: async m => {
    seen.push(m); if (seen.length === 1) await new Promise(resolve => {release = resolve;});
  }});
  batcher.push(msg(1, [words('first')])); await time.advance(3000);
  batcher.push(msg(2, [picture(2)])); batcher.push(msg(3, [words('next')]));
  await time.advance(25000); assert.equal(seen.length, 1);
  release(); await new Promise(resolve => setImmediate(resolve)); await time.advance(0);
  assert.equal(seen.length, 2); assert.equal(seen[1].lifeBatch.length, 2);
});
test('commands bypass photo waiting and are never appended to pictures', async t => {
  const {batcher, time, sent} = setup(t);
  batcher.push(msg(1, [picture(1)])); batcher.push(msg(2, [words('/status')]));
  await time.advance(0); assert.equal(sent.length, 1); assert.equal(sent[0].lifeBatch.length, 1);
  assert.equal(sent[0].message_id, '2');
});
test('failed or interrupted delivery is retained without automatic resend', async t => {
  const {batcher, options, time, errors} = setup(t, {dispatch: async () => {throw new Error('fixture');}});
  batcher.push(msg(1, [words('test')])); await time.advance(3000);
  assert.equal(batcher.snapshot().uncertain.length, 1); assert.equal(errors.length, 1);
  batcher.close(); const restored = createBatcher(options); t.after(() => restored.close());
  assert.equal(restored.push(msg(1, [words('test')])), false);
  await time.advance(30000); assert.equal(errors.length, 1);
});
test('abort preserves pending items and prevents their dispatch', async t => {
  const controller = new AbortController();
  const {batcher, time, sent, file} = setup(t, {signal: controller.signal});
  batcher.push(msg(1, [picture(1)])); controller.abort(); await time.advance(30000);
  assert.equal(sent.length, 0); assert.equal(JSON.parse(fs.readFileSync(file)).pending.length, 1);
});

function inboundApi() {
  const quoted = [];
  const api = {
    isMediaItem: i => [2, 3, 4, 5].includes(i.type),
    getWeixinMessageId: m => m.message_id,
    getActiveQuoteMediaSubdir: () => 'fixture',
    resolveStoredQuoteContext() {},
    async downloadMediaFromItem(item) {
      if (item.fail) return {};
      if (item.type === 4) return {decryptedFilePath: `C:/fixture/${item.file_item.file_name}`,
        fileMediaType: item.file_item.file_name.endsWith('.zip') ? 'application/zip' : 'application/pdf'};
      if (item.type === 3) return {decryptedVoicePath: 'C:/fixture/voice.wav', voiceMediaType: 'audio/wav'};
      if (item.type === 5) return {decryptedVideoPath: 'C:/fixture/video.mp4'};
      return {decryptedPicPath: `C:/fixture/${item.image_item.media.full_url.split('/').at(-1)}.png`};
    },
    weixinMessageToMsgContext(m, accountId, opts) {
      const mediaPath = opts?.decryptedPicPath || opts?.decryptedFilePath || opts?.decryptedVoicePath || opts?.decryptedVideoPath;
      const mediaType = opts?.decryptedPicPath ? 'image/png' : opts?.fileMediaType || opts?.voiceMediaType || 'video/mp4';
      return {Body: m.item_list?.[0]?.text_item?.text || m.item_list?.[0]?.voice_item?.text ||
        ({2: '[图片]', 3: '[语音]', 4: '[文件]', 5: '[视频]'}[m.item_list?.[0]?.type] || ''),
        From: m.from_user_id, To: m.from_user_id, AccountId: accountId, MessageSid: `generated-${m.message_id}`,
        MessageSidFull: m.message_id, Timestamp: m.create_time_ms, Provider: 'openclaw-weixin',
        ChatType: 'direct', context_token: m.context_token,
        ...(mediaPath ? {MediaPath: mediaPath, MediaType: mediaType} : {})};
    },
    getQuoteStore: () => ({put: async value => quoted.push(value)}), quoted,
  };
  return api;
}
test('all nine decoded images and the question enter one context; original quote IDs retained', async () => {
  const api = inboundApi(), deps = {accountId: 'fixture', channelRuntime: {media: {saveMediaBuffer() {}}}};
  const lifeBatch = Array.from({length: 9}, (_, i) => msg(i + 1, [picture(i + 1)]));
  lifeBatch.push(msg(10, [words('分析这九张图的提示词')]));
  const {ctx, originalEntries} = await prepareInboundBatch({...lifeBatch.at(-1), lifeBatch}, deps, api);
  assert.equal(ctx.MediaPaths.length, 9); assert.equal(new Set(ctx.MediaPaths).size, 9);
  assert.match(ctx.Body, /分析这九张图的提示词/); assert.match(ctx.ChannelPromptContext[0], /只给一份完整/);
  assert.deepEqual(ctx.media.map(m => m.messageId), ['1','2','3','4','5','6','7','8','9']);
  await rememberInboundBatch(originalEntries, deps, api.getQuoteStore, {warn() {}});
  assert.equal(api.quoted.length, 10); assert.equal(api.quoted[0].messageId, '1');
  assert.equal(api.quoted[9].body, '分析这九张图的提示词');
});
test('a native multi-image message keeps all items and reports failed downloads honestly', async () => {
  const api = inboundApi();
  const full = msg(1, [picture(1), {...picture(2), fail: true}, picture(3), words('这是问题')]);
  const {ctx} = await prepareInboundBatch(full, {accountId: 'fixture', channelRuntime: {media: {}}}, api);
  assert.equal(ctx.MediaPaths.length, 2); assert.match(ctx.Body, /这是问题/);
  assert.match(ctx.ChannelPromptContext.join('\n'), /1 个附件下载失败/);
});
test('mixed materials preserve filenames, MIME types, raw audio, and transcribed instructions', async () => {
  const api = inboundApi();
  const lifeBatch = [msg(1, [fileItem('素材.zip'), fileItem('说明.pdf')]), msg(2, [voice()]),
    msg(3, [voice('根据压缩包和文档回答')]), msg(4, [picture(4), video])];
  const {ctx} = await prepareInboundBatch({...lifeBatch.at(-1), lifeBatch}, {accountId: 'fixture', channelRuntime: {media: {}}}, api);
  assert.deepEqual(ctx.MediaTypes, ['application/zip', 'application/pdf', 'audio/wav', 'image/png', 'video/mp4']);
  assert.equal(ctx.media[0].fileName, '素材.zip'); assert.equal(ctx.media[1].fileName, '说明.pdf');
  assert.match(ctx.Body, /根据压缩包和文档回答/);
  assert.match(ctx.ChannelPromptContext.join('\n'), /文件 2 个、语音 2 个/);
  assert.match(ctx.ChannelPromptContext.join('\n'), /附件已收到不等于内容已读取/);
});

test('patched installed processing code dispatches nine images and sends exactly one final reply (offline SDK)', async t => {
  const pkg = sdk;

  const script = 'import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); from install import transform; print(transform(Path(sys.argv[2]).read_text("utf-8"),"process"))';
  const result = spawnSync(python, ['-X', 'utf8', '-c', script, here, path.join(pkg, 'dist/src/messaging/process-message.js')], {encoding: 'utf8', windowsHide: true});
  assert.equal(result.status, 0, result.stderr);
  const api = inboundApi(), sent = [], dispatched = [], admitted = [];
  const logger = {info() {}, debug() {}, warn() {}, error() {}};
  const exports = {...api, prepareInboundBatch, rememberInboundBatch, recordNativeTurn: (...args) => admitted.push(args), logger,
    MessageItemType: {TEXT: 1, IMAGE: 2, VOICE: 3, FILE: 4, VIDEO: 5},
    createTypingCallbacks: () => ({}), resolvePreferredOpenClawTmpDir: os.tmpdir,
    resolveSenderCommandAuthorizationWithRuntime: async () => ({senderAllowedForCommands: true, commandAuthorized: true}),
    resolveDirectDmAuthorizationOutcome: () => 'allowed',
    isDebugMode: () => false, handleSlashCommand: async () => ({handled: false}),
    getContextTokenFromMsgContext: ctx => ctx.context_token, setContextToken() {},
    resolveReplyProgressMessagesEnabled: () => false, withPublishedModelRuntime: x => x,
    applyWeixinMessageSendingHook: async x => ({text: x.text}), emitWeixinMessageSent() {},
    sendMessageWeixin: async x => sent.push(x), redactBody: x => x, redactToken: () => 'redacted',
    StreamingMarkdownFilter: class {feed(s) {return s;} flush() {return '';}}};
  const module = new vm.SourceTextModule(result.stdout);
  await module.link(async (specifier) => {
    const imports = [...result.stdout.matchAll(/import\s+([^;]+?)\s+from\s+["']([^"']+)["'];/g)]
      .filter(m => m[2] === specifier);
    const builtin = specifier.startsWith('node:') ? await import(specifier) : null;
    const names = imports.flatMap(m => m[1].startsWith('{') ? m[1].replace(/[{}]/g, '').split(',').map(n => n.trim()).filter(Boolean) : ['default']);
    return new vm.SyntheticModule([...new Set(names)], function () {
      for (const name of names) this.setExport(name, builtin ? builtin[name] : exports[name] ?? (() => {}));
    });
  });
  await module.evaluate();
  const lifeBatch = Array.from({length: 9}, (_, i) => msg(i + 1, [picture(i + 1)]));
  lifeBatch.push(msg(10, [words('分析这九张图的提示词')]));
  const channelRuntime = {media: {saveMediaBuffer() {}}, commands: {},
    routing: {resolveAgentRoute: () => ({agentId: 'fixture', sessionKey: 'fixture-session'})},
    session: {resolveStorePath: () => 'fixture', recordInboundSession: async () => {}},
    reply: {finalizeInboundContext: x => x, resolveHumanDelayConfig: () => ({}),
      createReplyDispatcherWithTyping: x => ({dispatcher: x, replyOptions: {}, markDispatchIdle() {}}),
      withReplyDispatcher: x => x.run(),
      dispatchReplyFromConfig: async x => {assert.equal(x.replyOptions.runId,admitted.at(-1)[3]);dispatched.push(x.ctx); await x.dispatcher.deliver({text: '统一分析结果'});}}};
  // MessageItemType is only used for slash detection in the patched processor.
  // SDK boundary and outbound transport are mocked; no account or network access occurs.
  await module.namespace.processOneMessage({...lifeBatch.at(-1), lifeBatch}, {accountId: 'fixture', config: {}, channelRuntime, log() {}, errLog() {}});
  assert.equal(dispatched.length, 1); assert.equal(dispatched[0].MediaPaths.length, 9);
  assert.match(dispatched[0].Body, /分析这九张图的提示词/);
  assert.equal(sent.length, 1); assert.equal(sent[0].text, '统一分析结果');
  const mixed = [msg(11, [fileItem('资料.zip')]), msg(12, [voice('分析这个压缩包')]), msg(13, [voice()])];
  await module.namespace.processOneMessage({...mixed.at(-1), lifeBatch: mixed}, {accountId: 'fixture', config: {}, channelRuntime, log() {}, errLog() {}});
  assert.equal(dispatched.length, 2); assert.equal(sent.length, 2);
  assert.deepEqual(dispatched[1].MediaTypes, ['application/zip', 'audio/wav']);
  assert.equal(dispatched[1].media[0].fileName, '资料.zip');
  assert.match(dispatched[1].Body, /分析这个压缩包/);
  const {batcher, time} = setup(t, {dispatch: full => module.namespace.processOneMessage(full,
    {accountId: 'fixture', config: {}, channelRuntime, log() {}, errLog() {}})});
  batcher.push(msg(20, [words('等我发完')])); batcher.push(msg(21, [fileItem('完整项目.zip')]));
  await time.advance(120000); batcher.push(msg(22, [voice('分析项目结构')]));
  await time.advance(120000); assert.equal(dispatched.length, 2); assert.equal(sent.length, 2);
  batcher.push(msg(23, [words('发完了，开始分析')])); await time.advance(0);
  assert.equal(dispatched.length, 3); assert.equal(sent.length, 3);
  assert.equal(dispatched[2].media[0].fileName, '完整项目.zip');
  assert.match(dispatched[2].Body, /分析项目结构/);
  assert.doesNotMatch(dispatched[2].Body, /等我发完|发完了/);
  assert.match(dispatched[2].ChannelPromptContext.join('\n'), /用户已明确结束本轮收集/);
});

test('patched monitor admits the entire poll before saving its cursor, without waiting for the model', async () => {
  const pkg = sdk;
  const script = 'import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); from install import transform; print(transform(Path(sys.argv[2]).read_text("utf-8"),"monitor"))';
  const result = spawnSync(python,
    ['-X', 'utf8', '-c', script, here, path.join(pkg, 'dist/src/monitor/monitor.js')], {encoding: 'utf8', windowsHide: true});
  assert.equal(result.status, 0, result.stderr);
  const controller = new AbortController(), events = [];
  let polls = 0;
  const noop = () => {};
  const logger = {debug: noop, info: noop, error: noop, withAccount() {return this;}};
  const exports = {logger, redactBody: x => x,
    getSyncBufFilePath: () => 'fixture', loadGetUpdatesBuf: () => '', saveGetUpdatesBuf: () => events.push('cursor'),
    WeixinConfigManager: class {},
    createWeixinBatcher: () => ({push: m => events.push(m.message_id), close: () => events.push('closed')}),
    getUpdates: async () => {if (++polls > 1) {controller.abort(); throw new Error('aborted');}
      return {get_updates_buf: 'fixture-cursor', msgs: [msg(1, [picture(1)]), msg(2, [words('question')])]};},
    processOneMessage: () => {throw new Error('monitor must enqueue, not call AI directly');}};
  const module = new vm.SourceTextModule(result.stdout);
  await module.link(async specifier => {
    const found = [...result.stdout.matchAll(/import\s+\{([^}]+)\}\s+from\s+["']([^"']+)["'];/g)].find(m => m[2] === specifier);
    const names = found[1].split(',').map(n => n.trim()).filter(Boolean);
    return new vm.SyntheticModule(names, function () {for (const n of names) this.setExport(n, exports[n] ?? noop);});
  });
  await module.evaluate();
  await module.namespace.monitorWeixinProvider({accountId: 'fixture', channelRuntime: {}, config: {}, abortSignal: controller.signal});
  assert.deepEqual(events, ['1', '2', 'cursor', 'closed']);
});
