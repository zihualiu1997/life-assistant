import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {pathToFileURL} from 'node:url';
import {classifyReceipt} from './weixin-receipt.mjs';

// Only the final sanitized receipt leaves this process. No credentials in argv.
let raw = '';
for await (const part of process.stdin) raw += part;
const finish = result => process.stdout.write('__LIFE_RESULT__' + JSON.stringify(result) + '\n');
let sending = false;
try {
  const r = JSON.parse(raw);
  if (!/^[\w-]+$/.test(r.account)) throw new Error('invalid_account');
  const read = file => JSON.parse(fs.readFileSync(file, 'utf8').replace(/^\uFEFF/, ''));
  const folder = path.join(r.state, 'openclaw-weixin');
  const ids = read(path.join(folder, 'accounts.json'));
  if (ids.length !== 1 || ids[0] !== r.account) throw new Error('owner_binding_changed');
  const account = read(path.join(folder, 'accounts', r.account + '.json'));
  if (account.userId !== r.owner || !account.token) throw new Error('owner_binding_changed');
  const base = new URL(account.baseUrl);
  if (base.href !== 'https://ilinkai.weixin.qq.com/' || base.username || base.password) throw new Error('unexpected_wechat_endpoint');
  const token = read(path.join(folder, 'accounts', r.account + '.context-tokens.json'))[r.owner];
  if (!token) throw new Error('conversation_required');
  const apiPath = path.join(r.package, 'dist', 'src', 'api', 'api.js');
  if (!fs.existsSync(apiPath)) throw new Error('transport_not_installed');
  if (r.action === 'check') {
    finish({status:'ready'});
  } else {
    if (r.action !== 'send' || typeof r.text !== 'string' || r.text.length > 1200 || !r.key) throw new Error('invalid_send');
    process.env.OPENCLAW_STATE_DIR = r.state;
    const {sendMessage} = await import(pathToFileURL(apiPath).href);
    const id = 'life-checkin-' + crypto.createHash('sha256').update(r.account + ':' + r.key).digest('hex').slice(0,32);
    sending = true;
    const result = await sendMessage({baseUrl:base.href, token:account.token, timeoutMs:25000,
      body:{msg:{from_user_id:'',to_user_id:r.owner,client_id:id,message_type:2,message_state:2,
        item_list:[{type:1,text_item:{text:r.text}}],context_token:token}}});
    // Tencent's SendMessageResp declares ret and message_id optional. A JSON
    // object with no error is accepted by the official SDK; requiring ret=0
    // incorrectly treats its empty success envelope as a delivery failure.
    finish(classifyReceipt(result,id));
  }
} catch (error) {
  const rejected = /^sendMessage ret=/.test(String(error?.message || ''));
  finish({status:sending ? rejected ? 'failed':'unknown':'not_ready',
    error:sending ? rejected ? 'wechat_rejected':'transport_outcome_unknown':'wechat_preflight_failed'});
}
