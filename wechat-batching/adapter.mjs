import path from 'node:path';
import fs from 'node:fs';
import {createHash} from 'node:crypto';
import {createBatcher} from './life-batcher.mjs';
import {loadWeixinAccount} from '../auth/accounts.js';
import {readFrameworkAllowFromList} from '../auth/pairing.js';
import {prepareInboundBatch as prepare, rememberInboundBatch} from './life-batcher.mjs';
export {rememberInboundBatch};

// The pinned gateway's message_received hook omits runId. Bind the SDK's own
// dispatch run to its authorized native envelope before starting the model.
export function recordNativeTurn(full, ctx, deps, runId) {
  if (process.env.LIFE_MANAGED !== '1') return;
  const cfg = deps.config.plugins?.entries?.['life-bridge']?.config;
  if (!cfg?.ownerId || cfg.accountId !== deps.accountId || full.group_id || full.from_user_id !== cfg.ownerId ||
      loadWeixinAccount(deps.accountId)?.userId?.trim() !== cfg.ownerId) throw new Error('native_turn_owner_mismatch');
  const root = process.env.OPENCLAW_STATE_DIR;
  if (!root || !path.isAbsolute(root) || !/^[0-9a-f-]{36}$/.test(runId)) throw new Error('native_turn_invalid_run');
  const id = ctx.MessageSidFull || ctx.MessageSid;
  const body = ctx.BodyForCommands ?? ctx.RawBody ?? ctx.Body;
  if (!id || !ctx.SessionKey || typeof body !== 'string' || body.length > 16000) throw new Error('native_turn_missing_source');
  const media = ctx.media || [];
  const sourceKind = body.includes('语音转写（待用户确认）') ? 'voice_transcript' :
    (media.length || ctx.MediaPath || ctx.MediaPaths?.length) ? 'unclassified' : 'user_text';
  const dir = path.join(root, 'life-native-turns');
  fs.mkdirSync(dir, {recursive:true,mode:0o700});
  const now = Date.now();
  for (const name of fs.readdirSync(dir)) {
    if (!/^[0-9a-f-]{36}\.json$/.test(name)) continue;
    const file = path.join(dir,name);
    if (now-fs.statSync(file).mtimeMs > 600000) fs.unlinkSync(file);
  }
  if (fs.readdirSync(dir).length >= 100) throw new Error('native_turn_queue_full');
  fs.writeFileSync(path.join(dir,runId+'.json'),JSON.stringify({id:String(id),body,session:ctx.SessionKey,
    accountId:deps.accountId,ownerId:cfg.ownerId,at:now,native:true,sourceKind}), {flag:'wx',mode:0o600});
}

export async function prepareInboundBatch(full, deps, api) {
  if (process.env.LIFE_MANAGED === '1') {
    api = {...api, allowedMediaTypes: [2, 3], preserveVoice: true, transcribe: async filename => {
      const token = fs.readFileSync(path.join(process.env.LIFE_DATA_DIR, '.secrets/bridge'), 'utf8');
      const response = await fetch('http://127.0.0.1:18932/internal/transcribe', {method:'POST',
        headers:{'Content-Type':'application/json',Authorization:'Bearer '+token}, body:JSON.stringify({path:filename}),
        redirect:'error', signal:AbortSignal.timeout(65000)});
      if (!response.ok) throw new Error('audio_transcription_failed');
      return response.json();
    }};
  }
  return prepare(full, deps, api);
}

export function createWeixinBatcher(opts, processOneMessage, configManager) {
  const stateDir = process.env.OPENCLAW_STATE_DIR;
  if (!stateDir || !path.isAbsolute(stateDir)) throw new Error('batching requires project state directory');
  const settings = opts.config.channels?.['openclaw-weixin']?.inboundBatching || {};
  const duration = (value, fallback) => Number.isInteger(value) && value >= 0 && value <= 120000 ? value : fallback;
  const log = opts.runtime?.log || (() => {});
  const batcher = createBatcher({
    accountId: opts.accountId,
    file: path.join(stateDir, 'weixin-inbound-batches', createHash('sha256').update(opts.accountId).digest('hex').slice(0, 24) + '.json'),
    mediaMs: duration(settings.mediaMs ?? settings.imageMs, 20000), textMs: duration(settings.textMs, 3000),
    signal: opts.abortSignal, log, onError: opts.runtime?.error || log,
    allowed(message) {
      const stored = readFrameworkAllowFromList(opts.accountId);
      const owner = loadWeixinAccount(opts.accountId)?.userId?.trim();
      const allowed = stored.length ? stored : owner ? [owner] : [];
      return !message.group_id && allowed.includes(message.from_user_id);
    },
    async dispatch(full) {
      const cached = await configManager.getForUser(full.from_user_id, full.context_token);
      if (opts.abortSignal?.aborted) throw new Error('batching stopped before dispatch');
      await processOneMessage(full, {
        accountId: opts.accountId, config: opts.config, channelRuntime: opts.channelRuntime,
        baseUrl: opts.baseUrl, cdnBaseUrl: opts.cdnBaseUrl, token: opts.token,
        typingTicket: cached.typingTicket, log, errLog: opts.runtime?.error || log,
      });
    },
  });
  log(`batching: enabled mediaMs=${duration(settings.mediaMs ?? settings.imageMs, 20000)} textMs=${duration(settings.textMs, 3000)} explicitCollection=true`);
  return batcher;
}
