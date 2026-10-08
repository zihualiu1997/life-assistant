"""Five-day optional discovery and source-backed, local profile projections."""
import datetime as dt
import hashlib
import json
import re

from life_assistant import atomic_write, lock
from life_service.core import ServiceError
from .knowledge import physical, digest

BASE = '02_生活领域/10_自我认识/'
PROFILE = BASE + '个人现状.md'
START = '<!-- life-profile:start -->'
END = '<!-- life-profile:end -->'
FIELDS = {
    'name': '称呼', 'timezone': '生活地区与时区', 'collaboration': '交流与协作偏好',
    'boundaries': '记录与话题边界', 'rhythm': '日常节奏', 'energy': '主观精力规律',
    'focus': '当前关注', 'success': '期待的变化', 'resources': '可用时间与资源',
    'habits': '已有习惯与有效方法', 'limits': '现实限制', 'support': '希望的支持方式',
    'values': '重视的事情', 'work': '工作与责任', 'learning': '学习与创作',
    'food': '饮食偏好', 'movement': '运动习惯', 'health': '身体限制自述',
    'finance': '财务关注', 'relationships': '关系与支持', 'devices': '已有设备与资料',
}
# Topic wording contains no personal values; only these anonymous intents reach
# the existing wording service. Sensitive fields are never proactively asked.
QUESTIONS = [
    (1, 'name', '怎么称呼你比较舒服？'),
    (1, 'collaboration', '平时聊天，你喜欢简短直接一点，还是一起慢慢聊？'),
    (2, 'rhythm', '一个普通工作日，你大致怎么安排时间？'),
    (2, 'energy', '一天里，什么时候你通常比较有精神？'),
    (3, 'focus', '最近最想照顾好生活里的哪一件事？'),
    (3, 'success', '如果这件事有了进展，你最想看到什么变化？'),
    (4, 'habits', '最近有什么小习惯或办法，你觉得挺管用？'),
    (4, 'resources', '忙的时候，你愿意给这件事留多大一点时间？'),
    (5, 'support', '接下来你更希望我帮你记录、一起想办法，还是陪你回顾？'),
    (5, 'boundaries', '有什么话题你暂时不想聊，或不希望我记下来？'),
]
INTRO = ('不用一次填问卷。接下来五天，我可以每天偶尔问一两个小问题，'
         '把你愿意分享的情况和变化留在本地资料里。可以跳过、暂停，也可以随时关闭资料更新。'
         '想试试就说“开始认识我”。')
UNSAFE = re.compile(r'[?？]|假如|如果|例如|比如|测试|虚构|转发|他说|她说|朋友说|```|<[^>]*>')
SECRET = re.compile(r'密码|口令|密钥|授权码|令牌|身份证|银行卡号|api[_ -]?key|access_token|refresh_token|sk-[A-Za-z0-9]', re.I)
NO_SAVE = re.compile(r'只聊不记|不要保存|不要记录|别保存|别记下来')
CORRECTION = re.compile(r'现在|改为|改成|改了|更正|纠正|不再|以后|从今天|更新为|已经变|目前')
PLANNED = re.compile(r'打算|计划|准备|想要|希望|想学|想做|想去')
TRANSIENT = re.compile(r'今天|今晚|昨晚|昨天|这次|刚刚|刚才')
COMMANDS = {
    '开始认识我': 'start', '暂停认识我': 'pause', '暂停引导': 'pause',
    '继续认识我': 'resume', '恢复引导': 'resume', '结束引导': 'finish',
    '跳过这个问题': 'skip', '这题以后再说': 'defer', '这题不想记录': 'decline',
    '关闭资料更新': 'disable', '开启资料更新': 'enable',
}
BOUNDARY_NAMES = {**{name: field for field, name in FIELDS.items()},
                  '健康': 'health', '财务': 'finance', '关系': 'relationships',
                  '家庭': 'relationships', '工作': 'work', '运动': 'movement', '饮食': 'food',
                  '学习': 'learning', '精力': 'energy', '作息': 'rhythm'}


def read_text(path):
    if not path.exists():
        return ''
    if not path.is_file() or path.stat().st_size > 5_000_000:
        raise ServiceError('profile_file_too_large')
    return path.read_bytes().decode('utf-8-sig')


class Profile:
    def __init__(self, store):
        self.s = store
        self.root = store.root

    def path(self, relative):
        return physical(self.root, relative)

    def state(self):
        raw = read_text(self.path('.local/profile/state.json'))
        return json.loads(raw) if raw else {'version': 1, 'enabled': False, 'onboarding': 'not_started',
            'started': None, 'paused_at': None, 'questions': {}, 'events': [], 'controls': []}

    def recover(self):
        """Replay a prepared multi-file write; never overwrite a concurrent edit."""
        txn = self.path('.local/profile/transaction.json')
        raw = read_text(txn)
        if not raw:
            return
        plan = json.loads(raw)
        if plan['status'] == 'done':
            return
        for item in plan['files']:
            target = self.path(item['path'])
            current = target.read_bytes() if target.exists() else b''
            if digest(current) not in (item['before'], digest(item['text'].encode())):
                raise ServiceError('profile_concurrent_edit_needs_review')
        for item in plan['files']:
            target = self.path(item['path'])
            if not target.exists() or digest(target.read_bytes()) != digest(item['text'].encode()):
                atomic_write(target, item['text'])
        plan['status'] = 'done'
        atomic_write(txn, json.dumps(plan, ensure_ascii=False))

    def save(self, state, project=False):
        outputs = {}
        if project:
            outputs = self.projections(state)
        outputs['.local/profile/state.json'] = json.dumps(state, ensure_ascii=False, indent=2) + '\n'
        files = []
        op = hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        for relative, text in outputs.items():
            path = self.path(relative)
            before = path.read_bytes() if path.exists() else b''
            if before == text.encode():
                continue
            if before:
                backup = '.local/profile/backups/' + op + '/' + relative
                atomic_write(self.path(backup), before.decode('utf-8'))
            files.append({'path': relative, 'before': digest(before), 'text': text})
        if files:
            atomic_write(self.path('.local/profile/transaction.json'),
                         json.dumps({'status': 'pending', 'files': files}, ensure_ascii=False))
            self.recover()

    def locked(self):
        # Shared with life_knowledge_manage so a full-document replacement cannot
        # race a profile projection. All physical paths are rechecked per write.
        self.path('.local/locks/knowledge.lock')
        return lock(self.root, 'knowledge')

    def source(self, data):
        row = self.s.db.execute('SELECT text,at FROM messages WHERE id=? AND session=?',
                               (data.get('message_id'), data.get('session'))).fetchone()
        quote = data.get('quote')
        if (not row or not isinstance(quote, str) or not quote.strip() or quote not in row['text']
            or not self.s.now() - dt.timedelta(minutes=10) <= dt.datetime.fromisoformat(row['at']) <= self.s.now() + dt.timedelta(minutes=1)):
            raise ServiceError('profile_source_required')
        if NO_SAVE.search(row['text']) or '语音转写（待用户确认）' in row['text']:
            raise ServiceError('profile_save_declined')
        return row

    def control(self, text, source):
        clean = text.strip().rstrip('。！!')
        command = COMMANDS.get(clean)
        boundary = None
        for name, field in BOUNDARY_NAMES.items():
            if clean in (f'不要记录{name}', f'不想记录{name}', f'{name}不要记录', f'不想聊{name}'):
                command, boundary = 'decline_field', field
                break
            if clean in (f'允许记录{name}', f'重新记录{name}', f'重新允许记录{name}'):
                command, boundary = 'allow_field', field
                break
        if not command:
            return None
        with self.locked():
            self.recover()
            state = self.state()
            if any(c['source'] == source for c in state['controls']):
                return 'profile_already_applied'
            now = self.s.now()
            if boundary:
                if command == 'decline_field':
                    state['questions'][boundary] = {'status': 'declined', 'at': now.isoformat(), 'source': source}
                else:
                    state['questions'].pop(boundary, None)
            elif command == 'start':
                state['enabled'] = True
                # Explicit restart never discards known answers or asked topics.
                if not state['started']:
                    state['started'] = now.date().isoformat()
                state['onboarding'] = 'active'
            elif command in ('enable', 'disable'):
                state['enabled'] = command == 'enable'
            elif command == 'pause':
                state['onboarding'] = 'paused'
                state['paused_at'] = state['paused_at'] or now.date().isoformat()
            elif command == 'resume':
                if state['paused_at'] and state['started']:
                    delta = now.date() - dt.date.fromisoformat(state['paused_at'])
                    state['started'] = (dt.date.fromisoformat(state['started']) + delta).isoformat()
                state['paused_at'] = None
                if state['started']:
                    state['onboarding'] = 'active'
            elif command == 'finish':
                state['onboarding'] = 'finished'
            else:
                asked = self.asked()
                if not asked or now - dt.datetime.fromisoformat(asked[-1]['at']) > dt.timedelta(hours=24):
                    return 'profile_no_recent_question'
                field = asked[-1]['field']
                state['questions'][field] = {'status': {'skip':'skipped','defer':'deferred','decline':'declined'}[command],
                                            'at': now.isoformat(), 'source': source}
            state['controls'].append({'action': command, 'field': boundary, 'source': source, 'at': now.isoformat()})
            self.save(state)
            return 'profile_' + command

    def asked(self):
        result = []
        # Includes ambiguous and failed transport handoffs: never blindly retry.
        for row in self.s.db.execute('SELECT topics,at,status FROM deliveries ORDER BY at'):
            for topic in json.loads(row['topics']):
                if topic.startswith('onboarding:'):
                    result.append({'field': topic.split(':', 1)[1], 'at': row['at'], 'status': row['status']})
        return result

    def ready(self, now, slot):
        with self.locked():
            self.recover()
            state = self.state()
        if not state['enabled'] or state['onboarding'] != 'active' or not state['started']:
            return None
        stage = (now.date() - dt.date.fromisoformat(state['started'])).days + 1
        # No late catch-up interview. Missing answers stay unknown after day 5.
        if stage not in range(1, 6) or slot not in ('opening', 'afternoon', 'night', 'evening'):
            return None
        cadence = self.s.get('cadence_windows', list(self.s.c['windows']))
        if slot == 'evening' and any(s in cadence for s in ('opening', 'afternoon', 'night')):
            return None
        asked = self.asked()
        today = [a for a in asked if dt.datetime.fromisoformat(a['at']).astimezone(self.s.c['tz']).date() == now.date()]
        if len(today) >= 2 or (asked and now - dt.datetime.fromisoformat(asked[-1]['at']) < dt.timedelta(hours=4)):
            return None
        # Two unanswered discovery prompts pause discovery, not unrelated care.
        activity = self.s.get('last_activity', '')
        if sum(a['at'] > activity for a in asked) >= 2:
            return None
        answered = {e['field'] for e in state['events'] if e['kind'] in ('current','goal')}
        unavailable = answered | set(state['questions']) | {a['field'] for a in asked}
        for day, field, question in QUESTIONS:
            if day == stage and field not in unavailable:
                return {'field': field, 'question': question, 'stage': stage}
        return None

    def context(self):
        with self.locked():
            self.recover()
            state = self.state()
        current = self.current(state)
        for field, event in current.items():
            event = dict(event)
            confirmations = [e['at'] for e in state['events'] if e['field'] == field and e['value'] == event['value'] and e['kind'] == 'confirmation']
            event['last_confirmed_at'] = max([event['at']] + confirmations)
            days = 14 if field == 'health' else 365 if field in ('name', 'values', 'collaboration', 'boundaries') else 90
            event['review_due'] = self.s.now() - dt.datetime.fromisoformat(event['last_confirmed_at']) > dt.timedelta(days=days)
            current[field] = event
        stage = ((self.s.now().date() - dt.date.fromisoformat(state['started'])).days + 1) if state['started'] else 0
        return {'enabled': state['enabled'], 'onboarding': 'finished' if stage > 5 else state['onboarding'],
                'stage': min(stage, 5), 'introduction': INTRO if not state['started'] and not self.path(PROFILE).exists() else None,
                'profile_path': PROFILE, 'has_existing_profile': self.path(PROFILE).exists(),
                'current': current, 'questions': state['questions'],
                'pending': self.unresolved(state)[-10:],
                'recent_changes': state['events'][-5:]}

    @staticmethod
    def current(state):
        result = {}
        for event in state['events']:
            if event['kind'] in ('current', 'goal'):
                result[event['field']] = event
        return result

    @staticmethod
    def unresolved(state):
        pending = {}
        for event in state['events']:
            if event['kind'] == 'pending':
                pending.setdefault(event['field'], []).append(event)
            elif event.get('replaces'):
                pending.pop(event['field'], None)
        return [e for events in pending.values() for e in events]

    def manage(self, data):
        if data.get('action') == 'status' and not set(data) - {'action','session'}:
            return dict(status='read', **self.context())
        allowed = {'action','session','message_id','quote','field','value','kind','replaces','effective_date'}
        if not isinstance(data, dict) or set(data) - allowed or data.get('action') != 'observe':
            raise ServiceError('invalid_profile_operation')
        row = self.source(data)
        field, value, kind = data.get('field'), data.get('value'), data.get('kind')
        if (field not in FIELDS or not isinstance(value, str) or not 1 <= len(value) <= 600
            or value not in data['quote'] or len(data['quote']) > 1200
            or kind not in ('current','goal','event','historical') or UNSAFE.search(row['text']) or SECRET.search(row['text'])
            or any(ord(c) < 32 and c not in '\n\t' for c in value)):
            raise ServiceError('profile_fact_not_grounded')
        if kind == 'current' and (PLANNED.search(value) or TRANSIENT.search(value) or re.search(r'以前|曾经|去年|当时', value)):
            raise ServiceError('profile_use_goal_or_event')
        date = data.get('effective_date')
        if date:
            try:
                dt.date.fromisoformat(date)
            except (ValueError, TypeError):
                raise ServiceError('profile_invalid_date') from None
            if date not in data['quote']:
                raise ServiceError('profile_explicit_date_required')
        identity = digest(json.dumps([data['session'], data['message_id'], field, value, kind], ensure_ascii=False).encode())
        with self.locked():
            self.recover()
            state = self.state()
            if not state['enabled']:
                raise ServiceError('profile_updates_disabled')
            previous = next((e for e in state['events'] if e['id'] == identity), None)
            if previous:
                return {'status': 'already_saved', 'path': PROFILE, 'event_id': identity, 'kind': previous['kind']}
            if state['questions'].get(field, {}).get('status') == 'declined':
                raise ServiceError('profile_field_declined')
            current = self.current(state).get(field)
            replaces = data.get('replaces')
            if replaces and (not current or replaces != current['id'] or not CORRECTION.search(data['quote'])):
                raise ServiceError('profile_revision_conflict_or_correction_required')
            # Differently worded facts are conservatively treated as a conflict.
            if current and current['value'] != value and kind in ('current','goal') and not replaces:
                kind = 'pending'
            if current and current['value'] == value and kind == current['kind']:
                kind = 'confirmation'
            event = {'id': identity, 'field': field, 'value': value, 'kind': kind,
                     'quote': data['quote'], 'message_id': data['message_id'], 'source': 'wechat',
                     'at': self.s.now().isoformat(timespec='seconds'), 'source_at': row['at'],
                     'effective_date': date, 'replaces': replaces}
            state['events'].append(event)
            self.save(state, project=True)
            return {'status': 'saved', 'path': PROFILE, 'event_id': identity, 'kind': kind,
                    'history_path': BASE + '成长记录/' + event['at'][:7] + '.md'}

    def projections(self, state):
        def quoted(value):
            return '\n'.join('> ' + line for line in value.splitlines())
        statuses = {'current':'明确自述', 'goal':'愿望或计划，未视为已执行', 'pending':'待确认冲突',
                    'event':'阶段事件', 'historical':'历史自述', 'confirmation':'再次确认'}
        lines = [START, '## 渐进更新的当前摘要', '',
                 '以下为有来源的自述。日期是记录时间；发生日期未明确时保持未知。历史基线与领域细节仍保留在下方。', '']
        if not self.current(state):
            lines += ['尚无启用后新收录的当前条目；已有情况见下方资料基线。不会为了填满摘要重新询问或编造内容。', '']
        for field, event in self.current(state).items():
            last_confirmed = max([event['at']] + [e['at'] for e in state['events']
                if e['field'] == field and e['value'] == event['value'] and e['kind'] == 'confirmation'])
            lines += ['### ' + FIELDS[field], quoted(event['value']),
                f"- {statuses[event['kind']]}；来源时间 {event['source_at']}；最近确认记录 {last_confirmed}；来源 {event['source']} / `{event['message_id']}`。",
                f"- [变化与原话](成长记录/{event['at'][:7]}.md)；当前条目 `{event['id']}`。", '']
        unresolved = self.unresolved(state)
        if unresolved:
            lines += ['### 待确认变化', '']
            for event in unresolved:
                lines += [f"- {FIELDS[event['field']]}：{event['at']} 有不同说法，当前摘要暂未覆盖，见 [来源](成长记录/{event['at'][:7]}.md)。"]
        lines += ['', END]
        block = '\n'.join(lines)
        path = self.path(PROFILE)
        old = read_text(path)
        if START in old or END in old:
            if old.count(START) != 1 or old.count(END) != 1 or old.index(START) > old.index(END):
                raise ServiceError('profile_markers_need_review')
            old_block = old[old.index(START):old.index(END)+len(END)]
            if state.get('profile_block_hash') and digest(old_block.replace('\r\n', '\n').encode()) != state['profile_block_hash']:
                raise ServiceError('profile_summary_concurrent_edit_needs_review')
            text = old[:old.index(START)] + block + old[old.index(END)+len(END):]
        else:
            text = '# 个人现状\n\n' + block + ('\n\n## 已有资料基线（保留原文及日期）\n\n' + old if old else '\n')
        outputs = {PROFILE: text}
        months = sorted({e['at'][:7] for e in state['events']})
        for month in months:
            history = [f'# 生活变化记录 · {month}', '', '保留原话、来源和记录日期；只记录实际自述，不自动评判成长或推断人格。', '']
            for event in state['events']:
                if not event['at'].startswith(month):
                    continue
                history += [f"## {event['at']} · {FIELDS[event['field']]} · {statuses[event['kind']]}",
                    f"- 条目：`{event['id']}`；来源：{event['source']} / `{event['message_id']}`。",
                    f"- 原消息时间：{event['source_at']}；明确发生日期：{event['effective_date'] or '未提供'}。",
                    f"- 替换条目：{event['replaces'] or '无'}。", '', quoted(event['quote']), '']
            relative = BASE + '成长记录/' + month + '.md'
            generated = '\n'.join(history)
            # A generated historical page may also contain manual notes. Do not
            # replace it unless its last successful generated digest matches.
            previous = self.path(relative)
            expected = state.get('projection_hashes', {}).get(relative)
            if previous.exists() and digest(previous.read_bytes()) != expected:
                raise ServiceError('profile_history_concurrent_edit_needs_review')
            outputs[relative] = generated
        state['projection_hashes'] = {p: digest(t.encode()) for p, t in outputs.items() if p != PROFILE}
        state['profile_block_hash'] = digest(block.encode())
        return outputs
