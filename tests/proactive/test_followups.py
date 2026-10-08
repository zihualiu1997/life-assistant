"""End-to-end scheduling tests with synthetic messages and a fake sender only."""
import datetime as dt
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from test_proactive import ProactiveFixture, Sender
from life_proactive.core import Store, SLOTS, iso, ServiceError
from life_proactive.followups import suggested_due
from life_proactive import wording


class FollowupTest(ProactiveFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.c.update(mode="companion", daily_cap=6, weekly_cap=42, followups={"enabled": True},
                      windows={"opening": ["10:00", "10:30"], "morning": ["11:00", "11:30"],
                               "midday": ["13:15", "13:45"], "afternoon": ["17:30", "18:00"],
                               "evening": ["20:45", "21:15"], "night": ["22:30", "23:00"]})
        self.advance("2026-09-30T09:00:00+08:00")

    def rows(self):
        return [dict(r) for r in self.s.db.execute("SELECT * FROM followups ORDER BY rowid")]

    def act(self, receipt, action, quote, fid, **kwargs):
        return self.s.manage_followup(dict(mid=receipt["message_id"], session="owner-session", action=action,
                                          quote=quote, followup_id=fid, **kwargs))

    def test_morning_cold_gets_one_afternoon_question_without_journal_or_extra_send(self):
        result = self.ingest("早上可能感冒了")
        self.assertEqual(result["followups"][0]["status"], "scheduled")
        self.assertFalse((self.root / "01_日常记录").exists())
        self.advance("2026-09-30T11:00:00+08:00")
        self.assertNotIn("感冒", self.s.decide()["body"])
        self.advance("2026-09-30T13:15:00+08:00")
        decision = self.s.decide()
        self.assertIn("可能感冒了", decision["body"])
        self.assertIn("meal:lunch", decision["topics"])
        self.assertEqual(len(decision["topics"]), 2)
        self.assertEqual(self.s.tick(self.sender)["status"], "sent")
        self.assertEqual(self.s.tick(self.sender)["reason"], "window_consumed")
        self.advance("2026-09-30T17:30:00+08:00")
        self.assertNotIn("感冒", self.s.decide()["body"])
        self.assertEqual(self.rows()[0]["status"], "asked")

    def test_all_twelve_categories_have_real_message_to_queue_coverage(self):
        examples = {"health": "我可能感冒了", "sleep": "昨晚没睡好", "exercise": "今天有训练课",
                    "mood": "我有点烦", "event": "明天下午三点面试", "work": "我在排查故障",
                    "learning": "今天第一次练口语", "creation": "我发了新的小红书",
                    "relationship": "我今天准备跟家人谈一下", "travel": "今天航班延误",
                    "errand": "我申请退款了", "joy": "今天收到了表扬，我被表扬了"}
        for i, (category, text) in enumerate(examples.items()):
            with self.subTest(category=category):
                self.ingest(text, f"category-{i}")
                self.assertEqual(self.rows()[-1]["category"], category)
        self.assertEqual(len(self.rows()), 12)

    def test_examples_forwarded_third_person_optout_questions_and_vague_plans_are_not_queued(self):
        examples = ["例如早上可能感冒了", "如果我感冒了", "转发：我可能感冒了", "我朋友感冒了",
                    "我没感冒", "我没去打球", "感冒怎么办？", "只聊不记，我有点烦", "不要保存，我胃疼",
                    "上周头疼", "以后有空想学英语", "最近想试用新设备", "她说身体不舒服", "我妈今天牙疼", "不是感冒", "感冒了吗"]
        for i, text in enumerate(examples):
            self.ingest(text, f"negative-{i}")
        self.assertFalse(self.rows())

    def test_repeated_delivery_and_model_extraction_do_not_duplicate(self):
        result = self.ingest("可能感冒了")
        again = self.ingest("可能感冒了")
        self.assertEqual(result, again)
        outcome = self.s.manage_followup(dict(mid=result["message_id"], session="owner-session", action="upsert",
                                             category="health", subject="感冒", quote="可能感冒了"))
        self.assertEqual(outcome["status"], "already_scheduled")
        self.assertEqual(len(self.rows()), 1)

    def test_restart_preserves_pending_and_one_send(self):
        self.ingest("我头疼")
        self.advance("2026-09-30T13:15:00+08:00")
        with_store = Store(self.c, lambda: self.at)
        try:
            self.assertIn("头疼", with_store.decide()["body"])
            self.assertEqual(with_store.tick(self.sender)["status"], "sent")
        finally:
            with_store.close()
        self.assertEqual(self.s.tick(self.sender)["reason"], "window_consumed")

    def test_partial_improvement_is_new_progress_not_resolution_and_not_twice_same_day(self):
        self.ingest("可能感冒了")
        self.advance("2026-09-30T13:15:00+08:00"); self.s.tick(self.sender)
        self.advance("2026-09-30T14:00:00+08:00")
        self.ingest("好一点了，但还是有点感冒", "update")
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["status"], "pending")
        self.advance("2026-09-30T20:45:00+08:00")
        self.assertNotIn("感冒", self.s.decide()["body"])
        self.advance("2026-10-01T10:00:00+08:00")
        self.assertIn("感冒", self.s.decide()["body"])

    def test_explicit_resolution_closes_and_ordinary_chitchat_does_not(self):
        self.ingest("我头疼")
        self.ingest("好的谢谢", "other")
        self.assertEqual(self.rows()[0]["status"], "pending")
        self.ingest("头疼已经好了", "resolved")
        self.assertEqual(self.rows()[0]["status"], "resolved")

    def test_contextual_stop_and_wait_never_resume_from_silence(self):
        self.ingest("我头疼")
        self.ingest("这件事别问了", "stop")
        self.assertEqual(self.rows()[0]["status"], "cancelled")
        self.ingest("明天下午三点面试", "interview")
        self.ingest("面试等我有结果再说", "wait")
        self.assertEqual(self.rows()[1]["status"], "waiting")
        self.advance("2026-10-01T20:45:00+08:00")
        self.assertNotIn("面试", self.s.decide()["body"])

    def test_ambiguous_stop_does_not_cancel_multiple_events(self):
        self.ingest("我头疼")
        self.ingest("明天有面试", "second")
        result = self.ingest("这件事别问了", "ambiguous")
        self.assertEqual(result["followups"][0]["status"], "needs_target")
        self.assertTrue(all(r["status"] == "pending" for r in self.rows()))

    def test_reschedule_uses_new_date_and_retains_identity(self):
        self.ingest("今天下午三点面试")
        fid = self.rows()[0]["id"]
        self.ingest("面试改到明天下午三点", "changed")
        row = self.rows()[0]
        self.assertEqual(row["id"], fid)
        self.assertEqual(row["due_at"], "2026-10-01T17:00:00+08:00")
        self.advance("2026-09-30T17:30:00+08:00")
        self.assertNotIn("面试", self.s.decide()["body"])
        self.advance("2026-10-01T17:30:00+08:00")
        self.assertIn("面试", self.s.decide()["body"])

    def test_weekday_and_cross_year_dates_and_time_range(self):
        self.assertEqual(suggested_due("下周五出面试结果", self.at, "event").date(), dt.date(2026, 10, 9))
        self.assertEqual(suggested_due("明天下午14:00到17:00打球", self.at, "exercise").hour, 20)
        at = dt.datetime.fromisoformat("2026-12-31T20:00:00+08:00")
        self.assertEqual(suggested_due("明天面试", at, "event").date(), dt.date(2027, 1, 1))

    def test_separate_events_in_same_category_stay_separate(self):
        self.ingest("明天公司A面试")
        self.ingest("后天公司B面试", "second")
        self.assertEqual(len(self.rows()), 2)

    def test_pause_frequency_cooldown_and_caps_also_apply_to_followups(self):
        self.ingest("我头疼")
        self.advance("2026-09-30T13:15:00+08:00")
        self.s.controls("暂停提醒"); self.s.db.commit()
        self.assertEqual(self.s.decide()["reason"], "paused")
        self.s.controls("开启后续关心"); self.s.db.commit()
        self.assertEqual(self.s.decide()["reason"], "paused")
        self.s.controls("恢复提醒"); self.s.controls("每天一次"); self.s.db.commit()
        self.assertEqual(self.s.decide()["reason"], "evening_only")
        self.advance("2026-09-30T20:45:00+08:00")
        self.ingest("聊个别的", "chat")
        self.assertEqual(self.s.decide()["reason"], "recent_conversation")
        self.advance("2026-09-30T23:00:00+08:00")
        self.assertEqual(self.s.decide()["reason"], "outside_window")

    def test_six_rounds_never_expand_with_multiple_pending_events(self):
        self.ingest("我头疼")
        self.ingest("今天有点烦", "mood")
        self.ingest("今天排查故障", "work")
        for slot in SLOTS:
            self.advance(f"2026-09-30T{self.c['windows'][slot][0]}:00+08:00")
            decision = self.s.decide()
            self.assertLessEqual(len(decision["topics"]), 2)
            self.s.tick(self.sender)
        self.assertEqual(len(self.sender.calls), 6)

    def test_failed_and_unknown_followups_are_not_retried(self):
        for offset, status in enumerate(("failed", "unknown", "timeout")):
            with self.subTest(status=status):
                day = (dt.date(2026, 9, 30) + dt.timedelta(days=offset)).isoformat()
                self.advance(f"{day}T09:00:00+08:00")
                self.ingest("可能感冒了", f"source-{status}")
                self.advance(f"{day}T13:15:00+08:00")
                self.s.tick(Sender(status))
                self.advance(f"{day}T17:30:00+08:00")
                self.assertFalse(any(t.startswith("followup:") for t in self.s.decide()["topics"]))

    def test_expired_followup_is_not_caught_up_after_long_offline(self):
        self.ingest("我头疼")
        self.advance("2026-10-04T10:00:00+08:00")
        self.assertNotIn("头疼", self.s.decide()["body"])
        self.s.tick(self.sender)
        self.assertEqual(self.rows()[0]["status"], "expired")

    def test_tool_cannot_use_wrong_session_fabricated_quote_or_unconfirmed_resolution(self):
        r = self.ingest("我头疼")
        fid = self.rows()[0]["id"]
        for change in [dict(session="other"), dict(quote="伪造原话"), dict(action="resolve"), dict(action="cancel")]:
            args = dict(mid=r["message_id"], session="owner-session", action="upsert", quote="我头疼", followup_id=fid)
            args.update(change)
            with self.assertRaises(ServiceError):
                self.s.manage_followup(args)
        self.assertEqual(self.rows()[0]["status"], "pending")

    def test_tool_cannot_schedule_before_explicit_event_or_beyond_horizon(self):
        r = self.ingest("下周五面试")
        fid = self.rows()[0]["id"]
        for value in ("2026-09-30T17:00:00+08:00", "2027-09-30T17:00:00+08:00", "2026-10-09T20:00:00"):
            with self.assertRaises(ServiceError):
                self.act(r, "reschedule", "下周五面试", fid, due_at=value)

    def test_disabled_feature_is_persistent_and_does_not_stop_normal_meals(self):
        self.ingest("关闭后续关心")
        self.ingest("我头疼", "symptom")
        self.assertFalse(self.rows())
        self.advance("2026-09-30T13:15:00+08:00")
        self.assertIn("meal:lunch", self.s.decide()["topics"])
        self.assertFalse(self.s.pending("owner-session")["followups_enabled"])

    def test_sixty_candidate_situations_are_recognized_without_real_records(self):
        cases = {
            "health": ["我可能感冒了", "我头疼", "我胃不舒服", "今天落枕了", "我牙疼"],
            "sleep": ["昨晚没睡好", "昨晚熬夜赶工", "白天困得厉害", "准备午睡", "今晚想早点睡"],
            "exercise": ["今天约了球局", "今天上训练课", "今天练了新动作", "运动后不舒服", "今天参加比赛"],
            "mood": ["我有点烦", "今天受了委屈", "今天有点失落", "觉得孤单", "最近压力很大"],
            "event": ["明天面试", "明天考试", "下午工作汇报", "今天有重要谈话", "今天是新工作第一天"],
            "work": ["准备交方案", "在排查故障", "任务卡住了", "等同事反馈", "正在处理突发任务"],
            "learning": ["今天第一次练口语", "今天做模拟题", "正在学难章节", "今天试新背词方法", "准备读完一本书"],
            "creation": ["选题卡住了", "准备拍摄", "正在剪辑", "今天发布作品", "收到作品反馈"],
            "relationship": ["和朋友有了争执", "今天准备跟家人谈一谈", "今天有约会", "今天探望家人", "挂心朋友的事"],
            "travel": ["正在赶火车", "今天长途开车", "航班延误了", "刚到陌生城市", "今天搬家"],
            "errand": ["正在等维修", "申请退款了", "今天办理证件", "我在找丢失的东西", "今天试用新设备"],
            "joy": ["收到表扬了", "突破了个人最好成绩", "买到了心仪的东西", "准备庆祝", "期待周末的活动"],
        }
        count = 0
        for category, samples in cases.items():
            for text in samples:
                count += 1
                with self.subTest(category=category, message=text):
                    with self.s.db:
                        self.s.db.execute("DELETE FROM followups")
                    self.ingest(text, f"scenario-{count}")
                    self.assertEqual(len(self.rows()), 1)
                    self.assertEqual(self.rows()[0]["category"], category)
        self.assertEqual(count, 60)
        self.assertFalse((self.root / "01_日常记录").exists())

    def test_unknown_future_event_time_requests_clarification(self):
        r = self.ingest("我约了一个球局")
        self.assertEqual(r["followups"][0]["status"], "needs_time")
        self.assertFalse(self.rows())

    def test_meal_reply_cannot_resolve_health_when_two_topics_were_asked(self):
        self.ingest("可能感冒了")
        self.advance("2026-09-30T13:15:00+08:00"); self.s.tick(self.sender)
        self.advance("2026-09-30T13:20:00+08:00")
        self.ingest("午饭吃好了", "meal")
        self.assertEqual(self.rows()[0]["status"], "asked")

    def test_known_driving_duration_delays_followup_until_after_driving(self):
        self.ingest("正在长途开车，预计要开八个小时")
        self.assertEqual(self.rows()[0]["due_at"], "2026-09-30T17:30:00+08:00")

    def test_concern_for_friend_is_relationship_not_users_health(self):
        self.ingest("我有点担心朋友今天身体不舒服")
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["category"], "relationship")

    def test_successful_method_is_followed_after_time_to_try_it(self):
        self.ingest("今天试的背词方法挺有效")
        self.assertEqual(self.rows()[0]["due_at"], "2026-10-03T09:00:00+08:00")

    def test_preview_and_restart_do_not_consume_or_resolve_followup(self):
        self.ingest("我胃不舒服")
        self.advance("2026-09-30T13:15:00+08:00")
        one = self.s.decide()
        self.assertEqual(self.s.decide(), one)
        self.assertEqual(self.rows()[0]["status"], "pending")
        self.assertEqual(len(self.sender.calls), 0)

    def test_short_event_reference_is_restored_locally_and_never_sent_to_wording_model(self):
        self.ingest("我胃不舒服")
        self.advance("2026-09-30T13:15:00+08:00")
        decision = self.s.decide()
        (self.root / "SOUL.md").write_text("## 语气\n亲切简短", encoding="utf-8")
        # Fetch the real implementation even while the base fixture patches it.
        import importlib
        real = importlib.reload(wording).render
        raw = json.dumps({"ids": [0, 1], "text": "之前说的__EVENT_0__，现在怎么样？午饭吃得怎么样？"}, ensure_ascii=False)
        with patch.object(wording, "load_config", return_value={}), patch.object(wording.model, "complete", return_value=raw) as call:
            rendered = real(self.root, decision)
        self.assertIn("胃", rendered)
        self.assertNotIn("「", rendered)
        self.assertNotIn("__EVENT_", rendered)
        self.assertNotIn("胃不舒服", str(call.call_args))
        with patch.object(wording, "load_config", return_value={}), patch.object(wording.model, "complete", return_value=json.dumps({"ids": [0, 1], "text": "那件事现在怎么样？"}, ensure_ascii=False)):
            with self.assertRaisesRegex(ServiceError, "checkin_style_invalid"):
                real(self.root, decision)


if __name__ == "__main__":
    unittest.main()
