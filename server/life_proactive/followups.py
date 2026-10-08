"""Evidence-backed event follow-ups. Local queue; no model calls or transport."""
import datetime as dt
import hashlib
import json
import re

from life_service.core import ServiceError


# category: label, default delay hours, priority, matching expressions, question
SCENARIOS = {
    "health": ("身体不适", 4, 90, r"感冒|头疼|头痛|胃不舒服|胃疼|落枕|牙疼|喉咙痛|嗓子疼|肩膀.*(?:疼|不舒服)|髋.*(?:疼|不舒服)|身体不舒服",
               "现在身体感觉怎么样？"),
    "sleep": ("睡眠精力", 4, 65, r"没睡好|失眠|熬夜|很困|有点困|困得厉害|有点累|午睡|早点睡",
              "后来有休息一下吗，现在精神怎么样？"),
    "exercise": ("运动恢复", 4, 75, r"球局|打球|训练课|新动作|运动后不舒服|打完.*(?:疼|不舒服)|参加比赛|羽毛球课",
                 "后来怎么样，身体感受和原先预想的一样吗？"),
    "mood": ("情绪困扰", 4, 80, r"有点烦|很烦|委屈|失落|孤单|孤独|压力(?:很大|大)|难过",
             "现在感觉怎么样，还想聊聊这件事吗？"),
    "event": ("重要事件", 4, 85, r"面试|考试|工作汇报|重要谈话|新工作第一天|入职第一天|述职",
              "后来进展怎么样，你自己感觉如何？"),
    "work": ("工作推进", 4, 60, r"交方案|排查故障|排查.*bug|任务卡住|等同事反馈|突发任务|卡住的.*问题|修.*bug",
             "后来有进展了吗，有没有需要一起处理的地方？"),
    "learning": ("学习尝试", 6, 50, r"练口语|练习口语|模拟题|难章节|背词方法|读完一本书|学英语",
                 "后来试得怎么样，哪个地方最有收获或最卡？"),
    "creation": ("创作发布", 8, 60, r"选题卡住|准备拍摄|正在剪辑|发布作品|发了.*小红书|发.*小红书|作品反馈|收到.*评论",
                 "后来怎么样，有没有新的进展或反馈？"),
    "relationship": ("人际家庭", 6, 65, r"和朋友.*争执|跟朋友.*吵架|跟家人谈|跟家里聊|约会|探望家人|挂心朋友|担心朋友",
                     "后来怎么样，你现在想聊聊吗？"),
    "travel": ("出行变化", 6, 70, r"赶火车|长途开车|航班延误|陌生城市|搬家|坐飞机|出差",
               "后来路上和安顿得还顺利吗？方便时再回就好。"),
    "errand": ("生活事务", 24, 45, r"等维修|申请退款|办理证件|办证|丢.*东西|东西丢了|试用新设备|来修|退款申请",
               "后来处理得怎么样，还有卡住的地方吗？"),
    "joy": ("开心期待", 12, 55, r"收到表扬|被表扬|最好成绩|心仪的东西|准备庆祝|期待.*周末|期待.*活动|找到感觉了",
            "后来有没有什么开心的细节想接着分享？"),
}
UNTRUSTED = re.compile(r"假如|如果|例如|比如|举例|测试|虚构|转发|引用|假设|示例|不要记录|别记下来|不记日记|只聊不记|不要保存|不要.*写.*日记")
HISTORICAL = re.compile(r"去年|上个月|上周|前几天|以前|曾经")
THIRD_PERSON = re.compile(r"(?:朋友|同事|家人|爸(?:爸)?|妈(?:妈)?|哥哥|姐姐|他|她)(?:说|可能|好像|今天|昨天|有点|也|又|很|在|要|的|身体|一直|最近|早上|下午|晚上){0,4}(?:感冒|头疼|头痛|胃|落枕|牙疼|没睡好|失眠|面试|考试|不舒服|喉咙痛|嗓子疼|肩膀|髋|难过|孤单|压力)")
VAGUE = re.compile(r"以后|有空|哪天|有机会|最近想|想学|考虑|可能会")
CANCEL = re.compile(r"别问了|不用(?:再)?问|不要(?:再)?问|别再跟进|停止跟进|不用跟进|取消了|不去了")
WAIT = re.compile(r"等我(?:有结果|消息|主动|说)|先别问|暂时别问")
RESOLVED = re.compile(r"好了|已经解决|解决了|不疼了|不痛了|没事了|找到了|结束了|完成了|已经到家|已经到了")
OPEN = re.compile(r"还没|没好|没解决|仍然|还是|依然|还在|还疼|还痛|好多了|好一点|好些了|缓解")
UNCERTAIN = re.compile(r"希望|但愿|应该|可能|也许|大概|如果|明天.*好|等.*好")
POSTPONE = re.compile(r"改到|推迟到|延期到|改成|晚点问|明天再问|后天再问")
FUTURE = re.compile(r"今天|今日|明天|明日|后天|周[一二三四五六日天]|星期[一二三四五六日天]|\d{1,2}月\d{1,2}日|\d{4}-\d{2}-\d{2}|下午|晚上|上午|中午|今晚|\d+天后")


def iso(value):
    return value.isoformat(timespec="seconds")


def stamp(value):
    result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone required")
    return result


def safe_source(text):
    return "语音转写（待用户确认）" not in text and not UNTRUSTED.search(text) and not (HISTORICAL.search(text) and not re.search(r"现在|仍然|还在|还是", text))


def chinese_number(value):
    if value.isdigit():
        return int(value)
    digits = "零一二三四五六七八九"
    if value == "两":
        return 2
    if "十" in value:
        a, b = value.split("十")
        return (digits.index(a) if a else 1) * 10 + (digits.index(b) if b else 0)
    return digits.index(value)


def suggested_due(text, at, category):
    """Conservative local timing. A time is an earliest follow-up, not an appointment."""
    delay = SCENARIOS[category][1]
    if category in ("learning", "work", "creation", "exercise") and re.search(r"有效|有用|顺手|有帮助", text):
        delay = 72
    base = at + dt.timedelta(hours=delay)
    if category == "travel" and re.search(r"开车|驾驶", text) and (duration := re.search(r"([一二两三四五六七八九十\d]{1,2})(?:个)?小时", text)):
        return at + dt.timedelta(hours=chinese_number(duration[1]) + 0.5)
    requested = bool(re.search(r"再问|问我|回问|跟进", text))
    if category in ("health", "mood") and not requested and not re.search(r"明天|明日|后天|改到|推迟", text):
        return base
    if category == "sleep" and re.search(r"早点睡|熬夜", text) and not requested:
        return dt.datetime.combine(at.date() + dt.timedelta(days=1), dt.time(10), at.tzinfo)
    if delay == 72 and not requested and not re.search(r"明天|后天|周[一二三四五六日天]", text):
        return base
    target = at.date()
    dated = False
    if m := re.search(r"(\d{4}-\d{2}-\d{2})", text):
        target, dated = dt.date.fromisoformat(m[1]), True
    elif m := re.search(r"(\d{1,2})月(\d{1,2})[日号]", text):
        target, dated = dt.date(at.year, int(m[1]), int(m[2])), True
        if target < at.date():
            target = target.replace(year=target.year + 1)
    elif "后天" in text:
        target, dated = target + dt.timedelta(days=2), True
    elif re.search(r"明天|明日", text):
        target, dated = target + dt.timedelta(days=1), True
    elif m := re.search(r"(下|本|这)?(?:周|星期)([一二三四五六日天])", text):
        weekday = "一二三四五六日".index(m[2].replace("天", "日"))
        delta = weekday - at.weekday()
        if m[1] == "下":
            delta += 7
        elif delta < 0:
            delta += 7
        target, dated = target + dt.timedelta(days=delta), True
    elif m := re.search(r"(\d{1,2})天后", text):
        target, dated = target + dt.timedelta(days=int(m[1])), True
    elif re.search(r"今天|今日|今晚", text):
        dated = True
    # Use the last clock in a range, then leave an event buffer. Never assume an
    # event has finished merely because this estimated time has passed.
    clocks = list(re.finditer(r"(?:(上午|早上|中午|下午|晚上|晚间))?\s*([零一二两三四五六七八九十\d]{1,3})(?:[:：](\d{2})|点(半|[零一二三四五六七八九十\d]{1,3}分?)?)", text))
    if clocks:
        m = clocks[-1]
        hour = chinese_number(m[2])
        minute = int(m[3]) if m[3] else 30 if m[4] == "半" else chinese_number(m[4].rstrip("分")) if m[4] else 0
        period = m[1] or ("下午" if re.search(r"下午|晚上|今晚", text[:m.start()]) else "")
        if period in ("下午", "晚上", "晚间") and hour < 12:
            hour += 12
        anchor = dt.datetime.combine(target, dt.time(hour, minute), at.tzinfo)
        # An explicit request to ask at this time needs no event-duration guess.
        buffer = 0 if requested else 0.5 if re.search(r"结束|到家|到达|抵达", text) else 3 if category in ("exercise", "travel") else 2
        return max(at + dt.timedelta(minutes=30), anchor + dt.timedelta(hours=buffer))
    if dated or re.search(r"下午|晚上|今晚|上午|中午", text):
        hour = (20 if requested else 22) if re.search(r"晚上|今晚", text) else (15 if requested else 18) if "下午" in text else 14 if re.search(r"上午|早上|中午", text) else 20
        return max(at + dt.timedelta(minutes=30), dt.datetime.combine(target, dt.time(hour), at.tzinfo))
    if category == "sleep" and re.search(r"早点睡|熬夜", text):
        return dt.datetime.combine(at.date() + dt.timedelta(days=1), dt.time(10), at.tzinfo)
    return base


class Followups:
    def __init__(self, store):
        self.s = store
        self.db = store.db
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS followups (
          id TEXT PRIMARY KEY, session TEXT NOT NULL, category TEXT NOT NULL,
          subject TEXT NOT NULL, quote TEXT NOT NULL, message_id TEXT NOT NULL,
          source_at TEXT NOT NULL, due_at TEXT NOT NULL, expires_at TEXT NOT NULL,
          status TEXT NOT NULL, priority INTEGER NOT NULL, updated_at TEXT NOT NULL,
          revision INTEGER NOT NULL DEFAULT 1, last_asked_at TEXT);
        CREATE INDEX IF NOT EXISTS followups_due ON followups(status,due_at);
        CREATE TABLE IF NOT EXISTS followup_actions (
          message_id TEXT NOT NULL, followup_id TEXT NOT NULL, action TEXT NOT NULL,
          result TEXT NOT NULL, at TEXT NOT NULL,
          PRIMARY KEY(message_id,followup_id,action));
        """)

    def enabled(self):
        return (self.s.c["mode"] == "companion" and self.s.c.get("followups", {}).get("enabled", False)
                and not self.s.get("followups_disabled", False))

    def context(self, session):
        now = iso(self.s.now())
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM followups WHERE session=? AND expires_at>? AND status IN ('pending','asked','waiting') ORDER BY updated_at DESC LIMIT 24", (session, now))]

    def source(self, mid, session, quote):
        row = self.db.execute("SELECT * FROM messages WHERE id=? AND session=?", (mid, session)).fetchone()
        if (not row or not row["text"] or not isinstance(quote, str) or not 1 <= len(quote) <= 400
                or quote not in row["text"] or not safe_source(row["text"])
                or stamp(row["at"]) < self.s.now() - dt.timedelta(hours=12)):
            raise ServiceError("followup_source_unavailable_or_not_grounded")
        return row

    def manage(self, mid, session, action, quote, category=None, subject=None, followup_id=None, due_at=None):
        if not self.enabled():
            raise ServiceError("followups_disabled")
        source = self.source(mid, session, quote)
        if action not in ("upsert", "reschedule", "resolve", "cancel", "wait"):
            raise ServiceError("invalid_followup_action")
        old = None
        if followup_id:
            old = self.db.execute("SELECT * FROM followups WHERE id=? AND session=?", (followup_id, session)).fetchone()
            if not old:
                raise ServiceError("followup_not_in_session")
        if action != "upsert" and not old:
            raise ServiceError("followup_target_required")
        if action in ("upsert", "reschedule"):
            category = category or (old["category"] if old else None)
            subject = subject or (old["subject"] if old else None)
            if category not in SCENARIOS or not isinstance(subject, str) or not 1 <= len(subject) <= 80:
                raise ServiceError("invalid_followup_subject")
            if not old and (subject not in quote or re.search(r"[?？\n]", subject) or (category != "relationship" and THIRD_PERSON.search(quote))):
                raise ServiceError("followup_subject_not_grounded")
            if old and (category != old["category"] or subject != old["subject"]):
                raise ServiceError("followup_subject_is_immutable")
            if not old and VAGUE.search(quote) and not FUTURE.search(quote):
                raise ServiceError("followup_time_unclear")
        if not old:
            # Repeated mentions of the same subject update one open thread.
            old = self.db.execute("SELECT * FROM followups WHERE session=? AND category=? AND subject=? AND status IN ('pending','asked','waiting') ORDER BY updated_at DESC LIMIT 1", (session, category, subject)).fetchone()
        fid = old["id"] if old else hashlib.sha256((session + "\0" + mid + "\0" + category + "\0" + subject).encode()).hexdigest()[:24]
        previous = self.db.execute("SELECT result FROM followup_actions WHERE message_id=? AND followup_id=? AND action=?", (mid, fid, action)).fetchone()
        if previous:
            return json.loads(previous[0])
        if old and stamp(source["at"]) < stamp(old["source_at"]):
            raise ServiceError("followup_stale_update")
        now = self.s.now()
        if action in ("upsert", "reschedule"):
            if old and old["status"] in ("cancelled", "resolved", "expired"):
                raise ServiceError("followup_already_closed")
            # A single source must not create a second, model-worded duplicate.
            same = self.db.execute("SELECT id FROM followups WHERE message_id=? AND category=? AND session=?", (mid, category, session)).fetchone()
            if not old and same:
                return {"status": "already_scheduled", "followup_id": same[0]}
            try:
                inferred = suggested_due(quote, stamp(source["at"]), category)
                due = stamp(due_at).astimezone(now.tzinfo) if due_at else inferred
                if due_at and FUTURE.search(quote) and due < inferred:
                    raise ValueError("before source event")
                if not now + dt.timedelta(minutes=15) <= due <= now + dt.timedelta(days=30):
                    raise ValueError("outside horizon")
            except (ValueError, TypeError):
                raise ServiceError("invalid_followup_due_time") from None
            expiry = due + dt.timedelta(days=2 if category in ("health", "sleep", "mood", "travel") else 7)
            if not old and self.db.execute("SELECT COUNT(*) FROM followups WHERE status IN ('pending','asked','waiting') AND expires_at>?", (iso(now),)).fetchone()[0] >= 24:
                raise ServiceError("followup_queue_full")
            if old:
                self.db.execute("UPDATE followups SET quote=?,message_id=?,source_at=?,due_at=?,expires_at=?,status='pending',updated_at=?,revision=revision+1 WHERE id=?",
                                (quote, mid, source["at"], iso(due), iso(expiry), iso(now), fid))
            else:
                self.db.execute("INSERT INTO followups(id,session,category,subject,quote,message_id,source_at,due_at,expires_at,status,priority,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?)",
                                (fid, session, category, subject, quote, mid, source["at"], iso(due), iso(expiry), SCENARIOS[category][2], iso(now)))
            result = {"status": "rescheduled" if old else "scheduled", "followup_id": fid, "due_at": iso(due), "expires_at": iso(expiry), "delivery": "next_eligible_checkin_window"}
        else:
            status = {"resolve": "resolved", "cancel": "cancelled", "wait": "waiting"}[action]
            if action == "resolve" and (not RESOLVED.search(quote) or OPEN.search(quote) or UNCERTAIN.search(quote) or re.search(r"[?？]", quote)):
                raise ServiceError("followup_resolution_not_confirmed")
            if action == "cancel" and not CANCEL.search(quote):
                raise ServiceError("followup_cancellation_not_confirmed")
            if action == "wait" and not WAIT.search(quote):
                raise ServiceError("followup_wait_not_confirmed")
            self.db.execute("UPDATE followups SET status=?,message_id=?,source_at=?,quote=?,updated_at=?,revision=revision+1 WHERE id=?", (status, mid, source["at"], quote, iso(now), fid))
            result = {"status": status, "followup_id": fid}
        self.db.execute("INSERT INTO followup_actions VALUES(?,?,?,?,?)", (mid, fid, action, json.dumps(result, ensure_ascii=False), iso(now)))
        return result

    def _target(self, text, session):
        rows = self.context(session)
        matches = [r for r in rows if r["subject"] in text]
        if len(matches) == 1:
            return matches[0]
        categories = {k for k, spec in SCENARIOS.items() if re.search(spec[3], text)}
        matches = [r for r in rows if r["category"] in categories]
        if len(matches) == 1:
            return matches[0]
        # An unqualified reply refers only to a recent delivered follow-up,
        # never an arbitrary last database row or another session.
        latest = self.db.execute("SELECT topics,at FROM deliveries WHERE status='sent' ORDER BY at DESC LIMIT 1").fetchone()
        if latest and stamp(latest["at"]) >= self.s.now() - dt.timedelta(hours=12):
            topics = json.loads(latest["topics"])
            ids = [t.split(":")[1] for t in topics if t.startswith("followup:")] if len(topics) == 1 else []
            matches = [r for r in rows if r["id"] in ids]
            if len(matches) == 1:
                return matches[0]
        if len(rows) == 1 and re.search(r"这件事|那个事|这事", text):
            return rows[0]
        return None

    def observe(self, mid, session):
        if not self.enabled():
            return []
        row = self.db.execute("SELECT * FROM messages WHERE id=? AND session=?", (mid, session)).fetchone()
        text = row["text"]
        if not text or not safe_source(text) or stamp(row["at"]) < self.s.now() - dt.timedelta(minutes=10):
            return []
        results = []
        for clause in re.split(r"[。；;\n，,]", text):
            clause = clause.strip()
            if not clause or len(clause) > 400:
                continue
            action = "wait" if WAIT.search(clause) else "cancel" if CANCEL.search(clause) else "reschedule" if POSTPONE.search(clause) else "resolve" if RESOLVED.search(clause) and not OPEN.search(clause) and not UNCERTAIN.search(clause) else None
            target = self._target(clause, session)
            if action:
                if target:
                    try:
                        results.append(self.manage(mid, session, action, clause, followup_id=target["id"]))
                    except ServiceError as e:
                        results.append({"status": "needs_model_review", "reason": e.code})
                else:
                    results.append({"status": "needs_target", "action": action})
                continue
            if re.search(r"[?？]|是不是|会不会|(?:吗|么)[呀呢啊吧]?$|(?:没|没有|并不|不是)(?:感冒|头疼|胃疼|不舒服)|(?:不|没|没有)(?:去|想|打算)(?:打球|面试|考试)", clause):
                continue
            if VAGUE.search(clause) and not FUTURE.search(clause):
                continue
            categories = [k for k, spec in SCENARIOS.items() if re.search(spec[3], clause)]
            if THIRD_PERSON.search(clause):
                categories = ["relationship"] if "relationship" in categories else []
            if categories and categories[0] in ("event", "exercise", "travel", "relationship") and re.search(r"约了|准备|打算|将要|要去|下次|即将", clause) and not FUTURE.search(text):
                results.append({"status": "needs_time", "category": categories[0]})
                continue
            if not categories and target and OPEN.search(clause):
                categories = [target["category"]]
            # One event per clause; more detailed extraction can use the model tool.
            for category in categories[:1]:
                matching = [r for r in self.context(session) if r["category"] == category and
                            (r["subject"] == clause[:80] or (category in ("health", "sleep", "mood") and
                             any(m.group() in clause for m in re.finditer(SCENARIOS[category][3], r["subject"]))))]
                existing = target if target and target["category"] == category and OPEN.search(clause) else matching[0] if len(matching) == 1 else None
                # Preserve the actual event time supplied in a nearby clause.
                quote = text if len(text) <= 400 and len(categories) == 1 and not re.search(r"[。；;\n]", text) else clause
                try:
                    results.append(self.manage(mid, session, "upsert", quote, category=category,
                                               subject=existing["subject"] if existing else clause[:80],
                                               followup_id=existing["id"] if existing else None))
                except ServiceError as e:
                    results.append({"status": "needs_model_review", "reason": e.code})
        return results

    def ready(self, now):
        if not self.enabled():
            return None
        day_start = iso(now.replace(hour=0, minute=0, second=0))
        for row in self.db.execute("SELECT * FROM followups WHERE status='pending' AND due_at<=? AND expires_at>? AND (last_asked_at IS NULL OR last_asked_at<?) ORDER BY priority DESC,due_at LIMIT 24", (iso(now), iso(now), day_start)):
            # All outcomes consume a revision, including ambiguous/failed sends.
            topic = f"followup:{row['id']}:{row['revision']}"
            if any(topic in json.loads(d[0]) for d in self.db.execute("SELECT topics FROM deliveries")):
                continue
            return dict(row)
        return None

    def question(self, row):
        return f"{self.reference(row)}，{SCENARIOS[row['category']][4]}"

    @staticmethod
    def reference(row):
        # Local extraction, never send private quotes for paraphrasing. Relative
        # dates belong to due_at; don't repeat tomorrow as if it were still true.
        text = row['subject']
        anchors = [
            (r'胃(?:不舒服|疼)', '胃'), (r'肩膀.*(?:疼|不舒服)', '肩膀'),
            (r'面试|考试|工作汇报|述职|球局|训练课|模拟题|背词方法|练口语|排查故障|渲染问题|选题|拍摄|剪辑|作品反馈|退款|维修|办理证件|搬家|航班', None),
        ]
        for pattern, label in anchors:
            if match := re.search(pattern, text):
                return label or match[0]
        # Retain uncertainty, e.g. 可能感冒, removing only opening deixis.
        text = re.sub(r'^(?:(?:我|今天|今日|昨天|昨日|明天|明日|昨晚|今晚|早上|上午|下午|晚上|刚才|现在)\s*)+', '', text)
        if not text or len(text) > 24 or POSTPONE.search(text) or re.search(r'下周|周[一二三四五六日天]|\d{1,2}月|\d{1,2}点', text):
            return '之前聊的那件事'
        return text.rstrip('。！!')

    def consume(self, topics, at):
        for topic in topics:
            if topic.startswith("followup:"):
                _, fid, revision = topic.split(":")
                self.db.execute("UPDATE followups SET status='asked',last_asked_at=? WHERE id=? AND revision=?", (at, fid, int(revision)))

    def cleanup(self, now):
        self.db.execute("UPDATE followups SET status='expired' WHERE expires_at<=? AND status IN ('pending','asked','waiting')", (iso(now),))
        cutoff = iso(now - dt.timedelta(days=7))
        self.db.execute("DELETE FROM followups WHERE expires_at<?", (cutoff,))
        self.db.execute("DELETE FROM followup_actions WHERE at<? AND followup_id NOT IN (SELECT id FROM followups)", (cutoff,))
