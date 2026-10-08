import datetime as dt
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from test_proactive import ProactiveFixture
from life_proactive import wording
from life_service.core import ServiceError


class WordingDeliveryTests(ProactiveFixture, unittest.TestCase):
    def test_sends_rendered_text_not_decision_template(self):
        old = self.s.decide()['body']
        self.s.tick(self.sender)
        self.assertEqual(self.sender.calls[0][1], self.wording.return_value)
        self.assertNotEqual(old, self.sender.calls[0][1])

    def test_model_failure_never_sends_template_or_consumes_window(self):
        self.wording.side_effect = ServiceError('checkin_style_invalid')
        result = self.s.tick(self.sender)
        self.assertEqual(result['reason'], 'wording_failed')
        self.assertEqual(self.sender.calls, [])
        self.assertEqual(self.s.db.execute('select count(*) from deliveries').fetchone()[0], 0)
        self.assertEqual(self.s.tick(self.sender)['reason'], 'wording_retry_wait')
        self.assertEqual(self.wording.call_count, 1)

    def test_pause_or_window_end_during_generation_prevents_delivery(self):
        for mode in ['pause','expiry']:
            with self.subTest(mode=mode):
                self.s.put('paused', False)
                self.advance('2026-09-30T13:15:00+08:00')
                def render(*args):
                    if mode == 'pause': self.s.put('paused', True)
                    else: self.advance('2026-09-30T13:45:00+08:00')
                    return '后来怎么样呀？'
                self.wording.side_effect = render
                self.assertEqual(self.s.tick(self.sender)['status'], 'suppressed')
                self.assertEqual(self.sender.calls, [])

    def test_three_retries_persist_and_defer_only_failed_slot(self):
        from life_proactive.core import Store
        self.wording.side_effect = ServiceError('checkin_style_invalid')
        for minute in [15, 16, 21, 26]:
            self.advance(f'2026-09-30T13:{minute}:00+08:00')
            result = self.s.tick(self.sender)
            self.s.close()
            self.s = Store(self.c, lambda: self.at)
            self.addCleanup(self.s.close)
        self.assertEqual(result['reason'], 'wording_retries_exhausted')
        self.assertEqual(self.wording.call_count, 4)
        self.advance('2026-09-30T13:32:00+08:00')
        self.assertEqual(self.s.tick(self.sender)['reason'], 'wording_retries_exhausted')
        self.assertEqual(self.wording.call_count, 4)
        bugs = list((self.root / '.local/proactive/bugs').glob('*.json'))
        self.assertEqual(len(bugs), 1)
        self.assertEqual(json.loads(bugs[0].read_text())['attempts'], 4)
        self.assertEqual(self.sender.calls, [])
        self.wording.side_effect = None
        self.advance('2026-09-30T20:45:00+08:00')
        self.assertEqual(self.s.tick(self.sender)['status'], 'sent')

    def test_style_change_during_generation_prevents_stale_send(self):
        def render(*args):
            (self.root / 'SOUL.md').write_text('## 语气\n新风格', encoding='utf-8')
            return '旧风格？'
        self.wording.side_effect = render
        self.assertEqual(self.s.tick(self.sender)['reason'], 'wording_context_changed')
        self.assertEqual(self.sender.calls, [])


class WordingRequestTests(unittest.TestCase):
    def test_model_receives_only_tone_and_anonymous_intents(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'SOUL.md').write_text('## 语气\n轻松简短', encoding='utf-8')
            original = {'topics':['followup:private-event-id:2','mood'], 'body':'PRIVATE_EVENT_AND_HEALTH','day':'2026-10-04'}
            response = json.dumps({'ids':[0,1],'text':'上次那件事后来怎么样啦？心情也跟我聊聊~'},ensure_ascii=False)
            with patch.object(wording,'load_config',return_value={}), patch.object(wording.model,'complete',return_value=response) as call:
                result = wording.render(root,original)
            sent = str(call.call_args)
            self.assertNotIn('PRIVATE_EVENT',sent)
            self.assertNotIn('private-event-id',sent)
            self.assertIn('轻松简短',sent)
            self.assertIn('怎么样',result)
            for raw in ['not json',json.dumps({'ids':[1],'text':'你好呀？'}),json.dumps({'ids':[0,1],'text':'已记录你的安排，请问还有什么？'})]:
                with patch.object(wording,'load_config',return_value={}), patch.object(wording.model,'complete',return_value=raw):
                    with self.assertRaises(ServiceError): wording.render(root,original)
