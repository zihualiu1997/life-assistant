"""Synthetic five-day users; mocked wording/transport, no external delivery."""
import datetime as dt
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from test_proactive import ProactiveFixture
from life_proactive.core import Store, load_config, ServiceError
from life_proactive.onboarding import PROFILE, BASE, START, END


class OnboardingTests(ProactiveFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        defaults = json.loads((Path(__file__).parents[2] / 'server/life_proactive/config.example.json').read_text())
        self.c.update({k: defaults[k] for k in ('mode','windows','daily_cap','weekly_cap')})
        self.advance('2026-10-08T09:00:00+08:00')
        self.serial = 0

    def say(self, text):
        self.serial += 1
        return self.ingest(text, 'onboard-' + str(self.serial))

    def observe(self, text='叫我小林', field='name', value=None, kind='current', **extra):
        receipt = self.say(text)
        return self.s.profile.manage(dict(action='observe', message_id=receipt['message_id'], session='owner-session',
                                         quote=text, value=value or text, field=field, kind=kind, **extra))

    def start(self):
        return self.say('开始认识我')

    def slot_at(self, day, slot):
        self.advance(f'2026-10-{day:02d}T{self.c["windows"][slot][0]}:00+08:00')

    def test_new_user_invitation_does_not_enable_without_response(self):
        self.assertIn('开始认识我', self.s.profile.context()['introduction'])
        self.slot_at(8, 'opening')
        self.assertFalse(self.s.decide()['topics'][0].startswith('onboarding:'))
        with self.assertRaisesRegex(ServiceError, 'disabled'):
            self.observe()

    def test_existing_user_is_not_reenrolled(self):
        p = self.root / PROFILE
        p.parent.mkdir(parents=True)
        p.write_text('# 原资料\n旧日期和原话\n', encoding='utf8')
        self.assertIsNone(self.s.profile.context()['introduction'])
        self.assertEqual(self.s.profile.context()['onboarding'], 'not_started')

    def test_five_days_two_single_questions_then_no_catchup(self):
        self.start()
        topics = []
        for day in range(8, 13):
            for slot in ('opening', 'afternoon'):
                self.slot_at(day, slot)
                d = self.s.decide()
                self.assertEqual(len(d['topics']), 1)
                self.assertTrue(d['topics'][0].startswith('onboarding:'))
                topics += d['topics']
                self.assertEqual(self.s.tick(self.sender)['status'], 'sent')
                field = d['topics'][0].split(':')[1]
                self.observe('我平时安排比较灵活', field=field)
            self.slot_at(day, 'night')
            self.assertFalse(any(t.startswith('onboarding:') for t in self.s.decide().get('topics', [])))
        self.assertEqual(len(set(topics)), 10)
        self.slot_at(13, 'opening')
        self.assertFalse(any(t.startswith('onboarding:') for t in self.s.decide()['topics']))
        self.assertEqual(self.s.profile.context()['onboarding'], 'finished')

    def test_known_answers_skip_even_before_stage(self):
        self.start()
        self.observe()
        self.slot_at(8, 'opening')
        self.assertEqual(self.s.decide()['topics'], ['onboarding:collaboration'])

    def test_two_unanswered_stop_discovery_without_inventing_answer(self):
        self.start()
        for slot in ('opening', 'afternoon'):
            self.slot_at(8, slot)
            self.s.tick(self.sender)
        self.slot_at(9, 'opening')
        self.assertFalse(self.s.decide()['topics'][0].startswith('onboarding:'))
        self.assertFalse(self.s.profile.context()['current'])
        self.say('我回来了')
        self.slot_at(9, 'afternoon')
        self.assertEqual(self.s.decide()['topics'], ['onboarding:rhythm'])

    def test_skip_defer_decline_have_distinct_persistent_states(self):
        self.start()
        self.slot_at(8, 'opening'); self.s.tick(self.sender)
        self.say('跳过这个问题')
        self.assertEqual(self.s.profile.context()['questions']['name']['status'], 'skipped')
        self.slot_at(8, 'afternoon'); self.s.tick(self.sender)
        self.say('这题以后再说')
        self.assertEqual(self.s.profile.context()['questions']['collaboration']['status'], 'deferred')
        self.slot_at(9, 'opening'); self.s.tick(self.sender)
        self.say('这题不想记录')
        with self.assertRaisesRegex(ServiceError, 'declined'):
            self.observe('我一般十点上班', field='rhythm')

    def test_pause_resume_shifts_days_and_does_not_remove_global_pause(self):
        self.start(); self.say('暂停认识我'); self.say('暂停提醒')
        self.slot_at(10, 'opening'); self.say('继续认识我')
        self.assertEqual(self.s.profile.context()['stage'], 1)
        self.assertEqual(self.s.decide()['reason'], 'paused')
        self.say('恢复提醒'); self.slot_at(10, 'afternoon')
        self.assertEqual(self.s.decide()['topics'], ['onboarding:name'])

    def test_disable_prevents_writes_and_questions(self):
        self.start(); self.say('关闭资料更新')
        with self.assertRaisesRegex(ServiceError, 'disabled'):
            self.observe()
        self.slot_at(8, 'opening')
        self.assertFalse(self.s.decide()['topics'][0].startswith('onboarding:'))
        self.say('开启资料更新')
        self.assertEqual(self.observe()['status'], 'saved')

    def test_dedup_conflict_correction_and_historical_event(self):
        self.start()
        first = self.observe()
        receipt = next(m for m in self.s.pending('owner-session')['messages'] if m['text'] == '叫我小林')
        self.assertEqual(self.s.profile.manage(dict(action='observe', message_id=receipt['id'], session='owner-session',
            quote='叫我小林', value='叫我小林', field='name', kind='current'))['status'], 'already_saved')
        self.assertEqual(self.observe('叫我阿林')['kind'], 'pending')
        self.assertEqual(self.s.profile.context()['current']['name']['value'], '叫我小林')
        self.observe('以后叫我阿林', replaces=first['event_id'])
        self.assertFalse(self.s.profile.context()['pending'])
        self.observe('以前大家叫我小树', kind='historical')
        self.assertEqual(self.s.profile.context()['current']['name']['value'], '以后叫我阿林')
        history = (self.root / BASE / '成长记录/2026-10.md').read_text(encoding='utf8')
        self.assertIn('叫我小林', history); self.assertIn('以前大家叫我小树', history)

    def test_preserves_old_profile_and_manual_edits_outside_generated_block(self):
        p = self.root / PROFILE; p.parent.mkdir(parents=True)
        p.write_text('# 原资料\n原问卷矛盾尚待确认\n', encoding='utf8')
        self.start(); self.observe()
        p.write_text(p.read_text(encoding='utf8') + '\n人工附注保留\n', encoding='utf8')
        self.observe('我每周散步', field='movement')
        text = p.read_text(encoding='utf8')
        self.assertIn('原问卷矛盾尚待确认', text); self.assertIn('人工附注保留', text)
        self.assertEqual(text.count(START), 1); self.assertEqual(text.count(END), 1)
        self.assertTrue(list((self.root / '.local/profile/backups').rglob('*.md')))

    def test_source_identity_excerpt_time_and_secret_checks(self):
        self.start()
        receipt = self.say('叫我小林')
        data = dict(action='observe', message_id=receipt['message_id'], session='owner-session',quote='叫我小林',value='小林',field='name',kind='current')
        for changes in [dict(session='other'),dict(message_id='forged'),dict(quote='假的'),dict(value='模型编造'),dict(field='arbitrary'),dict(path='../SOUL.md')]:
            with self.subTest(changes=changes), self.assertRaises(ServiceError):
                self.s.profile.manage(dict(data, **changes))
        self.advance('2026-10-08T09:11:00+08:00')
        with self.assertRaises(ServiceError): self.s.profile.manage(data)
        for text in ['只聊不记，叫我小林','如果我每周运动','朋友说他每周运动','我的密码是hello123','我喜欢散步？']:
            with self.subTest(text=text), self.assertRaises(ServiceError): self.observe(text)

    def test_goals_and_temporary_states_do_not_become_stable_facts(self):
        self.start()
        for text in ['我打算每天背词','今天我很累']:
            with self.assertRaisesRegex(ServiceError, 'goal_or_event'): self.observe(text, field='energy')
        self.observe('我打算每天背词', field='learning', kind='goal')
        self.observe('今天我很累', field='energy', kind='event')
        self.assertNotIn('energy', self.s.profile.context()['current'])
        self.assertEqual(self.s.profile.context()['current']['learning']['kind'], 'goal')

    def test_explicit_date_only_and_stale_correction_conflict(self):
        self.start(); first = self.observe()
        with self.assertRaisesRegex(ServiceError, 'explicit_date'): self.observe('我上周换工作', field='work', effective_date='2026-10-01')
        with self.assertRaisesRegex(ServiceError, 'revision'): self.observe('叫我阿林', replaces=first['event_id'])
        self.observe('现在叫我阿林', replaces=first['event_id'])
        with self.assertRaisesRegex(ServiceError, 'revision'): self.observe('现在叫我大林', replaces=first['event_id'])

    def test_crash_after_first_projection_recovers_without_duplicate(self):
        from life_proactive import onboarding
        self.start()
        real = onboarding.atomic_write
        hit = []
        def crash(path, text):
            if str(path).endswith('成长记录\\2026-10.md') or str(path).endswith('成长记录/2026-10.md'):
                hit.append(True)
                raise OSError('disk interrupted')
            return real(path, text)
        with patch.object(onboarding, 'atomic_write', side_effect=crash), self.assertRaises(OSError): self.observe()
        self.assertTrue(hit)
        self.assertEqual(self.s.profile.context()['current']['name']['value'], '叫我小林')
        self.assertEqual(len(self.s.profile.state()['events']), 1)

    def test_concurrent_manual_history_is_not_overwritten(self):
        self.start(); self.observe()
        p = self.root / BASE / '成长记录/2026-10.md'
        p.write_text(p.read_text(encoding='utf8') + '\n手工内容', encoding='utf8')
        with self.assertRaisesRegex(ServiceError, 'concurrent_edit'): self.observe('我每周散步', field='movement')
        self.assertIn('手工内容', p.read_text(encoding='utf8'))
        self.assertNotIn('movement', self.s.profile.context()['current'])

    def test_restart_and_month_rollover_keep_history(self):
        self.start(); self.observe()
        self.advance('2026-11-01T09:00:00+08:00')
        self.observe('我学会了做面包', field='habits', kind='event')
        other = Store(self.c, lambda: self.at)
        try:
            self.assertEqual(other.profile.context()['current']['name']['value'], '叫我小林')
            self.assertTrue((self.root / BASE / '成长记录/2026-10.md').exists())
            self.assertTrue((self.root / BASE / '成长记录/2026-11.md').exists())
        finally: other.close()

    def test_wording_failure_and_unknown_delivery_do_not_repeat(self):
        self.start(); self.slot_at(8, 'opening')
        self.wording.side_effect = ServiceError('unavailable')
        self.assertEqual(self.s.tick(self.sender)['reason'], 'wording_failed')
        self.assertFalse(self.s.profile.asked())
        self.wording.side_effect = None
        self.advance('2026-10-08T10:02:00+08:00')
        self.sender.status = 'timeout'
        self.assertEqual(self.s.tick(self.sender)['status'], 'unknown')
        self.slot_at(8, 'afternoon')
        self.assertEqual(self.s.decide()['topics'], ['onboarding:collaboration'])

    def test_global_caps_and_cooldown_still_apply(self):
        self.start(); self.slot_at(8, 'opening'); self.s.tick(self.sender)
        self.c['daily_cap'] = 1
        self.slot_at(8, 'afternoon'); self.assertEqual(self.s.decide()['reason'], 'daily_cap')

    def test_confirmation_marks_review_date_without_overwriting_current(self):
        self.start(); first = self.observe()
        self.advance('2027-11-01T09:00:00+08:00')
        self.assertTrue(self.s.profile.context()['current']['name']['review_due'])
        self.observe()
        self.assertFalse(self.s.profile.context()['current']['name']['review_due'])
        self.assertEqual(self.s.profile.context()['current']['name']['id'], first['event_id'])

    def test_symlink_and_hardlink_are_refused(self):
        self.start()
        with patch.object(Path, 'is_symlink', return_value=True), self.assertRaises(ServiceError):
            self.observe()

    def test_field_boundary_is_not_cleared_by_global_enable(self):
        self.start(); self.say('不要记录财务'); self.say('开启资料更新')
        with self.assertRaisesRegex(ServiceError, 'declined'):
            self.observe('我用记账本记录开支', field='finance')
        self.say('允许记录财务')
        self.assertEqual(self.observe('我用记账本记录开支', field='finance')['status'], 'saved')

    def test_manual_change_inside_summary_requires_merge(self):
        self.start(); self.observe()
        p = self.root / PROFILE
        p.write_text(p.read_text(encoding='utf8').replace('叫我小林', '人工修正'), encoding='utf8')
        with self.assertRaisesRegex(ServiceError, 'concurrent_edit'):
            self.observe('我习惯散步', field='habits')
        self.assertIn('人工修正', p.read_text(encoding='utf8'))

    def test_one_daily_window_still_allows_optional_discovery(self):
        self.start(); self.say('每天一次'); self.slot_at(8, 'evening')
        self.assertEqual(self.s.decide()['topics'], ['onboarding:name'])

    def test_new_user_has_no_device_assumption_and_no_extra_model_payload(self):
        from life_proactive.wording import INTENTS
        self.start(); self.observe('我有一块手表', field='devices')
        self.slot_at(8, 'opening')
        decision = self.s.decide()
        self.assertNotIn('手表', json.dumps(decision, ensure_ascii=False))
        for key, intent in INTENTS.items():
            if key.startswith('onboarding:'):
                self.assertNotIn('WHOOP', intent)
