import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from life_proactive.core import Store, load_config, iso, ServiceError


class Sender:
    def __init__(self, status="sent"):
        self.calls = []
        self.status = status
    def check(self):
        return "ready"
    def send(self, key, body):
        self.calls.append((key, body))
        if self.status == "timeout":
            raise TimeoutError()
        return {"status": self.status, "message_id": "mock-provider-id"}


class ProactiveFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.at = dt.datetime.fromisoformat("2026-09-30T13:15:00+08:00")
        config = json.loads((Path(__file__).resolve().parents[2] / "server/life_proactive/config.example.json").read_text())
        config.update(workspace=".", account_id="fictional-account", owner_id="fictional-owner", enabled=True)
        config.update(mode="gentle", daily_cap=3, weekly_cap=21, sharing_weekdays=[1, 3, 6],
                      windows={"morning": ["10:30", "11:00"], "midday": ["13:15", "13:45"], "evening": ["20:45", "21:00"]})
        self.path = self.root / "config.json"
        self.path.write_text(json.dumps(config), encoding="utf-8")
        self.c = load_config(self.path)
        self.s = Store(self.c, lambda: self.at)
        self.addCleanup(self.s.close)
        self.sender = Sender()
        (self.root / 'SOUL.md').write_text('## 语气\n虚构测试风格', encoding='utf-8')
        wording_patch = patch('life_proactive.wording.render', return_value='后来怎么样呀？跟我聊聊~')
        self.wording = wording_patch.start()
        self.addCleanup(wording_patch.stop)

    def event(self, text, mid="m1", **extra):
        return {"channel":"openclaw-weixin", "account":"fictional-account", "sender":"fictional-owner",
                "message_id":mid, "session":"owner-session", "at":iso(self.at), "text":text, **extra}

    def ingest(self, text, mid="m1", **extra):
        return self.s.ingest(self.event(text, mid, **extra))

    def advance(self, value):
        self.at = dt.datetime.fromisoformat(value)

    def plan(self):
        (self.root / "plan.md").write_text("# Fictional\n## 每日安排\n| 日期 | 内容 |\n| 9/30 周三 | 虚构的英语安排，待预约 |\n", encoding="utf-8")
        self.c["plans"]=[{"path":"plan.md","start":"2026-09-25","end":"2026-10-07","section":"每日安排"}]


class ProactiveTest(ProactiveFixture, unittest.TestCase):
    def test_managed_explicit_journal_has_only_one_writer(self):
        self.c["managed"] = True
        receipt = self.ingest("记一下：早餐：吃了面包")
        self.assertEqual(receipt["status"], "received")
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM facts").fetchone()[0], 0)
        with self.assertRaisesRegex(ServiceError, "explicit_journal_owned_by_bridge"):
            self.s.record(receipt["message_id"], "owner-session", [{"day": "2026-09-30", "kind": "meal", "name": "breakfast", "state": "reported", "quote": "早餐：吃了面包"}])
        ordinary = self.ingest("午餐：吃了米饭", "second")
        self.assertEqual(ordinary["status"], "recorded")

    def test_midday_two_missing_meals_one_message(self):
        d = self.s.decide()
        self.assertEqual(d["topics"], ["meal:breakfast","meal:lunch"])
        self.assertEqual(self.s.tick(self.sender)["status"], "sent")
        self.assertEqual(len(self.sender.calls),1)

    def test_three_windows_skip_already_asked_breakfast(self):
        self.advance("2026-09-30T10:30:00+08:00")
        self.assertEqual(self.s.decide()["topics"],["meal:breakfast"])
        self.assertEqual(self.s.tick(self.sender)["status"],"sent")
        self.advance("2026-09-30T13:15:00+08:00")
        self.assertEqual(self.s.decide()["topics"],["meal:lunch"])
        self.assertEqual(self.s.tick(self.sender)["status"],"sent")
        self.advance("2026-09-30T20:45:00+08:00")
        self.assertEqual(self.s.tick(self.sender)["status"],"sent")
        self.assertEqual(len(self.sender.calls),3)

    def test_three_window_controls_and_unanswered_backoff(self):
        self.s.controls("每天两次");self.s.db.commit()
        self.advance("2026-09-30T10:30:00+08:00")
        self.assertEqual(self.s.decide()["reason"],"two_windows")
        self.s.controls("每天三次");self.s.db.commit()
        self.assertEqual(self.s.decide()["status"],"ready")
        self.s.controls("只在晚上问");self.s.db.commit()
        self.assertEqual(self.s.decide()["reason"],"evening_only")
        self.s.controls("恢复默认频率");self.s.db.commit()
        self.s.tick(self.sender)
        self.advance("2026-09-30T13:15:00+08:00");self.s.tick(self.sender)
        self.advance("2026-10-01T10:30:00+08:00")
        self.assertEqual(self.s.decide()["reason"],"evening_only")

    def test_legacy_two_window_config_remains_valid(self):
        raw=json.loads(self.path.read_text())
        raw['windows'].pop('morning')
        raw.update(daily_cap=2,weekly_cap=10)
        self.path.write_text(json.dumps(raw),encoding='utf-8')
        self.assertNotIn('morning',load_config(self.path)['windows'])

    def test_morning_window_validation(self):
        raw=json.loads(self.path.read_text())
        raw['windows']['morning']=['07:30','08:00']
        self.path.write_text(json.dumps(raw),encoding='utf-8')
        with self.assertRaisesRegex(ServiceError,'invalid_proactive_config'):
            load_config(self.path)

    def test_text_counts_and_journal_keeps_original(self):
        self.advance("2026-09-30T12:00:00+08:00")
        self.ingest("早餐：两个鸡蛋；午餐：米饭和青菜")
        self.advance("2026-09-30T13:15:00+08:00")
        self.assertEqual(self.s.decide()["reason"], "nothing_missing")
        journal = (self.root / "01_日常记录/2026/2026-09-30.md").read_text(encoding="utf-8")
        self.assertIn("> 早餐：两个鸡蛋；午餐：米饭和青菜", journal)
        self.assertIn("消息时间：2026-09-30T12:00:00+08:00",journal)

    def test_explicit_skipped_and_deferred_not_unknown(self):
        self.ingest("早餐没吃；午餐还没吃")
        rows=self.s.db.execute("SELECT state FROM facts ORDER BY id").fetchall()
        self.assertEqual([r[0] for r in rows],["skipped","deferred"])

    def test_canonical_journal_other_entry_prevents_repeat(self):
        from life_assistant import journal
        journal(self.root,"2026-09-30","desktop","original-id","早餐：鸡蛋；午餐：米饭")
        self.assertEqual(self.s.decide()["reason"],"nothing_missing")

    def test_ai_journal_advice_is_not_a_meal_record(self):
        p=self.root/"01_日常记录/2026/2026-09-30.md";p.parent.mkdir(parents=True)
        p.write_text("# 2026-09-30\n## AI 建议\n早餐：鸡蛋\n午餐：米饭\n",encoding="utf-8")
        self.assertEqual(self.s.decide()["topics"],["meal:breakfast","meal:lunch"])

    def test_unlabelled_picture_does_not_fake_meal(self):
        p=self.root / "image.jpg";p.write_bytes(b"fake")
        self.ingest("", media=[{"path":str(p)}])
        self.assertEqual(self.s.status()["facts"],0)

    def test_labelled_image_is_recorded_with_separate_provenance(self):
        p=self.root / "image.jpg";p.write_bytes(b"fake")
        self.ingest("午餐：米饭", media=[{"path":str(p)}])
        row=self.s.db.execute("SELECT state FROM facts").fetchone()
        self.assertEqual(row[0],"photo")
        text=(self.root/"01_日常记录/2026/2026-09-30.md").read_text(encoding="utf-8")
        self.assertIn("### 来源元数据（系统记录）",text)
        self.assertNotIn("> image.jpg",text)
        archive=list((self.root/"01_日常记录/2026/附件/2026-09-30").glob("*.jpg"))
        self.assertEqual(len(archive),1)
        self.assertEqual(archive[0].read_bytes(),b"fake")
        self.assertTrue(p.exists())

    def test_two_workers_do_not_send_twice(self):
        import concurrent.futures
        def worker():
            s=Store(self.c,lambda:self.at)
            try:return s.tick(self.sender)
            finally:s.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:worker(),range(2)))
        self.assertEqual(len(self.sender.calls),1)
        self.assertEqual(sum(r["status"]=="sent" for r in results),1)

    def test_picture_outside_workspace_is_not_read(self):
        self.ingest("午餐：米饭",media=[{"path":"../outside.jpg"}])
        self.assertEqual(self.s.db.execute("SELECT state FROM facts").fetchone()[0],"reported")

    def test_following_label_confirms_recent_image(self):
        p=self.root / "image.jpg";p.write_bytes(b"fake")
        self.ingest("",media=[{"path":str(p)}])
        self.advance("2026-09-30T13:17:00+08:00")
        self.ingest("这是今天的午餐","m2")
        row=self.s.db.execute("SELECT name,state FROM facts").fetchone()
        self.assertEqual(tuple(row),("lunch","photo"))

    def test_label_does_not_claim_old_image(self):
        p=self.root / "image.jpg";p.write_bytes(b"fake")
        self.ingest("",media=[{"path":str(p)}])
        self.advance("2026-09-30T14:00:00+08:00")
        self.ingest("午餐","m2")
        self.assertEqual(self.s.status()["facts"],0)

    def test_questions_plans_examples_not_meal_facts(self):
        for n,text in enumerate(["早餐：吃什么好", "午餐：打算吃面", "晚餐：如果吃米饭", "午餐：推荐吃什么？", "早餐：虚构测试两个鸡蛋"]):
            self.ingest(text,str(n))
        self.assertEqual(self.s.status()["facts"],0)

    def test_wrong_owner_group_and_account_ignored(self):
        for extra in ({"sender":"other"},{"account":"other"},{"is_group":True},{"channel":"desktop"}):
            self.assertEqual(self.ingest("早餐：鸡蛋",**extra)["status"],"ignored_identity")
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0],0)

    def test_duplicates_do_not_duplicate_journal(self):
        first=self.ingest("早餐：鸡蛋")
        self.assertEqual(self.ingest("早餐：鸡蛋"),first)
        text=(self.root/"01_日常记录/2026/2026-09-30.md").read_text(encoding="utf-8")
        self.assertEqual(text.count("<!-- entry:"),1)

    def test_cross_session_cannot_record(self):
        receipt=self.ingest("今天中午吃了面")
        with self.assertRaisesRegex(ServiceError,"source_unavailable"):
            self.s.record(receipt["message_id"],"other",[{"day":"2026-09-30","kind":"meal","name":"lunch","state":"reported","quote":"今天中午吃了面"}])

    def test_model_quote_must_be_verbatim(self):
        receipt=self.ingest("今天中午吃了面")
        with self.assertRaisesRegex(ServiceError,"fact_not_grounded"):
            self.s.record(receipt["message_id"],"owner-session",[{"day":"2026-09-30","kind":"meal","name":"lunch","state":"reported","quote":"吃了一碗面和两个鸡蛋"}])

    def test_natural_reply_can_be_semantically_recorded(self):
        receipt=self.ingest("今天中午吃了面")
        result=self.s.record(receipt["message_id"],"owner-session",[{"day":"2026-09-30","kind":"meal","name":"lunch","state":"reported","quote":"今天中午吃了面"}])
        self.assertEqual(result["status"],"recorded")
        self.assertEqual(json.loads(self.s.pending("owner-session")["messages"][0]["receipt"])["status"],"recorded")

    def test_optout_never_stored_as_fact_or_raw_text(self):
        receipt=self.ingest("只聊不记：午餐是米饭")
        self.assertEqual(self.s.db.execute("SELECT text FROM messages").fetchone()[0],"")
        with self.assertRaises(ServiceError):
            self.s.record(receipt["message_id"],"owner-session",[])

    def test_today_pause_survives_reopen_and_expires(self):
        self.ingest("今天别问了")
        s2=Store(self.c,lambda:self.at)
        self.assertEqual(s2.decide()["reason"],"paused");s2.close()
        self.advance("2026-10-01T13:15:00+08:00")
        self.assertEqual(self.s.decide()["status"],"ready")

    def test_pause_resume_and_evening_only(self):
        self.ingest("暂停提醒")
        self.assertEqual(self.s.decide()["reason"],"paused")
        self.ingest("恢复提醒","m2")
        self.ingest("只在晚上问","m3")
        self.assertEqual(self.s.decide()["reason"],"evening_only")

    def test_quiet_time_and_expired_window_no_catchup(self):
        for t in ("08:30","13:45","21:00","23:30"):
            self.advance(f"2026-09-30T{t}:00+08:00")
            self.assertEqual(self.s.tick(self.sender)["reason"],"outside_window")
        self.assertFalse(self.sender.calls)

    def test_recent_chat_defers_then_skips_if_window_ended(self):
        self.ingest("普通讨论")
        self.advance("2026-09-30T13:35:00+08:00")
        self.assertEqual(self.s.decide()["reason"],"recent_conversation")
        self.advance("2026-09-30T13:46:00+08:00")
        self.assertEqual(self.s.decide()["reason"],"outside_window")

    def test_sent_window_is_idempotent_across_restart(self):
        self.s.tick(self.sender)
        s2=Store(self.c,lambda:self.at)
        try:self.assertEqual(s2.tick(self.sender)["reason"],"window_consumed")
        finally:s2.close()
        self.assertEqual(len(self.sender.calls),1)

    def test_unknown_never_retried(self):
        sender=Sender("timeout")
        self.assertEqual(self.s.tick(sender)["status"],"unknown")
        self.assertEqual(self.s.tick(sender)["reason"],"window_consumed")
        self.assertEqual(len(sender.calls),1)

    def test_crash_sending_becomes_unknown(self):
        self.s.db.execute("INSERT INTO deliveries VALUES(?,?,?,?,?,'sending',?,NULL,0,NULL)",
                          ("2026-09-30:midday","2026-09-30","midday",'["meal:lunch"]',"fictional",iso(self.at)))
        self.s.db.commit()
        self.s.tick(self.sender)
        self.assertEqual(self.s.db.execute("SELECT status FROM deliveries").fetchone()[0],"unknown")
        self.assertEqual(len(self.sender.calls),0)

    def test_evening_does_not_repeat_asked_meals(self):
        self.s.tick(self.sender)
        self.plan()
        self.advance("2026-09-30T20:45:00+08:00")
        self.assertEqual(self.s.decide()["topics"],["meal:dinner","tasks"])

    def test_tasks_only_when_current_approved_plan_exists(self):
        self.advance("2026-09-30T20:45:00+08:00")
        self.assertNotIn("tasks",self.s.decide()["topics"])
        self.plan()
        self.assertIn("tasks",self.s.decide()["topics"])
        self.advance("2027-09-30T20:45:00+08:00")
        self.assertIsNone(self.s.plan(self.at.date()))

    def test_sharing_rotates_without_third_topic(self):
        self.plan();self.advance("2026-10-01T20:45:00+08:00")
        d=self.s.decide()
        self.assertIn("share",d["topics"])
        self.assertNotIn("tasks",d["topics"])

    def test_progress_is_not_plan_completion(self):
        self.plan();before=(self.root/"plan.md").read_bytes()
        self.ingest("任务：英语做了一点，没做完")
        self.assertEqual((self.root/"plan.md").read_bytes(),before)
        self.assertEqual(self.s.db.execute("SELECT state FROM facts").fetchone()[0],"reported")

    def test_unanswered_downgrades_then_stops(self):
        self.s.tick(self.sender)
        self.advance("2026-09-30T20:45:00+08:00");self.s.tick(self.sender)
        self.advance("2026-10-01T13:15:00+08:00")
        self.assertEqual(self.s.decide()["reason"],"evening_only")
        self.advance("2026-10-01T20:45:00+08:00");self.s.tick(self.sender)
        self.advance("2026-10-02T20:45:00+08:00");self.s.tick(self.sender)
        self.advance("2026-10-03T20:45:00+08:00")
        self.assertEqual(self.s.decide()["reason"],"waiting_for_user_after_four_unanswered")
        self.ingest("恢复提醒")
        self.advance("2026-10-04T13:15:00+08:00")
        self.assertEqual(self.s.decide()["status"],"ready")

    def test_rolling_week_cap(self):
        self.c["weekly_cap"]=1
        self.s.tick(self.sender)
        self.advance("2026-10-01T20:45:00+08:00")
        self.assertEqual(self.s.decide()["reason"],"rolling_week_cap")

    def test_journal_crash_recovery(self):
        with patch("life_proactive.core.journal",side_effect=OSError("disk")):
            with self.assertRaises(OSError):self.ingest("早餐：鸡蛋")
        self.s.flush_journals();self.s.flush_journals()
        text=(self.root/"01_日常记录/2026/2026-09-30.md").read_text(encoding="utf-8")
        self.assertEqual(text.count("<!-- entry:"),1)

    def test_late_reply_yesterday_does_not_fill_today(self):
        self.ingest("昨天晚餐：面条")
        self.assertEqual(self.s.db.execute("SELECT day FROM facts").fetchone()[0],"2026-09-29")

    def test_transport_preflight_rechecks_window(self):
        sender=Sender()
        def late_check():
            self.advance("2026-09-30T13:46:00+08:00")
            return "ready"
        sender.check=late_check
        self.assertEqual(self.s.tick(sender)["reason"],"outside_window")
        self.assertFalse(sender.calls)

    def test_transport_preflight_rechecks_new_pause(self):
        sender=Sender()
        def pause_check():
            self.ingest("暂停提醒")
            return "ready"
        sender.check=pause_check
        self.assertEqual(self.s.tick(sender)["reason"],"paused")
        self.assertFalse(sender.calls)

    def test_connection_test_is_once_only_and_not_a_journal(self):
        self.assertEqual(self.s.connection_test(self.sender)["status"],"sent")
        self.assertEqual(self.s.connection_test(self.sender)["status"],"already_attempted")
        self.assertEqual(len(self.sender.calls),1)
        self.assertFalse((self.root/"01_日常记录").exists())


if __name__ == '__main__':unittest.main()
