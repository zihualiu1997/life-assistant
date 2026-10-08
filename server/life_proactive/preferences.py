"""Chat preferences: fixed destinations, no shell, code paths or general writes."""
import datetime as dt
import re
from pathlib import Path

from life_assistant import atomic_write, lock
from life_service.context import sections
from life_service.core import ServiceError


def bounded(root, relative):
    root = Path(root).resolve()
    path = root / relative
    for part in (path, *path.parents):
        if part == root:
            break
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise ServiceError('preference_link_refused')
    if not path.resolve().is_relative_to(root):
        raise ServiceError('preference_path_refused')
    return path


def apply(root, data):
    action = data.get('action')
    allowed = {'tone': {'action', 'tone'}, 'persona': {'action', 'persona'}, 'list': {'action'}}
    if action not in allowed or set(data) - allowed[action]:
        raise ServiceError('invalid_preference')
    library = bounded(root, '99_系统与模板/Persona/人物')
    if action == 'list':
        return {'status': 'available', 'personas': sorted(p.name for p in library.iterdir()
            if re.fullmatch(r'[a-z0-9-]+', p.name) and not p.is_symlink()
            and (p / 'SOUL.md').is_file()) if library.exists() else []}
    if action == 'persona':
        slug = data.get('persona', '')
        if not isinstance(slug, str) or not re.fullmatch(r'[a-z0-9-]{1,60}', slug):
            raise ServiceError('invalid_persona')
        candidate = bounded(root, f'99_系统与模板/Persona/人物/{slug}/SOUL.md')
        if not candidate.is_file():
            raise ServiceError('persona_not_found')
        tone = sections(candidate.read_text(encoding='utf-8-sig'), ['语气'])
        tone = re.sub(r'^## 语气\s*', '', tone).strip()
    else:
        tone = data.get('tone')
    if not isinstance(tone, str) or not 2 <= len(tone) <= 6000 or re.search(r'(?m)^\s*#|```|\x00', tone):
        raise ServiceError('invalid_tone')
    target = bounded(root, 'SOUL.md')
    backup = bounded(root, '.local/persona-history')
    bounded(root, '.local/locks')
    with lock(Path(root), 'persona'):
        previous = target.read_text(encoding='utf-8-sig') if target.exists() else ''
        updated = '# 说话风格：用户定制\n\n## 语气\n\n' + tone.strip() + '\n'
        if previous == updated:
            return {'status': 'unchanged', 'path': 'SOUL.md'}
        if previous:
            atomic_write(backup / (dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.md'), previous)
        atomic_write(target, updated)
    return {'status': 'updated', 'path': 'SOUL.md'}


def manage(store, data):
    allowed = {'action', 'message_id', 'session', 'quote', 'tone', 'persona', 'daily_count', 'control'}
    if set(data) - allowed:
        raise ServiceError('invalid_preference')
    action = data.get('action')
    if action == 'list':
        return apply(store.root, {'action': 'list'})
    row = store.db.execute('SELECT text,at FROM messages WHERE id=? AND session=?',
                           (data.get('message_id'), data.get('session'))).fetchone()
    quote = data.get('quote')
    if (not row or not isinstance(quote, str) or not quote.strip() or quote not in row['text']
            or dt.datetime.fromisoformat(row['at']) < store.now() - dt.timedelta(minutes=10)):
        raise ServiceError('preference_source_required')
    if action in ('frequency', 'control'):
        if action == 'frequency':
            count = data.get('daily_count')
            if type(count) is not int or not 1 <= count <= 6:
                raise ServiceError('invalid_frequency')
            text = f'每天{count}次'
        else:
            text = data.get('control')
            if text not in ('暂停提醒', '恢复提醒', '今天别问了', '恢复默认频率', '开启后续关心', '关闭后续关心'):
                raise ServiceError('invalid_preference_control')
        result = store.controls(text)
        if not result:
            raise ServiceError('unsupported_preference')
        store.db.commit()
        return {'status': result}
    fields = {'tone': {'action', 'tone'}, 'persona': {'action', 'persona'}}
    if action not in fields:
        raise ServiceError('invalid_preference')
    return apply(store.root, {k: v for k, v in data.items() if k in fields[action]})
