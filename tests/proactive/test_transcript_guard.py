import datetime as dt
import unittest
from life_proactive.core import Store
from life_proactive.followups import safe_source


class TranscriptGuardTest(unittest.TestCase):
    def test_multiline_transcript_is_not_a_confirmed_fact_or_event(self):
        text = "平台语音转写（待用户确认）：\n午餐：吃了米饭\n我明天出结果"
        self.assertEqual(Store.parse(text, dt.date(2026, 10, 6)), [])
        self.assertFalse(safe_source(text))
