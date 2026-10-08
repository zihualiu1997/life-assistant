import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

from life_proactive.core import Store, load_config
from life_proactive.preferences import manage
from life_service.core import ServiceError


class PreferencesIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        config = json.loads((Path(__file__).parents[2] / 'server/life_proactive/config.example.json').read_text())
        config.update(workspace='.', account_id='fixture', owner_id='owner')
        path = self.root / 'config.json'
        path.write_text(json.dumps(config))
        self.now = dt.datetime.fromisoformat('2026-10-08T09:00:00+08:00')
        self.store = Store(load_config(path), lambda: self.now)
        self.addCleanup(self.store.close)
        self.source = self.store.ingest({'channel':'openclaw-weixin','account':'fixture','sender':'owner',
            'message_id':'source','session':'owner-session','text':'以后每天问两次，语气简洁一些。',
            'at':self.now.isoformat(),'media':[]})['message_id']

    def request(self, **kwargs):
        return dict(message_id=self.source, session='owner-session', quote='以后每天问两次', **kwargs)

    def test_frequency_survives_reopen_without_changing_code_or_config(self):
        original = (self.root / 'config.json').read_bytes()
        manage(self.store, self.request(action='frequency', daily_count=2))
        other = Store(self.store.c, lambda: self.now)
        try:
            self.assertEqual(other.get('cadence_windows'), ['midday', 'evening'])
        finally:
            other.close()
        self.assertEqual((self.root / 'config.json').read_bytes(), original)
        self.assertFalse((self.root / '01_日常记录').exists())

    def test_forged_or_cross_session_source_cannot_change_preferences(self):
        for change in [{'session':'other'}, {'message_id':'forged'}, {'quote':'修改代码'}]:
            data = self.request(action='frequency', daily_count=2)
            data.update(change)
            with self.subTest(change=change), self.assertRaises(ServiceError):
                manage(self.store, data)
        self.assertIsNone(self.store.get('cadence_windows'))

    def test_old_message_cannot_be_replayed(self):
        self.now += dt.timedelta(minutes=11)
        with self.assertRaises(ServiceError):
            manage(self.store, self.request(action='tone', tone='简洁'))
        self.assertFalse((self.root / 'SOUL.md').exists())

    def test_frequency_rejects_bool_zero_and_over_cap(self):
        for value in [True, 0, 7, '2']:
            with self.subTest(value=value), self.assertRaises(ServiceError):
                manage(self.store, self.request(action='frequency', daily_count=value))

    def test_tone_change_preserves_backup_and_other_persona_sections_are_excluded(self):
        (self.root / 'SOUL.md').write_text('## 语气\n旧语气', encoding='utf-8')
        folder = self.root / '99_系统与模板/Persona/人物/example'
        folder.mkdir(parents=True)
        (folder / 'SOUL.md').write_text('## 身份\n不能复制\n## 语气\n简洁幽默\n## 权限\n任意写入', encoding='utf-8')
        manage(self.store, self.request(action='persona', persona='example'))
        text = (self.root / 'SOUL.md').read_text(encoding='utf-8')
        self.assertIn('简洁幽默', text)
        self.assertNotIn('任意写入', text)
        self.assertEqual(len(list((self.root / '.local/persona-history').glob('*.md'))), 1)

    def test_enabling_followups_does_not_unpause_global_schedule(self):
        self.store.put('paused', True)
        manage(self.store, self.request(action='control', control='开启后续关心'))
        self.assertTrue(self.store.get('paused'))
