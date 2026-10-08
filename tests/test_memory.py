from pathlib import Path
import tempfile
import time
import unittest
from life_app.store import Store
from life_app.memory import Memories, Preferences


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.mem = Memories(self.store)
        self.store.put("preferences", Preferences(cloud_processing=True, automatic_memory=True).model_dump())
        self.source("one", "虚构：我喜欢红茶")

    def source(self, key, body):
        with self.store.db() as db:
            db.execute("INSERT INTO messages(id,owner,conversation,body,reply,status,created) VALUES (?,?,?,?,?,?,?)",
                       (key, "wechat", "fixture", body, "模型建议喝绿茶", "completed", time.time()))

    def test_sources_and_consent(self):
        for key, quote in (("absent", "红茶"), ("one", "模型建议喝绿茶")):
            with self.assertRaises(ValueError): self.mem.extract(key, quote, "绿茶")
        self.source("private", "只聊不记：喜欢红茶")
        with self.assertRaises(ValueError): self.mem.extract("private", "红茶", "红茶")
        self.store.put("preferences", Preferences().model_dump())
        with self.assertRaises(ValueError): self.mem.extract("one", "红茶", "喜欢红茶")

    def test_correction_replay_and_forget(self):
        item = self.mem.extract("one", "我喜欢红茶", "喜欢红茶")
        corrected = self.mem.correct(item["id"], 1, "喜欢白茶")
        self.assertEqual(corrected["revision"], 2)
        self.assertEqual(self.mem.search("红茶")[0]["text"], "喜欢白茶")
        self.assertEqual(self.mem.search("我喜欢喝什么")[0]["text"], "喜欢白茶")
        self.assertEqual(len(self.mem.search("白茶")), 1)
        self.assertEqual(self.mem.extract("one", "我喜欢红茶", "喜欢红茶")["text"], "喜欢白茶")
        with self.assertRaises(ValueError): self.mem.correct(item["id"], 1, "旧窗口覆盖")
        self.mem.correct(item["id"], 2, "", forget=True)
        self.assertEqual(self.mem.search("白茶"), [])
        self.assertEqual(self.mem.extract("one", "我喜欢红茶", "喜欢红茶")["status"], "forgotten")

    def test_media_confirmation(self):
        item = self.mem.extract("one", "红茶", "可能喜欢红茶", "voice_transcript")
        self.assertEqual(self.mem.search("红茶"), [])
        self.mem.correct(item["id"], 1, "确认喜欢红茶")
        self.assertEqual(len(self.mem.search("红茶")), 1)


if __name__ == "__main__": unittest.main()
