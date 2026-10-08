"""Deterministic scheduling/evidence; isolated wording model runs before delivery."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import os
import shutil
import sqlite3
import tempfile
from zoneinfo import ZoneInfo

from life_assistant import journal, lock
from life_service.core import inside, read_json, write_json, ServiceError
from .followups import Followups
from .onboarding import Profile

MEALS = {"breakfast": "早餐", "lunch": "午餐", "dinner": "晚餐"}
ALIASES = {"早餐": "breakfast", "早饭": "breakfast", "午餐": "lunch", "午饭": "lunch", "晚餐": "dinner", "晚饭": "dinner"}
UNSAFE_FACT = re.compile(r"[?？]|假如|如果|打算|准备吃|计划吃|想吃|要吃|吃什么|怎么吃|推荐|能不能|建议|要不要|应该|例如|比如|测试|虚构|转发")
NO_SAVE = re.compile(r"不要记录|别记下来|不记日记|只聊不记|不要保存|不要.*写.*日记")
SLOTS = ("opening", "morning", "midday", "afternoon", "evening", "night")
FACT_NAMES = {"tasks": ("progress",), "share": ("reflection",), "plan": ("today", "tomorrow"),
              "mood": ("feeling",), "state": ("energy",)}
NOT_PERSONAL = re.compile(r"[?？]|假如|如果|例如|比如|测试|虚构|转发")


def fact_topic(kind, name):
    return kind + ":" + name if kind in ("meal", "plan") else kind


def iso(t):
    return t.isoformat(timespec="seconds")


def stamp(value):
    t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise ServiceError("timezone_required")
    return t


def load_config(path):
    c = read_json(path)
    try:
        c["root"] = (path.resolve().parent / c["workspace"]).resolve()
        c["tz"] = ZoneInfo(c["timezone"])
        assert c["version"] == 1 and c["root"].is_dir()
        assert type(c["enabled"]) is bool
        assert c["account_id"] and c["owner_id"]
        assert re.fullmatch(r"[A-Za-z0-9_-]+", c["account_id"])
        assert type(c["daily_cap"]) is int and 1 <= c["daily_cap"] <= 6
        assert type(c["weekly_cap"]) is int and 1 <= c["weekly_cap"] <= 42
        assert 15 <= c["conversation_cooldown_minutes"] <= 120
        c.setdefault("mode", "gentle")  # Existing installations retain their chosen behaviour.
        assert c["mode"] in ("gentle", "companion")
        c.setdefault("followups", {"enabled": False})
        assert isinstance(c["followups"], dict) and type(c["followups"].get("enabled")) is bool
        assert {"midday", "evening"} <= set(c["windows"]) <= set(SLOTS)
        if c["mode"] == "companion":
            assert set(c["windows"]) == set(SLOTS)
        else:
            assert set(c["windows"]) <= {"morning", "midday", "evening"}
        slots = [s for s in SLOTS if s in c["windows"]]
        for slot in slots:
            start, end = c["windows"][slot]
            assert re.fullmatch(r"\d{2}:\d{2}", start) and re.fullmatch(r"\d{2}:\d{2}", end)
            a, b = dt.time.fromisoformat(start), dt.time.fromisoformat(end)
            lower, upper = (dt.time(8), dt.time(12)) if slot in ("opening", "morning") else (dt.time(12), dt.time(23) if c["mode"] == "companion" else dt.time(22))
            assert lower <= a < b <= upper
        for first, second in zip(slots, slots[1:]):
            assert c["windows"][first][1] < c["windows"][second][0]
        assert set(c.get("sharing_weekdays", [])) <= set(range(7))
        for spec in c.get("plans", []):
            inside(c["root"], spec["path"])
            assert dt.date.fromisoformat(spec["start"]) <= dt.date.fromisoformat(spec["end"])
        inside(c["root"], c["openclaw_state"])
        if not c.get("managed"):
            inside(c["root"], c["weixin_package"])
    except (KeyError, ValueError, TypeError, AssertionError):
        raise ServiceError("invalid_proactive_config") from None
    c["config_path"] = path.resolve()
    if c.get("managed"):
        from life_app.store import Store as AppStore
        from life_app.memory import preferences
        pref = preferences(AppStore(c["root"]))
        c["enabled"] = pref.cloud_processing and pref.proactive_contact and not pref.paused and pref.daily_cap > 0 and pref.weekly_cap > 0
        c["memory_allowed"] = pref.cloud_processing and pref.automatic_memory and not pref.paused
        c["followups"]["enabled"] = c["enabled"] and c["memory_allowed"]
        c["daily_cap"], c["weekly_cap"] = pref.daily_cap, pref.weekly_cap
        c["tz"] = ZoneInfo(pref.timezone)
        c["quiet"] = [pref.quiet_start, pref.quiet_end]
    return c


class Store:
    def __init__(self, c, clock=None):
        self.c = c
        self.root = c["root"]
        self.clock = clock or (lambda: dt.datetime.now(c["tz"]))
        folder = inside(self.root, ".local/proactive")
        folder.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(folder / "state.sqlite", timeout=20)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS messages (
          id TEXT PRIMARY KEY, native_id TEXT NOT NULL, session TEXT NOT NULL,
          at TEXT NOT NULL, text TEXT NOT NULL, media TEXT NOT NULL, receipt TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS facts (
          id INTEGER PRIMARY KEY, message_id TEXT NOT NULL REFERENCES messages(id),
          day TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL, state TEXT NOT NULL,
          quote TEXT NOT NULL, journal_path TEXT, UNIQUE(message_id,day,kind,name));
        CREATE TABLE IF NOT EXISTS deliveries (
          id TEXT PRIMARY KEY, day TEXT NOT NULL, slot TEXT NOT NULL, topics TEXT NOT NULL,
          body TEXT NOT NULL, status TEXT NOT NULL, at TEXT NOT NULL,
          server_id TEXT, answered INTEGER NOT NULL DEFAULT 0, error TEXT);
        """)
        self.followups = Followups(self)
        self.profile = Profile(self)

    def close(self):
        self.db.close()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False)))

    def now(self):
        return self.clock().astimezone(self.c["tz"])

    def controls(self, text):
        now = self.now()
        text = text.strip().rstrip("。！!")
        result = None
        if text in ("关闭后续关心", "关闭事件跟进"):
            self.put("followups_disabled", True)
            result = "followups_disabled"
        elif text in ("开启后续关心", "恢复后续关心", "开启事件跟进"):
            self.put("followups_disabled", False)
            result = "followups_enabled"
        elif text in ("今天别问了", "跳过今天", "今天不提醒"):
            self.put("pause_until", iso(now.replace(hour=0, minute=0, second=0) + dt.timedelta(days=1)))
            result = "paused_today"
        elif text in ("暂停提醒", "停止提醒", "关闭主动询问"):
            self.put("paused", True)
            result = "paused"
        elif text in ("恢复提醒", "开启主动询问"):
            self.put("paused", False)
            self.put("pause_until", None)
            self.put("backoff_reset_at", iso(now))
            result = "resumed"
        elif text in ("只在晚上问", "每天一次"):
            self.put("evening_only", True)
            self.put("cadence_windows", ["evening"])
            result = "evening_only"
        elif text in ("恢复每天两次", "每天两次"):
            self.put("evening_only", False)
            self.put("morning_disabled", True)
            self.put("cadence_windows", ["midday", "evening"])
            result = "two_windows"
        elif text in ("恢复每天三次", "每天三次") or (text == "恢复默认频率" and self.c["mode"] == "gentle"):
            self.put("evening_only", False)
            self.put("morning_disabled", False)
            self.put("cadence_windows", ["morning", "midday", "evening"])
            result = "three_windows" if "morning" in self.c["windows"] else "two_windows"
        elif self.c["mode"] == "companion" and text in ("每天六次", "恢复每天六次", "恢复默认频率"):
            self.put("evening_only", False)
            self.put("morning_disabled", False)
            self.put("cadence_windows", list(SLOTS))
            result = "six_windows"
        elif self.c["mode"] == "companion" and (m := re.fullmatch(r"(?:恢复)?每天(?:最多|上限)?\s*([一二三四五六1-6])\s*次", text)):
            count = int(m[1]) if m[1].isdigit() else "一二三四五六".index(m[1]) + 1
            choices = {1: ["evening"], 2: ["midday", "evening"], 3: ["morning", "midday", "evening"],
                       4: ["opening", "midday", "evening", "night"],
                       5: ["opening", "morning", "midday", "evening", "night"], 6: list(SLOTS)}
            self.put("evening_only", count == 1)
            self.put("morning_disabled", count == 2)
            self.put("cadence_windows", choices[count])
            result = f"{count}_windows"
        elif m := re.fullmatch(r"暂停(?:提醒)?\s*(\d{1,2})\s*天", text):
            days = int(m[1])
            if 1 <= days <= 30:
                self.put("pause_until", iso(now + dt.timedelta(days=days)))
                result = "paused_until"
        return result

    def ingest(self, event):
        """Trusted gateway envelope only; never obtain identity from model arguments."""
        c = self.c
        if (event.get("channel") != "openclaw-weixin" or event.get("account") != c["account_id"]
                or event.get("sender") != c["owner_id"] or event.get("is_group", False)):
            return {"status": "ignored_identity"}
        native, session = event.get("message_id"), event.get("session")
        if not isinstance(native, str) or not native or not isinstance(session, str) or not session:
            return {"status": "missing_native_identity"}
        text = event.get("text", "")
        if not isinstance(text, str) or len(text) > 16000:
            return {"status": "invalid_message"}
        now = self.now()
        at = stamp(event["at"]).astimezone(c["tz"])
        if at > now + dt.timedelta(minutes=5) or at < now - dt.timedelta(days=7):
            return {"status": "stale_message"}
        mid = hashlib.sha256((c["account_id"] + "\0" + c["owner_id"] + "\0" + native).encode()).hexdigest()
        previous = self.db.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        if previous:
            self.flush_journals()
            return json.loads(previous["receipt"])
        media = []
        for m in event.get("media", [])[:8]:
            if isinstance(m, dict) and isinstance(m.get("path"), str):
                try:
                    p = inside(self.root, m["path"])
                    if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".heic"):
                        media.append({"path": p.relative_to(self.root).as_posix(), "kind": "image"})
                except ServiceError:
                    pass
        # A short explicit meal label can confirm the immediately preceding photo.
        # Neither the clock nor an arbitrary attachment is sufficient on its own.
        if not media and re.fullmatch(r"(?:这是)?(?:今天的?|今日的?)?(?:早餐|早饭|午餐|午饭|晚餐|晚饭)[。！!]?", text.strip()):
            previous_image = self.db.execute("SELECT * FROM messages WHERE session=? ORDER BY at DESC LIMIT 1", (session,)).fetchone()
            if previous_image and json.loads(previous_image["media"]) and at - dt.timedelta(minutes=10) <= stamp(previous_image["at"]) <= at:
                if not self.db.execute("SELECT 1 FROM facts WHERE message_id=?", (previous_image["id"],)).fetchone():
                    media = json.loads(previous_image["media"])
        self.put("last_activity", max(iso(at), self.get("last_activity", iso(at))))
        control = (self.profile.control(text, 'wechat:' + mid) or self.controls(text)) if at >= now - dt.timedelta(minutes=10) else None
        receipt = {"status": control or "received", "message_id": mid, "date": at.date().isoformat()}
        # Opt-out messages never enter the evidence cache or journal.
        discard = bool(NO_SAVE.search(text)) or bool(control)
        self.db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?)", (mid, native, session, iso(at), "" if discard else text,
                        json.dumps([] if discard else media), json.dumps(receipt)))
        followup_results = self.followups.observe(mid, session) if not discard else []
        if followup_results:
            receipt["followups"] = followup_results
            self.db.execute("UPDATE messages SET receipt=? WHERE id=?", (json.dumps(receipt, ensure_ascii=False), mid))
        # A real reply ends a no-response streak, but is not assumed to answer any particular topic.
        self.put("backoff_reset_at", max(iso(at), self.get("backoff_reset_at", iso(at))))
        self.db.commit()
        if not discard:
            explicit_journal = bool(c.get("managed") and re.match(r"^(?:记一下[\s:：]*|日记\s*[:：])", text))
            items = [] if explicit_journal else self.parse(text, at.date(), bool(media))
            if items:
                receipt = self.record(mid, session, items)
            elif text.strip() in ("跳过", "不想聊", "今天不想聊"):
                self.db.execute("UPDATE deliveries SET answered=1 WHERE day=? AND status='sent'", (at.date().isoformat(),))
                self.db.commit()
                receipt["status"] = "skipped_answer"
            if followup_results:
                receipt["followups"] = followup_results
            self.db.execute("UPDATE messages SET receipt=? WHERE id=?", (json.dumps(receipt, ensure_ascii=False), mid))
            self.db.commit()
        return receipt

    def manage_followup(self, data):
        with lock(self.root, "proactive-send"), self.db:
            return self.followups.manage(**data)

    @staticmethod
    def parse(text, day, image=False):
        items = []
        if "语音转写（待用户确认）" in text:
            return items
        text = re.sub(r"^(?:记一下|日记)[：:]\s*", "", text.strip())
        if image and (label := re.fullmatch(r"(?:这是)?(?:今天的?|今日的?)?(早餐|早饭|午餐|午饭|晚餐|晚饭)[。！!]?", text.strip())):
            return [{"day": day.isoformat(), "kind": "meal", "name": ALIASES[label[1]], "state": "photo", "quote": text}]
        for line in re.split(r"[\n；;]", text):
            line = line.strip()
            m = re.fullmatch(r"(今天|今日|昨天|昨日)?(早餐|早饭|午餐|午饭|晚餐|晚饭)(?:[：:]\s*|\s*(?=吃了|没吃|未吃|不吃|还没吃|还没来得及吃|只喝|喝了))(.+)", line)
            if m and not UNSAFE_FACT.search(line):
                details = m[3].strip()
                if details in ("吃了什么", "怎么吃", "吃什么"):
                    continue
                state = "deferred" if details.startswith(("还没", "还未")) else "skipped" if details in ("没吃", "未吃", "不吃", "没吃。") else "photo" if image else "reported"
                target = day - dt.timedelta(days=m[1] in ("昨天", "昨日"))
                items.append({"day": target.isoformat(), "kind": "meal", "name": ALIASES[m[2]], "state": state, "quote": line})
            elif line.startswith(("任务：", "任务:", "进度：", "进度:")) and not UNSAFE_FACT.search(line):
                if re.search(r"完成|做了|没做|做完|取消|改到|推迟|进行|一点|分钟", line):
                    items.append({"day": day.isoformat(), "kind": "tasks", "name": "progress", "state": "reported", "quote": line})
            elif line.startswith(("分享：", "分享:")) and len(line) > 3 and not re.search(r"虚构|测试|例如", line):
                items.append({"day": day.isoformat(), "kind": "share", "name": "reflection", "state": "reported", "quote": line})
            elif m := re.fullmatch(r"(今日安排|今天安排|日程|明日安排|明天安排|心情|状态|精神状态)[：:]\s*(.+)", line):
                if NOT_PERSONAL.search(line) or not m[2].strip():
                    continue
                kind, name = {"今日安排": ("plan", "today"), "今天安排": ("plan", "today"), "日程": ("plan", "today"),
                              "明日安排": ("plan", "tomorrow"), "明天安排": ("plan", "tomorrow"),
                              "心情": ("mood", "feeling"), "状态": ("state", "energy"), "精神状态": ("state", "energy")}[m[1]]
                items.append({"day": day.isoformat(), "kind": kind, "name": name,
                              "state": "planned" if kind == "plan" else "reported", "quote": line})
        return items

    def record(self, mid, session, items):
        row = self.db.execute("SELECT * FROM messages WHERE id=? AND session=?", (mid, session)).fetchone()
        if not row or not row["text"] or NO_SAVE.search(row["text"]):
            raise ServiceError("source_unavailable_or_opted_out")
        if "语音转写（待用户确认）" in row["text"]:
            raise ServiceError("transcript_requires_user_confirmation")
        if self.c.get("managed") and re.match(r"^(?:记一下[\s:：]*|日记\s*[:：])", row["text"]):
            raise ServiceError("explicit_journal_owned_by_bridge")
        if stamp(row["at"]) < self.now() - dt.timedelta(days=1):
            raise ServiceError("source_too_old")
        if not isinstance(items, list) or not 1 <= len(items) <= 10:
            raise ServiceError("invalid_facts")
        validated = []
        for item in items:
            try:
                day = dt.date.fromisoformat(item["day"])
                assert self.now().date() - dt.timedelta(days=7) <= day <= self.now().date()
                assert item["kind"] in ("meal", *FACT_NAMES)
                assert isinstance(item["quote"], str) and item["quote"].strip() and item["quote"] in row["text"]
                guard = UNSAFE_FACT if item["kind"] in ("meal", "tasks") else NOT_PERSONAL
                assert not guard.search(item["quote"]) or item["kind"] == "share"
                if item["kind"] == "meal":
                    assert item["name"] in MEALS and item["state"] in ("reported", "photo", "skipped", "deferred")
                    if item["state"] == "photo":
                        assert json.loads(row["media"])
                else:
                    assert item["name"] in FACT_NAMES[item["kind"]]
                    assert item["state"] == ("planned" if item["kind"] == "plan" else "reported")
                old = self.db.execute("SELECT * FROM facts WHERE message_id=? AND day=? AND kind=? AND name=?",
                                      (mid, item["day"], item["kind"], item["name"])).fetchone()
                if old and (old["state"] != item["state"] or old["quote"] != item["quote"]):
                    raise ServiceError("fact_conflict_use_new_message")
                validated.append(item)
            except (KeyError, ValueError, TypeError, AssertionError):
                raise ServiceError("fact_not_grounded") from None
        with self.db:
            for item in validated:
                self.db.execute("INSERT OR IGNORE INTO facts(message_id,day,kind,name,state,quote) VALUES(?,?,?,?,?,?)",
                                (mid, item["day"], item["kind"], item["name"], item["state"], item["quote"]))
                topic = fact_topic(item["kind"], item["name"])
                for delivery in self.db.execute("SELECT * FROM deliveries WHERE day=? AND status='sent'", (item["day"],)).fetchall():
                    if topic in json.loads(delivery["topics"]):
                        self.db.execute("UPDATE deliveries SET answered=1 WHERE id=?", (delivery["id"],))
        self.flush_journals()
        paths = [r[0] for r in self.db.execute("SELECT DISTINCT journal_path FROM facts WHERE message_id=?", (mid,))]
        receipt = {"status": "recorded", "message_id": mid, "paths": paths, "facts": validated,
                   "note": "仅记录用户自述；任务反馈不等于全部完成，未自动勾选原计划。"}
        with self.db:
            self.db.execute("UPDATE messages SET receipt=? WHERE id=?", (json.dumps(receipt, ensure_ascii=False), mid))
        return receipt

    def flush_journals(self):
        """Durable outbox: retrying after a crash uses the canonical journal idempotency key."""
        rows = self.db.execute("SELECT DISTINCT message_id,day FROM facts WHERE journal_path IS NULL").fetchall()
        for entry in rows:
            m = self.db.execute("SELECT * FROM messages WHERE id=?", (entry["message_id"],)).fetchone()
            body = m["text"]
            context = {"消息时间": m["at"], "消息编号": m["native_id"], "记录机制": "主动询问 / 用户明确反馈"}
            refs = json.loads(m["media"])
            if refs:
                context["原始附件"] = "；".join(x["path"] for x in refs)
                archived = []
                for ref in refs:
                    source = inside(self.root, ref["path"])
                    if not source.is_file():
                        context["附件限制"] = "至少一个原始附件当前不可读；不声称已归档该附件"
                        continue
                    with source.open("rb") as f:
                        fingerprint = hashlib.file_digest(f, "sha256").hexdigest()
                    target = inside(self.root, f"01_日常记录/{entry['day'][:4]}/附件/{entry['day']}/{fingerprint}{source.suffix.lower()}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if not target.exists():
                        fd, temporary = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
                        try:
                            with os.fdopen(fd, "wb") as out, source.open("rb") as inp:
                                shutil.copyfileobj(inp, out)
                                out.flush(); os.fsync(out.fileno())
                            with open(temporary,"rb") as check:
                                if hashlib.file_digest(check,"sha256").hexdigest() != fingerprint:
                                    raise ServiceError("attachment_changed_during_archive")
                            os.replace(temporary,target)
                        finally:
                            if os.path.exists(temporary):
                                os.unlink(temporary)
                    archived.append(target.relative_to(self.root).as_posix())
                if archived:
                    context["归档附件"] = "；".join(archived)
            result = journal(self.root, entry["day"], "wechat", "proactive:" + entry["message_id"] + ":" + entry["day"], body, timezone=self.c["tz"], context=context)
            rel = Path(result["path"]).relative_to(self.root).as_posix()
            with self.db:
                self.db.execute("UPDATE facts SET journal_path=? WHERE message_id=? AND day=?", (rel, entry["message_id"], entry["day"]))

    def plan(self, day):
        for spec in self.c.get("plans", []):
            if not spec["start"] <= day.isoformat() <= spec["end"]:
                continue
            path = inside(self.root, spec["path"])
            if not path.exists():
                continue
            section = False
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                if line.startswith("## "):
                    section = line[3:].strip() == spec["section"]
                if section and re.match(rf"\|\s*(?:{day.isoformat()}|{day.month}/{day.day})(?:\s|\|)", line):
                    return {"path": spec["path"], "row": line[:1800]}
        return None

    def pending(self, session):
        now = self.now()
        messages = self.db.execute("SELECT id,at,text,media,receipt FROM messages WHERE session=? ORDER BY at DESC LIMIT 4", (session,)).fetchall()
        # Facts and prompts are evidence only, never executable instructions.
        deliveries = self.db.execute("SELECT id,body,topics,status FROM deliveries WHERE day=? AND status='sent'", (now.date().isoformat(),)).fetchall()
        facts = self.db.execute("SELECT f.day,f.kind,f.name,f.state,f.quote,m.at FROM facts f JOIN messages m ON m.id=f.message_id WHERE f.day=? AND f.journal_path IS NOT NULL ORDER BY m.at DESC LIMIT 40", (now.date().isoformat(),)).fetchall()
        return {"messages": [dict(r) for r in messages if stamp(r["at"]) >= now - dt.timedelta(hours=12)],
                "prompts": [dict(r) for r in deliveries], "facts_today": [dict(r) for r in facts],
                "plan": self.plan(now.date()), "mode": self.c["mode"],
                "followups_enabled": self.followups.enabled(), "followups": self.followups.context(session),
                "profile": self.profile.context(),
                "local_now": iso(now),
                "yesterday_tomorrow_plan": self.previous_plan(now.date()),
                "cadence_windows": self.get("cadence_windows", list(self.c["windows"])), "daily_cap": self.c["daily_cap"]}

    def previous_plan(self, day):
        previous = (day - dt.timedelta(days=1)).isoformat()
        return [dict(r) for r in self.db.execute(
            "SELECT f.quote,f.state,m.at FROM facts f JOIN messages m ON m.id=f.message_id WHERE f.day=? AND f.kind='plan' AND f.name='tomorrow' AND f.journal_path IS NOT NULL ORDER BY m.at DESC LIMIT 5", (previous,))]

    def journal_topics(self, day):
        """Read only canonical user-quotation blocks; never mine AI advice or templates."""
        path = inside(self.root, f"01_日常记录/{day.year}/{day.isoformat()}.md")
        if not path.exists():
            return set()
        topics = set()
        for block in re.split(r"(?m)^## 随手记录 · ", path.read_text(encoding="utf-8-sig"))[1:]:
            if f"- 发生日期：{day.isoformat()}" not in block:
                continue
            match = re.search(r"(?m)^### 用户原文\s*\n(.*?)(?=^#{1,3} |\Z)", block, re.S | re.M)
            if not match:
                continue
            raw = "\n".join(line[2:] for line in match[1].splitlines() if line.startswith("> "))
            for item in self.parse(raw, day):
                if item["day"] == day.isoformat():
                    topics.add(fact_topic(item["kind"], item["name"]))
        return topics

    def companion_questions(self, slot, known, asked, day):
        """Daily coverage, independent of reply streaks; no invented personal context."""
        variant = day.toordinal() % 2
        meal = {"morning": "breakfast", "midday": "lunch", "evening": "dinner"}.get(slot)
        if meal:
            options = {
                "breakfast": ("早餐吃上了吗？给我看看，或者说说吃了什么、大概多少，我帮你记下来。还没吃也告诉我一声。",
                              "来关心一下你的早饭。吃了什么呀，大概多少？拍张照或随口说说都行，没吃就照实说。"),
                "lunch": ("午饭吃得怎么样？吃了什么、大概多少，拍给我或说一句吧，我帮你把这顿记住。",
                          "到午饭这会儿了，今天吃了什么呀？照片或文字都好，分量也顺便告诉我。"),
                "dinner": ("晚饭吃上了吗？今天这顿吃了什么、大概多少，跟我说说或者拍给我，我帮你记。",
                           "来看看你的晚饭。吃了什么呀，大概多少？忙得还没吃也可以直接说。")}
            candidates = [("meal:" + meal, options[meal][variant])]
        elif slot == "opening":
            plan_question = ("你昨天说的今天安排，有没有变化？哪一两件最想先顾好？" if self.previous_plan(day)
                             else "今天计划里已经列了些安排，有没有想调整的？哪一两件最想先顾好？" if self.plan(day)
                             else "今天准备怎么过呀？有没有确定的安排，或一两件想让我帮你惦记的事？")
            candidates = [("plan:today", plan_question), ("state", "昨晚睡得怎么样，现在精神还好吗？累、困或者哪里不舒服，都可以跟我说。")]
        elif slot == "afternoon":
            candidates = [("tasks", "下午过得怎么样，手上的事情推进到哪儿了？有卡住的地方就丢给我，我们一起拆一小步。"),
                          ("mood", "也想问问你今天的心情，开心、烦闷，还是有点说不清？不用整理好了才讲。")]
        else:
            candidates = [("share", "今天有没有什么好玩的、特别的，或者一直挂在心上的事？我想听听你这一天，开心的和不太好过的都可以。"),
                          ("plan:tomorrow", "明天有什么需要提前记住或准备的吗？你说，我帮你整理。")]
        remaining = [(topic, question) for topic, question in candidates if topic not in known | asked]
        return [topic for topic, _ in remaining], [question for _, question in remaining]

    def decide(self, now=None):
        now = (now or self.now()).astimezone(self.c["tz"])
        day = now.date().isoformat()
        base = {"day": day, "status": "suppressed"}
        if not self.c["enabled"]:
            return dict(base, reason="disabled")
        if self.get("paused", False) or (self.get("pause_until") and now < stamp(self.get("pause_until"))):
            return dict(base, reason="paused")
        minute = now.strftime("%H:%M")
        quiet = self.c.get("quiet")
        if quiet:
            a, b = quiet
            if (a <= minute < b if a < b else minute >= a or minute < b):
                return dict(base, reason="quiet_time")
        slot = next((s for s, (a, b) in self.c["windows"].items() if a <= minute < b), None)
        if not slot:
            return dict(base, reason="outside_window")
        base["slot"] = slot
        did = f"{day}:{slot}"
        if self.db.execute("SELECT 1 FROM deliveries WHERE id=?", (did,)).fetchone():
            return dict(base, reason="window_consumed")
        sent = self.db.execute("SELECT * FROM deliveries WHERE status IN ('sent','sending','unknown') ORDER BY at DESC").fetchall()
        reset = self.get("backoff_reset_at", "")
        streak = 0
        for r in sent:
            if r["at"] <= reset or r["answered"]:
                break
            streak += 1
        companion = self.c["mode"] == "companion"
        if not companion and streak >= 4:
            return dict(base, reason="waiting_for_user_after_four_unanswered")
        if slot != "evening" and (self.get("evening_only", False) or (not companion and streak >= 2)):
            return dict(base, reason="evening_only")
        if slot == "morning" and self.get("morning_disabled", False):
            return dict(base, reason="two_windows")
        if self.get("cadence_windows") is not None and slot not in self.get("cadence_windows"):
            return dict(base, reason="selected_frequency")
        if len([r for r in sent if r["day"] == day]) >= self.c["daily_cap"]:
            return dict(base, reason="daily_cap")
        week_start = (now.date() - dt.timedelta(days=6)).isoformat()
        if len([r for r in sent if week_start <= r["day"] <= day]) >= self.c["weekly_cap"]:
            return dict(base, reason="rolling_week_cap")
        if self.get("last_activity") and now - stamp(self.get("last_activity")) < dt.timedelta(minutes=self.c["conversation_cooldown_minutes"]):
            return dict(base, reason="recent_conversation")
        known = self.journal_topics(now.date())
        for r in self.db.execute("SELECT kind,name FROM facts WHERE day=? AND journal_path IS NOT NULL", (day,)):
            known.add(fact_topic(r["kind"], r["name"]))
        asked = set()
        for r in self.db.execute("SELECT topics FROM deliveries WHERE day=? AND status IN ('sent','sending','unknown')", (day,)):
            asked.update(json.loads(r[0]))
        discovery = self.profile.ready(now, slot) if (not self.c.get("managed") or self.c.get("memory_allowed")) else None
        if discovery and not self.followups.ready(now):
            return {"status": "ready", "id": did, "day": day, "slot": slot,
                    "topics": ['onboarding:' + discovery['field']], "body": discovery['question'],
                    "style_variant": len(sent) % 4}
        if companion:
            topics, questions = self.companion_questions(slot, known, asked, now.date())
            followup = self.followups.ready(now)
            if followup:
                # Keep meal questions; use one specific event in place of a
                # generic related topic. Never exceed two subjects per round.
                related = {"health": "state", "sleep": "state", "exercise": "state",
                           "mood": "mood", "relationship": "mood", "event": "tasks",
                           "work": "tasks", "learning": "tasks", "creation": "tasks",
                           "travel": "share", "errand": "tasks", "joy": "share"}[followup["category"]]
                pairs = [(t, q) for t, q in zip(topics, questions) if t != related]
                topics = [f"followup:{followup['id']}:{followup['revision']}"] + [p[0] for p in pairs[:1]]
                questions = [self.followups.question(followup)] + [p[1] for p in pairs[:1]]
            if not questions:
                return dict(base, reason="nothing_missing")
            decision = {"status": "ready", "id": did, "day": day, "slot": slot, "topics": topics, "body": "\n".join(questions)}
            decision["style_variant"] = len(sent) % 4
            if followup:
                decision["followup_category"] = followup["category"]
                decision["followup_references"] = {"__EVENT_0__": self.followups.reference(followup)}
            return decision
        meals = {"morning": ("breakfast",), "midday": ("breakfast", "lunch"),
                 "evening": ("breakfast", "lunch", "dinner")}[slot]
        missing = [m for m in meals if "meal:" + m not in known | asked]
        topics = ["meal:" + m for m in missing]
        questions = []
        if missing:
            names = "、".join(MEALS[m] for m in missing)
            questions.append(f"{names}还没有记录，愿意补一句吃了什么、大概多少吗？文字或照片都行，没吃也可以直说。")
        if slot == "evening":
            share = now.weekday() in self.c["sharing_weekdays"]
            if share and "share" not in known | asked:
                topics.append("share")
                questions.append("今天有没有一件有趣、特别，或者让你挂心的事想聊聊？没有也没关系。")
            elif self.plan(now.date()) and "tasks" not in known | asked:
                topics.append("tasks")
                questions.append("今天计划里的事情推进得怎样？做完、做了一点、没做，或临时改了安排，都可以简单说。")
        if not questions:
            return dict(base, reason="nothing_missing")
        body = {"morning": "早餐简单补个记录。\n", "midday": "午后轻轻补个记录。\n",
                "evening": "今天简单收个尾。\n"}[slot] + "\n".join(questions)
        body += "\n可只答一项或回复「跳过」。回答会留在本地日记；只想聊天可说「只聊不记」。"
        return {"status": "ready", "id": did, "day": day, "slot": slot, "topics": topics, "body": body}

    def tick(self, sender):
        with lock(self.root, "proactive-send"):
            self.flush_journals()
            # A crash after handing off to the transport is ambiguous, never retried.
            with self.db:
                self.db.execute("UPDATE deliveries SET status='unknown',error='interrupted_send' WHERE status='sending'")
                self.followups.cleanup(self.now())
                cutoff = iso(self.now() - dt.timedelta(days=7))
                self.db.execute("DELETE FROM messages WHERE at<? AND id NOT IN (SELECT message_id FROM facts)", (cutoff,))
            decision = self.decide()
            if decision["status"] != "ready":
                return decision
            retry_key = "wording:" + decision["day"] + ":" + decision["slot"]
            retry = self.get(retry_key, {"attempts": 0})
            def defer_wording():
                bug = self.root / ".local/proactive/bugs" / (hashlib.sha256(retry_key.encode()).hexdigest()[:24] + ".json")
                if not bug.exists():
                    write_json(bug, {"status": "deferred", "reason": "retries_exhausted",
                                    "day": decision["day"], "slot": decision["slot"],
                                    "attempts": retry["attempts"], "error": retry.get("error", "wording_interrupted"),
                                    "at": iso(self.now())})
                return {"status": "suppressed", "reason": "wording_retries_exhausted", "attempts": retry["attempts"]}
            if retry["attempts"] >= 4:
                return defer_wording()
            if retry.get("next_attempt_at") and self.now() < stamp(retry["next_attempt_at"]):
                return {"status": "suppressed", "reason": "wording_retry_wait"}
            if sender.check() != "ready":
                return {"status": "suppressed", "reason": "wechat_not_ready"}
            from .wording import render
            soul = (self.root / "SOUL.md")
            retry["attempts"] += 1
            retry["next_attempt_at"] = iso(self.now() + dt.timedelta(seconds=60 if retry["attempts"] == 1 else 300))
            with self.db:
                self.put(retry_key, retry)  # A crash also consumes the attempt.
            try:
                soul_before = soul.read_bytes()
                body = render(self.root, decision)
            except Exception as exc:
                retry["error"] = exc.code if isinstance(exc, ServiceError) else "wording_unavailable"
                with self.db:
                    self.put(retry_key, retry)
                if retry["attempts"] >= 4:
                    return defer_wording()
                return {"status": "suppressed", "reason": "wording_failed", "error": retry["error"], "attempts": retry["attempts"]}
            # Model latency must not bypass a pause, slot expiry, reply or changed SOUL.
            fresh = self.decide()
            if fresh["status"] != "ready":
                return fresh
            if fresh["id"] != decision["id"] or fresh["topics"] != decision["topics"] or soul.read_bytes() != soul_before:
                return {"status": "suppressed", "reason": "wording_context_changed"}
            decision["body"] = body
            with self.db:
                self.put(retry_key, {"attempts": 0})
                self.db.execute("INSERT INTO deliveries(id,day,slot,topics,body,status,at) VALUES(?,?,?,?,?,'sending',?)",
                                (decision["id"], decision["day"], decision["slot"], json.dumps(decision["topics"]), decision["body"], iso(self.now())))
                self.followups.consume(decision["topics"], iso(self.now()))
            try:
                receipt = sender.send(decision["id"], decision["body"])
                status = receipt.get("status", "unknown")
                if status not in ("sent", "failed", "unknown"):
                    status = "unknown"
            except Exception:
                receipt, status = {"error": "transport_outcome_unknown"}, "unknown"
            with self.db:
                self.db.execute("UPDATE deliveries SET status=?,server_id=?,error=? WHERE id=?",
                                (status, receipt.get("message_id"), receipt.get("error"), decision["id"]))
            return {"status": status, "id": decision["id"], "error": receipt.get("error")}

    def connection_test(self, sender):
        """One explicitly requested deployment test, audited and counted in the caps."""
        with lock(self.root, "proactive-send"):
            key = "connection-test-v1"
            old = self.db.execute("SELECT status FROM deliveries WHERE id=?", (key,)).fetchone()
            if old:
                return {"status": "already_attempted", "previous": old[0]}
            if not dt.time(12) <= self.now().time().replace(tzinfo=None) < dt.time(22):
                return {"status": "suppressed", "reason": "quiet_time"}
            if sender.check() != "ready":
                return {"status": "suppressed", "reason": "wechat_not_ready"}
            body = "微信主动询问连接测试。这条只验证发送，不需要回复，也不会记入日记。"
            with self.db:
                self.db.execute("INSERT INTO deliveries(id,day,slot,topics,body,status,at,answered) VALUES(?,?,?,'[]',?,'sending',?,1)",
                                (key,self.now().date().isoformat(),"connection_test",body,iso(self.now())))
            try:
                receipt = sender.send(key, body)
                status = receipt.get("status", "unknown")
                if status not in ("sent", "failed", "unknown"):
                    status = "unknown"
            except Exception:
                status,receipt="unknown",{"error":"transport_outcome_unknown"}
            with self.db:
                self.db.execute("UPDATE deliveries SET status=?,server_id=?,error=? WHERE id=?",
                                (status,receipt.get("message_id"),receipt.get("error"),key))
            return {"status":status,"id":key,"error":receipt.get("error")}

    def status(self):
        return {"enabled": self.c["enabled"], "decision": self.decide(),
                "mode": self.c["mode"], "daily_cap": self.c["daily_cap"], "weekly_cap": self.c["weekly_cap"],
                "windows": self.c["windows"],
                "followups_enabled": self.followups.enabled(),
                "followup_counts": {r[0]: r[1] for r in self.db.execute("SELECT status,COUNT(*) FROM followups GROUP BY status")},
                "preferences": {k: self.get(k) for k in ("paused", "pause_until", "evening_only", "morning_disabled", "cadence_windows", "last_activity")},
                "facts": self.db.execute("SELECT COUNT(*) FROM facts").fetchone()[0],
                "deliveries": [dict(r) for r in self.db.execute("SELECT id,status,at,error,answered FROM deliveries ORDER BY at DESC LIMIT 10")]}
