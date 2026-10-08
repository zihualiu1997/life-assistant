"""Version-guarded local patch for Tencent Weixin 2.4.9; no downloads or sends."""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import shutil

HERE = Path(__file__).resolve().parent
MARKER = '// life-wechat-batching-v2'


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError('Plugin source changed; patch anchor must occur exactly once: ' + old[:70])
    return text.replace(old, new, 1)


def transform(text, kind):
    if MARKER in text:
        return text
    if '// life-wechat-batching-v1' in text:
        if kind == 'process':
            text = replace_once(text, 'prepareInboundBatch, rememberInboundBatch }', 'prepareInboundBatch, rememberInboundBatch, recordNativeTurn }')
            text = replace_once(text, 'const runId = randomUUID();', 'const runId = randomUUID();\n    recordNativeTurn(full, finalized, deps, runId);')
            text = replace_once(text, 'disableBlockStreaming: true,', 'runId,\n                    disableBlockStreaming: true,')
        return text.replace('// life-wechat-batching-v1', MARKER, 1)
    text = MARKER + '\n' + text
    if kind == 'monitor':
        text = 'import { createWeixinBatcher } from "../messaging/life-batching-adapter.mjs";\n' + text
        anchor = re.search(r'(?m)^(\s*)const configManager = new WeixinConfigManager\([^\n]+\);', text)
        if not anchor:
            raise ValueError('configManager anchor absent')
        text = replace_once(text, anchor.group(), anchor.group() + '\n' + anchor[1] +
                            'const lifeBatcher = createWeixinBatcher(opts, processOneMessage, configManager);')
        # Accept and persist the whole poll before advancing the server cursor.
        cursor = re.search(r'(?m)^([ \t]*)if \(resp.get_updates_buf != null[^\n]*\) \{\n.*?^\1\}', text, re.S)
        if not cursor:
            raise ValueError('sync cursor anchor absent')
        cursor_text = cursor.group()
        text = text[:cursor.start()] + text[cursor.end():]
        start = text.index('const fromUserId = full.from_user_id ?? "";')
        end = text.index('\n', text.index('});', text.index('await processOneMessage(full, {', start)))
        text = text[:start] + 'lifeBatcher.push(full);' + text[end:]
        after_loop = text.index('\n', text.index('}', start))
        text = text[:after_loop] + '\n' + cursor_text + text[after_loop:]
        text = replace_once(text, 'aLog.info(`Monitor stopped (aborted)`);',
                            'lifeBatcher.close();\n        aLog.info(`Monitor stopped (aborted)`);')
        text = replace_once(text, 'aLog.info(`Monitor ended`);', 'lifeBatcher.close();\n  aLog.info(`Monitor ended`);')
    else:
        text = 'import { prepareInboundBatch, rememberInboundBatch, recordNativeTurn } from "./life-batching-adapter.mjs";\n' + text
        start = text.index('  const mediaOpts')
        end = text.index('// --- Framework command authorization ---', start)
        text = text[:start] + '''  const {ctx, originalEntries} = await prepareInboundBatch(full, deps, {
    downloadMediaFromItem, weixinMessageToMsgContext, getActiveQuoteMediaSubdir,
    getWeixinMessageId, isMediaItem, resolveStoredQuoteContext,
  });

  ''' + text[end:]
        start = text.index('  resolveStoredQuoteContext(ctx, full, deps.accountId);')
        end = text.index('const route = deps.channelRuntime.routing.resolveAgentRoute({', start)
        text = text[:start] + '  await rememberInboundBatch(originalEntries, deps, getQuoteStore, logger);\n\n  ' + text[end:]
        text = text.replace('import type { WeixinInboundMediaOpts } from "./inbound.js";\n', '')
        text = replace_once(text, 'const runId = randomUUID();', 'const runId = randomUUID();\n    recordNativeTurn(full, finalized, deps, runId);')
        text = replace_once(text, 'disableBlockStreaming: true,', 'runId,\n                    disableBlockStreaming: true,')
    return text


def digest(data):
    return hashlib.sha256(data).hexdigest()
