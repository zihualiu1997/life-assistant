"""Source-preserving memories with optimistic revisions and explicit consent."""
import json
import time
from uuid import uuid4
from zoneinfo import ZoneInfo
from pydantic import BaseModel, ConfigDict, Field, field_validator


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cloud_processing: bool = False
    automatic_memory: bool = False
    proactive_contact: bool = False
    paused: bool = False
    display_name: str = Field(default="你", min_length=1, max_length=80, pattern=r"^[^\r\n]+$")
    timezone: str = "Asia/Shanghai"
    morning: str = "08:30"
    evening: str = "21:00"
    quiet_start: str = "23:00"
    quiet_end: str = "10:00"
    daily_cap: int = Field(default=6, ge=0, le=6)
    weekly_cap: int = Field(default=42, ge=0, le=42)

    @field_validator("timezone")
    @classmethod
    def timezone_exists(cls, value):
        try: ZoneInfo(value)
        except Exception: raise ValueError("invalid_timezone") from None
        return value

    @field_validator("quiet_start", "quiet_end", "morning", "evening")
    @classmethod
    def clock(cls, value):
        import re
        if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value): raise ValueError("invalid_time")
        return value


def preferences(store):
    return Preferences(**store.get("preferences", {}))


def no_save(text):
    return any(term in text for term in ("只聊不记", "不要记录", "别记录", "不要保存", "别保存"))


class Memories:
    def __init__(self, store):
        self.store = store
        with store.db() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY, source_id TEXT NOT NULL, quote TEXT NOT NULL,
                source_kind TEXT NOT NULL, source_at REAL NOT NULL,
                text TEXT NOT NULL, revision INTEGER NOT NULL, status TEXT NOT NULL,
                UNIQUE(source_id,quote));
              CREATE TABLE IF NOT EXISTS memory_revisions (
                id TEXT NOT NULL, revision INTEGER NOT NULL, text TEXT NOT NULL,
                status TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY(id,revision));
            ''')

    def extract(self, source_id, quote, text, source_kind="user_text"):
        pref = preferences(self.store)
        if not pref.cloud_processing or not pref.automatic_memory or pref.paused:
            raise ValueError("memory_not_enabled")
        if source_kind not in {"user_text", "voice_transcript", "image_interpretation", "unclassified"}:
            raise ValueError("invalid_source_kind")
        if not text.strip() or len(text) > 4000 or not quote.strip(): raise ValueError("invalid_memory")
        with self.store.db() as db:
            source = db.execute("SELECT body,created FROM messages WHERE id=? AND status='completed'", (source_id,)).fetchone()
            if not source or quote not in source[0] or no_save(source[0]): raise ValueError("unverified_source")
            old = db.execute("SELECT * FROM memories WHERE source_id=? AND quote=?", (source_id, quote)).fetchone()
            # Replay never overwrites a user's correction or reactivates a forgotten item.
            if old: return dict(old)
            key = str(uuid4())
            status = "active" if source_kind == "user_text" else "needs_confirmation"
            db.execute("INSERT INTO memories VALUES (?,?,?,?,?,?,?,?)",
                       (key, source_id, quote, source_kind, source[1], text, 1, status))
            return dict(db.execute("SELECT * FROM memories WHERE id=?", (key,)).fetchone())

    def items(self, active_only=False):
        with self.store.db() as db:
            sql = "SELECT * FROM memories" + (" WHERE status='active'" if active_only else "") + " ORDER BY source_at DESC LIMIT 1000"
            return [dict(row) for row in db.execute(sql)]

    def correct(self, key, revision, text, forget=False):
        if not forget and (not text.strip() or len(text) > 4000): raise ValueError("invalid_memory")
        with self.store.db() as db:
            row = db.execute("SELECT * FROM memories WHERE id=?", (key,)).fetchone()
            if not row or row["revision"] != revision: raise ValueError("memory_changed_reload_first")
            db.execute("INSERT INTO memory_revisions VALUES (?,?,?,?,?)",
                       (key, revision, row["text"], row["status"], time.time()))
            db.execute("UPDATE memories SET text=?,revision=?,status=? WHERE id=?",
                       (text if not forget else row["text"], revision + 1, "forgotten" if forget else "active", key))
            return dict(db.execute("SELECT * FROM memories WHERE id=?", (key,)).fetchone())

    def search(self, query):
        if preferences(self.store).paused: return []
        import re
        terms = set(re.findall(r"[a-z0-9]{2,}|(?=([\u4e00-\u9fff]{2}))", query.lower()))
        terms = {term for term in terms if term} | set(re.findall(r"[a-z0-9]{2,}", query.lower()))
        rows = [(sum((item["text"] + " " + item["quote"]).lower().count(term) for term in terms), item) for item in self.items(True)]
        return [{"source": "memory:" + item["id"], "text": item["text"], "source_kind": item["source_kind"]}
                for score, item in sorted(rows, key=lambda pair: pair[0], reverse=True)[:2] if score]
