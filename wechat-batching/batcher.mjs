import fs from 'node:fs';
import path from 'node:path';
import {createHash, randomUUID} from 'node:crypto';

const TEXT = 1, IMAGE = 2;
const MEDIA_TYPES = new Set([IMAGE, 3, 4, 5]); // voice, file (including archives), video
const DAY = 86400000;
const identity = m => String(m.message_id || m.client_id || m.item_list?.find(i => i.msg_id)?.msg_id ||
  (m.seq != null ? `seq:${m.seq}` : createHash('sha256').update(JSON.stringify(m)).digest('hex')));
const conversation = m => JSON.stringify([m.from_user_id, m.to_user_id || '', m.group_id || '']);
const hasMedia = m => m.item_list?.some(i => MEDIA_TYPES.has(i.type) || MEDIA_TYPES.has(i.ref_msg?.message_item?.type));
const command = m => m.item_list?.some(i => i.type === TEXT && /^\s*\//.test(i.text_item?.text || ''));

// Controls must be standalone typed text, never quoted material or a voice transcript.
function collectionControl(message) {
  const items = message.item_list || [];
  if (items.length !== 1 || items[0].type !== TEXT || items[0].ref_msg) return;
  const text = (items[0].text_item?.text || '').trim().toLowerCase().replace(/[\s，,。.!！?？；;：:]/gu, '');
  if (['等我发完', '开始收集', '/collect'].includes(text)) return 'collect';
  if (['发完了', '发完了开始分析', '/done'].includes(text)) return 'done';
  if (['取消这批', '取消收集', '/cancelbatch'].includes(text)) return 'cancel';
}

/** A durable per-account inbox. Polling must not await an AI turn. */
export function createBatcher({accountId, file, dispatch, allowed, onError = () => {}, log = () => {},
  imageMs, mediaMs = imageMs ?? 20000, textMs = 3000, now = Date.now, timer = setTimeout, cancel = clearTimeout,
  signal}) {
  let state = {version: 1, accountId, pending: [], active: [], uncertain: [], cancelled: [], seen: {}};
  let closed = false;
  const timers = new Map(), running = new Set();
  const save = () => {
    fs.mkdirSync(path.dirname(file), {recursive: true});
    const tmp = `${file}.${process.pid}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(state), {encoding: 'utf8', mode: 0o600});
    fs.renameSync(tmp, file);
  };
  if (fs.existsSync(file)) {
    state = JSON.parse(fs.readFileSync(file, 'utf8'));
    if (state.version !== 1 || state.accountId !== accountId) throw new Error('batch inbox identity mismatch');
    state.cancelled ??= [];
    for (const batch of [...state.pending, ...state.active, ...state.uncertain, ...state.cancelled]) batch.id ??= randomUUID();
    // A crash during dispatch has unknown delivery state. Never replay it automatically.
    if (state.active.length) {
      state.uncertain.push(...state.active.map(b => ({...b, reason: 'interrupted'})));
      state.active = [];
      save();
      onError('batching: interrupted delivery retained for review; not replayed');
    }
  }
  const clear = key => { if (timers.has(key)) cancel(timers.get(key)); timers.delete(key); };
  function arm(key) {
    clear(key);
    if (closed || running.has(key)) return;
    const batch = state.pending.find(b => b.key === key);
    if (!batch || batch.held) return;
    const handle = timer(() => {timers.delete(key); void flush(key);}, Math.max(0, batch.due - now()));
    handle?.unref?.();
    timers.set(key, handle);
  }
  async function flush(key) {
    if (closed || running.has(key)) return;
    const batch = state.pending.find(b => b.key === key);
    if (!batch || batch.held) return;
    if (batch.due > now()) {arm(key); return;}
    clear(key);
    running.add(key);
    state.pending.splice(state.pending.indexOf(batch), 1);
    state.active.push(batch);
    try {
      save(); // Durable claim before any download, model invocation or delivery.
      const messages = batch.messages.filter(m => allowed(m));
      if (messages.length) {
        const last = messages.at(-1);
        const token = batch.releaseMessage?.context_token || [...messages].reverse().find(m => m.context_token)?.context_token;
        await dispatch({...last, context_token: token, lifeBatch: messages, lifeBatchReleased: Boolean(batch.sealed)});
        log(`batching: dispatched messages=${messages.length} attachments=${messages.flatMap(m => m.item_list || []).filter(i => MEDIA_TYPES.has(i.type)).length}`);
      }
      state.active = state.active.filter(b => b.id !== batch.id);
      save();
    } catch {
      state.active = state.active.filter(b => b.id !== batch.id);
      state.uncertain.push({...batch, reason: 'dispatch_failed_or_unknown'});
      try {save();} catch { /* The pre-dispatch durable claim remains on disk. */ }
      onError('batching: failed or uncertain batch retained; not retried automatically');
    } finally {
      running.delete(key);
      arm(key);
    }
  }
  function push(message) {
    if (closed) throw new Error('batch inbox closed');
    if (!message.from_user_id || !allowed(message)) return false;
    const key = conversation(message), id = `${key}:${identity(message)}`;
    const at = now();
    for (const [k, time] of Object.entries(state.seen)) if (at - time > DAY) delete state.seen[k];
    if (state.seen[id] != null || [...state.pending, ...state.active, ...state.uncertain, ...state.cancelled]
      .some(b => b.key === key && [...b.messages, ...(b.controls || [])].some(m => identity(m) === identity(message)))) return false;
    // Snapshot permits safe redelivery if the local durable write fails.
    const before = structuredClone(state);
    const control = collectionControl(message);
    const isCommand = command(message);
    let batch = state.pending.find(b => b.key === key && !b.command && !b.sealed);
    if (control) {
      if (control === 'collect') {
        if (!batch) {
          batch = {id: randomUUID(), key, messages: [], command: false, due: null};
          state.pending.push(batch);
        }
        batch.held = true;
        batch.due = null;
      } else if (control === 'cancel') {
        // A submitted batch may still be waiting behind a running turn. Cancellation
        // may remove that pending batch, but never pretends to stop an active model.
        batch ??= state.pending.filter(b => b.key === key && !b.command).at(-1);
      }
      if (batch) {
        (batch.controls ??= []).push(message);
        if (control === 'done') {
          batch.held = false;
          batch.sealed = true;
          batch.due = at;
          batch.releaseMessage = message;
          if (!batch.messages.length) state.pending = state.pending.filter(b => b.id !== batch.id);
        } else if (control === 'cancel') {
          state.pending = state.pending.filter(b => b.id !== batch.id);
          state.cancelled.push({...batch, cancelledAt: at});
        }
      }
      state.seen[id] = at;
      try {save();} catch (error) {state = before; throw error;}
      arm(key);
      return true;
    }
    if (isCommand) {
      // Keep pending materials for the next ordinary turn. Commands are never appended to them.
      batch = {id: randomUUID(), key: `${key}:command:${identity(message)}`, messages: [], command: true, due: at};
      state.pending.push(batch);
    } else if (!batch) {
      batch = {id: randomUUID(), key, messages: [], command: false, due: at};
      state.pending.push(batch);
    }
    batch.messages.push(message);
    batch.due = batch.held ? null : isCommand ? at : at + (batch.messages.some(hasMedia) ? mediaMs : textMs);
    state.seen[id] = at;
    try {save();} catch (error) {state = before; throw error;}
    arm(batch.key);
    return true;
  }
  function close() {
    closed = true;
    for (const key of timers.keys()) clear(key);
    signal?.removeEventListener('abort', close);
  }
  if (signal?.aborted) close();
  else signal?.addEventListener('abort', close, {once: true});
  for (const b of state.pending) arm(b.key);
  return {push, close, snapshot: () => structuredClone(state)};
}

/** Load every attachment and preserve original message order and quote metadata. */
export async function prepareInboundBatch(full, deps, api) {
  const originals = full.lifeBatch || [full];
  const entries = [];
  for (const original of originals) {
    const media = [], missing = [];
    const transcripts = new Map(), warnings = [];
    const items = original.item_list || [];
    const main = items.filter(i => api.isMediaItem(i) && !(i.type === 3 && i.voice_item?.text && !api.preserveVoice));
    const referenced = main.length ? [] : items.flatMap(i => i.ref_msg?.message_item &&
      api.isMediaItem(i.ref_msg.message_item) ? [i.ref_msg.message_item] : []);
    for (const item of [...main, ...referenced]) {
      if (api.allowedMediaTypes && !api.allowedMediaTypes.includes(item.type)) {
        warnings.push('本期不支持视频及复杂附件；请明确告知用户，不能声称已经读取。');
        continue;
      }
      const opts = await api.downloadMediaFromItem(item, {
        cdnBaseUrl: deps.cdnBaseUrl, saveMedia: deps.channelRuntime.media.saveMediaBuffer,
        mediaSubdir: api.getActiveQuoteMediaSubdir(deps.accountId),
        log: deps.log, errLog: deps.errLog, label: referenced.length ? 'ref' : 'inbound',
      });
      const part = api.weixinMessageToMsgContext(original, deps.accountId, opts);
      if (item.type === 3 && api.transcribe && !item.voice_item?.text) {
        try {
          if (!part.MediaPath) throw new Error('missing_audio');
          const result = await api.transcribe(part.MediaPath);
          transcripts.set(item, '语音转写（待用户确认）：' + result.text);
        } catch { warnings.push('这条语音未能识别，请用户补发文字；不能猜测语音内容。'); }
      }
      if (part.MediaPath) media.push({path: part.MediaPath, contentType: part.MediaType,
        messageId: api.getWeixinMessageId(original), ...(item.file_item?.file_name ? {fileName: item.file_item.file_name} : {})});
      else missing.push(item.type);
    }
    const ctx = api.weixinMessageToMsgContext(original, deps.accountId);
    // The upstream converter only returns the first item's body.
    ctx.Body = items.map(item => transcripts.get(item) || (api.transcribe && item.type === 3 && item.voice_item?.text ? '平台语音转写（待用户确认）：' + item.voice_item.text : api.weixinMessageToMsgContext({...original, item_list: [item]}, deps.accountId).Body))
      .filter(Boolean).join('\n');
    ctx.ChannelPromptContext = [...(ctx.ChannelPromptContext || []), ...warnings];
    if (media.length) {
      ctx.MediaPaths = media.map(m => m.path);
      ctx.MediaTypes = media.map(m => m.contentType);
      ctx.media = media;
    }
    api.resolveStoredQuoteContext(ctx, original, deps.accountId);
    entries.push({original, ctx, missing, mainMediaItem: main[0]});
  }
  const last = entries.at(-1);
  const ctx = {...last.ctx, Body: entries.map(e => e.ctx.Body).filter(Boolean).join('\n'), context_token: full.context_token};
  const media = entries.flatMap(e => e.ctx.media || []);
  if (media.length) {
    ctx.MediaPaths = media.map(m => m.path);
    ctx.MediaTypes = media.map(m => m.contentType);
    ctx.media = media;
  }
  const items = originals.flatMap(m => m.item_list || []);
  const counts = [[IMAGE, '图片'], [4, '文件'], [3, '语音'], [5, '视频']]
    .map(([type, label]) => ({label, count: items.filter(i => i.type === type).length}));
  const expected = counts.reduce((n, item) => n + item.count, 0);
  const summary = counts.filter(item => item.count).map(item => `${item.label} ${item.count} 个`).join('、') || '无附件';
  const missing = entries.reduce((n, e) => n + e.missing.length, 0);
  ctx.ChannelPromptContext = entries.flatMap(e => e.ctx.ChannelPromptContext || []);
  if (entries.length > 1 || expected > 1 || full.lifeBatchReleased) ctx.ChannelPromptContext.push(
    `同一轮连续微信消息已合并，共 ${entries.length} 条，素材包括：${summary}。按发送顺序结合全部素材、文字和语音内容，理解为同一个请求，只给一份完整的最终回复，不逐个素材另发回复，也不发送进度消息。附件已收到不等于内容已读取，需实际检查文件、压缩包或语音后才能声称理解；无法读取或转写时说明具体缺失。`);
  if (full.lifeBatchReleased) ctx.ChannelPromptContext.push('用户已明确结束本轮收集，现在按这批消息中的要求处理；收集口令本身不属于待分析素材或日记内容。');
  if (missing) ctx.ChannelPromptContext.push(`本轮有 ${missing} 个附件下载失败，必须说明缺失，不能声称看过这些附件。`);
  return {ctx, originalEntries: entries, mainMediaItem: last.mainMediaItem};
}

export async function rememberInboundBatch(entries, deps, getQuoteStore, logger) {
  for (const {original, ctx, mainMediaItem} of entries) {
    const messageId = ctx.MessageSidFull;
    if (!messageId) continue;
    try {
      await getQuoteStore()?.put({accountId: deps.accountId, conversationId: original.from_user_id,
        messageId, direction: 'inbound', body: ctx.Body,
        ...(ctx.MediaPaths?.[0] ? {sourceMediaPath: ctx.MediaPaths[0], mediaMime: ctx.MediaTypes?.[0]} : {}),
        ...(mainMediaItem?.file_item?.file_name ? {mediaName: mainMediaItem.file_item.file_name} : {}),
        createdAt: original.create_time_ms ?? Date.now()});
    } catch {logger.warn('batching: original quote cache write failed');}
  }
}
