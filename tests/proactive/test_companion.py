"""Synthetic local evidence only; never calls the real WeChat sender."""
import datetime as dt
import json
from pathlib import Path
import unittest

from test_proactive import ProactiveFixture, Sender
from life_proactive.core import Store, load_config, ServiceError, SLOTS


class CompanionTest(ProactiveFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        defaults = json.loads((Path(__file__).resolve().parents[2] / "server/life_proactive/config.example.json").read_text())
        raw = json.loads(self.path.read_text())
        for key in ("mode", "windows", "daily_cap", "weekly_cap", "sharing_weekdays"):
            raw[key] = defaults[key]
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        self.c.update(load_config(self.path))

    def at_slot(self, slot, day="2026-09-30"):
        self.advance(f"{day}T{self.c['windows'][slot][0]}:00+08:00")

    def test_six_daily_contacts_cover_every_subject_without_replies(self):
        topics = []
        for slot in SLOTS:
            self.at_slot(slot)
            decision = self.s.decide()
            self.assertLessEqual(len(decision["topics"]), 2)
            topics.extend(decision["topics"])
            self.assertNotIn("可只答一项", decision["body"])
            self.assertEqual(self.s.tick(self.sender)["status"], "sent")
            self.assertEqual(self.s.tick(self.sender)["reason"], "window_consumed")
        self.assertEqual(set(topics), {"meal:breakfast", "meal:lunch", "meal:dinner", "plan:today", "state", "tasks", "mood", "share", "plan:tomorrow"})
        self.assertEqual(len(topics), 9)
        self.assertEqual(len(self.sender.calls), 6)
        self.assertFalse((self.root / "01_日常记录").exists())
        self.at_slot("opening", "2026-10-01")
        self.assertEqual(self.s.decide()["status"], "ready")

    def test_seven_unanswered_days_still_get_daily_coverage(self):
        for offset in range(7):
            day = (dt.date(2026, 9, 30) + dt.timedelta(days=offset)).isoformat()
            for slot in SLOTS:
                self.at_slot(slot, day)
                self.assertEqual(self.s.tick(self.sender)["status"], "sent")
        self.assertEqual(len(self.sender.calls), 42)
        self.at_slot("opening", "2026-10-07")
        self.assertEqual(self.s.tick(self.sender)["status"], "sent")

    def test_daily_cap_cannot_be_bypassed_by_no_reply_mode(self):
        self.at_slot("opening")
        self.c["daily_cap"] = 1
        self.assertEqual(self.s.tick(self.sender)["status"], "sent")
        self.at_slot("morning")
        self.assertEqual(self.s.decide()["reason"], "daily_cap")

    def test_rolling_cap_cannot_be_bypassed_by_no_reply_mode(self):
        self.c["weekly_cap"] = 1
        self.at_slot("opening"); self.s.tick(self.sender)
        self.at_slot("opening", "2026-10-01")
        self.assertEqual(self.s.decide()["reason"], "rolling_week_cap")

    def test_daily_mood_and_sharing_do_not_require_a_plan_or_weekday(self):
        self.c["plans"] = []
        for day in ("2026-09-30", "2026-10-01", "2026-10-02"):
            self.at_slot("afternoon", day)
            self.assertEqual(self.s.decide()["topics"], ["tasks", "mood"])
            self.at_slot("night", day)
            self.assertEqual(self.s.decide()["topics"], ["share", "plan:tomorrow"])

    def test_new_categories_record_original_once_and_reach_reply_context(self):
        raw = "今日安排：打算整理书桌；状态：昨晚没睡好，现在有点困；心情：有点烦；明日安排：准备去公园；分享：路上看到一只可爱的猫"
        result = self.ingest(raw)
        self.assertEqual(result["status"], "recorded")
        self.assertEqual(len(result["facts"]), 5)
        text = (self.root / result["paths"][0]).read_text(encoding="utf-8")
        self.assertIn("> " + raw, text)
        self.assertEqual(text.count("<!-- entry:"), 1)
        self.assertEqual({f["state"] for f in result["facts"] if f["kind"] == "plan"}, {"planned"})
        self.assertEqual(len(self.s.pending("owner-session")["facts_today"]), 5)
        self.at_slot("afternoon")
        self.assertEqual(self.s.decide()["topics"], ["tasks"])
        self.at_slot("night")
        self.assertEqual(self.s.decide()["reason"], "nothing_missing")

    def test_natural_language_tool_can_record_mood_state_and_plan(self):
        result = self.ingest("我有点累，不过心情挺好的，明天想去看展")
        items = [dict(day="2026-09-30", kind=k, name=n, state=s, quote=q) for k, n, s, q in
                 [("state", "energy", "reported", "我有点累"), ("mood", "feeling", "reported", "心情挺好的"),
                  ("plan", "tomorrow", "planned", "明天想去看展")]]
        record = self.s.record(result["message_id"], "owner-session", items)
        self.assertEqual(len(record["facts"]), 3)
        self.assertEqual(len(record["paths"]), 1)

    def test_plan_cannot_be_recorded_as_an_accomplished_fact(self):
        result = self.ingest("明天想看展")
        with self.assertRaisesRegex(ServiceError, "fact_not_grounded"):
            self.s.record(result["message_id"], "owner-session", [dict(day="2026-09-30", kind="plan", name="tomorrow", state="reported", quote="明天想看展")])

    def test_questions_hypotheticals_and_optout_are_not_mood_facts(self):
        for index, text in enumerate(["心情：如果今天开心", "状态：是不是没睡好？", "只聊不记，心情：有点烦"]):
            self.ingest(text, str(index))
        self.assertEqual(self.s.status()["facts"], 0)

    def test_tomorrow_intent_carries_to_next_morning_as_plan(self):
        self.ingest("明日安排：想整理书桌")
        self.at_slot("opening", "2026-10-01")
        decision = self.s.decide()
        self.assertIn("昨天说的", decision["body"])
        context = self.s.pending("owner-session")
        self.assertEqual(context["yesterday_tomorrow_plan"][0]["state"], "planned")
        self.assertFalse(context["facts_today"])
        self.assertIsNone(self.s.plan(self.at.date()))

    def test_other_entry_meals_and_state_suppress_known_topics(self):
        from life_assistant import journal
        journal(self.root, "2026-09-30", "desktop", "source", "早餐：鸡蛋；状态：精神不错；今日安排：想读书")
        self.at_slot("opening")
        self.assertEqual(self.s.decide()["reason"], "nothing_missing")
        self.at_slot("morning")
        self.assertEqual(self.s.decide()["reason"], "nothing_missing")

    def test_frequency_controls_survive_restart_and_default_restores_six(self):
        for command, expected in [("每天一次", 1), ("每天两次", 2), ("每天三次", 3), ("每天最多4次", 4), ("每天五次", 5), ("每天六次", 6)]:
            self.assertIsNotNone(self.s.controls(command))
            self.s.db.commit()
            available = 0
            for slot in SLOTS:
                self.at_slot(slot)
                available += self.s.decide()["status"] == "ready"
            self.assertEqual(available, expected, command)
        self.s.controls("每天两次"); self.s.db.commit()
        other = Store(self.c, lambda: self.at)
        try:
            self.assertEqual(other.get("cadence_windows"), ["midday", "evening"])
        finally:
            other.close()
        self.assertEqual(self.s.controls("恢复默认频率"), "six_windows")
        self.assertEqual(self.s.get("cadence_windows"), list(SLOTS))

    def test_pause_stays_in_force_even_when_frequency_is_restored(self):
        self.ingest("暂停提醒")
        self.s.controls("恢复默认频率"); self.s.db.commit()
        self.at_slot("night")
        self.assertEqual(self.s.decide()["reason"], "paused")
        self.s.controls("恢复提醒"); self.s.db.commit()
        self.assertEqual(self.s.decide()["status"], "ready")

    def test_no_late_catchup_and_recent_chat_still_respected(self):
        self.at_slot("night"); self.ingest("正在忙")
        self.assertEqual(self.s.decide()["reason"], "recent_conversation")
        self.advance("2026-09-30T23:00:00+08:00")
        self.assertEqual(self.s.decide()["reason"], "outside_window")
        self.advance("2026-10-01T09:59:00+08:00")
        self.assertEqual(self.s.decide()["reason"], "outside_window")

    def test_new_config_rejects_missing_overlap_late_windows_and_excess_caps(self):
        original = json.loads(self.path.read_text())
        for change in ("missing", "overlap", "late", "cap"):
            raw = json.loads(json.dumps(original))
            if change == "missing": raw["windows"].pop("night")
            if change == "overlap": raw["windows"]["morning"] = ["10:15", "11:00"]
            if change == "late": raw["windows"]["night"] = ["22:30", "23:30"]
            if change == "cap": raw["daily_cap"] = 7
            self.path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ServiceError, "invalid_proactive_config"):
                load_config(self.path)


if __name__ == "__main__":
    unittest.main()
