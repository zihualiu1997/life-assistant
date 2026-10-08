"""Owner-evidenced Markdown data management, never an executable file editor."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import re

from life_assistant import atomic_write, lock
from life_service.core import ServiceError

AREAS = {'00_收件箱', '02_生活领域', '03_目标与项目', '04_知识与资源', '05_复盘与计划'}
PROTECTED = {'agents.md', 'agents.override.md', 'soul.md', 'skill.md', 'tools.md', 'identity.md', 'user.md',
             'heartbeat.md', 'memory.md', 'claude.md', 'gemini.md', 'copilot-instructions.md',
             '附件', '设备数据', '设备记录', '晨报', '明日安排', '工具', '脚本', '插件',
             'tools', 'scripts', 'plugins', 'src', 'node_modules', 'vendor', 'skills'}
CODE_SUFFIX = re.compile(r'\.(?:py|pyw|js|mjs|cjs|ts|tsx|jsx|ps1|psm1|bat|cmd|exe|dll|sh|json|yaml|yml|toml|ini|html)(?:\.|$)', re.I)
DEVICE = re.compile(r'^(?:con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])(?:\.|$)', re.I)
LIMIT = 100000


def digest(data):
    return hashlib.sha256(data).hexdigest()


def physical(root, relative):
    root = Path(root).resolve()
    path = root / relative
    for part in (path, *path.parents):
        if part == root:
            break
        if part.is_symlink() or part.is_junction():
            raise ServiceError('knowledge_link_refused')
        if part.exists() and part.is_file() and part.stat().st_nlink > 1:
            raise ServiceError('knowledge_hardlink_refused')
    if not path.resolve().is_relative_to(root):
        raise ServiceError('knowledge_path_refused')
    return path


def destination(root, relative, directory=False):
    if not isinstance(relative, str) or not 1 <= len(relative) <= 240 or '\\' in relative:
        raise ServiceError('knowledge_path_refused')
    parts = relative.split('/')
    if any(not p or p in ('.', '..') or p.startswith('.') or p.endswith((' ', '.'))
           or re.search(r'[\x00-\x1f\x7f<>:"|?*~]', p) or DEVICE.match(p)
           or p.casefold() in PROTECTED or CODE_SUFFIX.search(p) for p in parts):
        raise ServiceError('knowledge_path_refused')
    if relative != '总览.md' and (len(parts) < (1 if directory else 2) or parts[0] not in AREAS):
        raise ServiceError('knowledge_area_refused')
    if not directory and not parts[-1].endswith('.md'):
        raise ServiceError('knowledge_markdown_only')
    return physical(root, relative)


def file_bytes(path):
    if not path.exists():
        return b''
    if not path.is_file() or path.stat().st_size > LIMIT:
        raise ServiceError('knowledge_file_too_large_or_not_file')
    raw = path.read_bytes()
    try:
        raw.decode('utf-8')
    except UnicodeError:
        raise ServiceError('knowledge_utf8_required') from None
    return raw


def manage(store, data):
    if not isinstance(data, dict):
        raise ServiceError('invalid_knowledge_operation')
    action = data.get('action')
    allowed = {'action', 'path', 'session'} if action in ('read','list') else {
        'action','path','session','message_id','quote','content','expected_sha256'}
    if set(data) - allowed or action not in ('read','list','create','append','replace'):
        raise ServiceError('invalid_knowledge_operation')
    path = destination(store.root, data.get('path'), directory=action=='list')
    if action == 'list':
        if not path.is_dir():
            raise ServiceError('knowledge_directory_not_found')
        entries=[]
        scanned=0
        for child in path.iterdir():
            scanned += 1
            if scanned > 500 or len(entries) >= 100:
                return {'status':'listed','entries':entries,'truncated':True}
            relative=child.relative_to(store.root).as_posix()
            try:
                destination(store.root,relative,directory=child.is_dir())
            except ServiceError:
                continue
            entries.append({'path':relative,'kind':'directory' if child.is_dir() else 'note'})
        return {'status':'listed','entries':sorted(entries,key=lambda x:x['path']),'truncated':False}
    if action == 'read':
        if not path.is_file():
            raise ServiceError('knowledge_not_found')
        raw = file_bytes(path)
        return {'status':'read','path':data['path'],'sha256':digest(raw),'content':raw.decode('utf-8-sig')}
    content, quote = data.get('content'), data.get('quote')
    if not isinstance(content, str) or not content.strip() or len(content.encode('utf-8')) > LIMIT or '\x00' in content:
        raise ServiceError('invalid_knowledge_content')
    row = store.db.execute('SELECT text,at FROM messages WHERE id=? AND session=?',
                           (data.get('message_id'), data.get('session'))).fetchone()
    if (not row or not isinstance(quote, str) or not quote.strip() or quote not in row['text']
            or not store.now() - dt.timedelta(minutes=10) <= dt.datetime.fromisoformat(row['at']) <= store.now() + dt.timedelta(minutes=1)):
        raise ServiceError('knowledge_source_required')
    if '语音转写（待用户确认）' in row['text'] or re.search(r'只聊不记|不要保存|不要记录|别保存|别记下来', row['text']):
        raise ServiceError('knowledge_save_declined')
    # Retry identity includes payload but not the optional optimistic revision.
    op = digest(json.dumps([data['session'], data['message_id'], action, data['path'], content],ensure_ascii=False).encode())
    receipt_path = physical(store.root, f'.local/knowledge/receipts/{op}.json')
    backup = physical(store.root, f'.local/knowledge/backups/{op}.md')
    physical(store.root, '.local/locks/knowledge.lock')
    with lock(store.root, 'knowledge'):
        # Recheck after taking the shared lock; every destination is backend selected.
        path = destination(store.root, data['path'])
        previous = file_bytes(path)
        before_hash = digest(previous)
        prior = json.loads(receipt_path.read_text(encoding='utf-8')) if receipt_path.exists() else None
        if prior and prior['status'] == 'saved':
            if not path.is_file():
                raise ServiceError('knowledge_saved_file_missing')
            return {'status':'already_saved','path':data['path'],'sha256':before_hash}
        if prior and before_hash == prior['after_sha256']:
            prior['status'] = 'saved'
            atomic_write(receipt_path,json.dumps(prior,ensure_ascii=False,indent=2))
            return {'status':'already_saved','path':data['path'],'sha256':before_hash}
        if prior and before_hash != prior['before_sha256']:
            raise ServiceError('knowledge_recovery_conflict')
        if action == 'create' and path.exists():
            raise ServiceError('knowledge_already_exists')
        if action in ('append','replace') and not path.is_file():
            raise ServiceError('knowledge_not_found')
        if action == 'replace' and data.get('expected_sha256') != before_hash:
            raise ServiceError('knowledge_revision_conflict_read_first')
        if data.get('expected_sha256') and data['expected_sha256'] != before_hash:
            raise ServiceError('knowledge_revision_conflict_read_first')
        text = (previous.decode('utf-8').rstrip() + '\n\n' if action == 'append' else '') + content.rstrip() + '\n'
        provenance = {'at':store.now().isoformat(timespec='seconds'),'source':'wechat','message_id':data['message_id']}
        # Machine-controlled metadata cannot close the HTML comment with source text.
        marker = json.dumps(provenance,ensure_ascii=True).replace('--','\\u002d\\u002d')
        text += '\n<!-- life-knowledge '+marker+' -->\n'
        if len(text.encode('utf-8')) > LIMIT:
            raise ServiceError('knowledge_file_too_large_or_not_file')
        receipt = dict(provenance, status='pending', action=action, path=data['path'],
                       quote=quote, before_sha256=before_hash, after_sha256=digest(text.encode('utf-8')))
        if path.exists():
            atomic_write(backup, previous.decode('utf-8'))
            receipt['backup'] = backup.relative_to(store.root).as_posix()
        atomic_write(receipt_path, json.dumps(receipt,ensure_ascii=False,indent=2))
        atomic_write(path, text)
        receipt['status'] = 'saved'
        atomic_write(receipt_path, json.dumps(receipt,ensure_ascii=False,indent=2))
        return {'status':'created' if action == 'create' else 'updated', 'path':data['path'],
                'sha256':receipt['after_sha256'], 'source_message_id':data['message_id']}
