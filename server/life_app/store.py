"""Durable state; no credentials or private records in application logs."""
import contextlib
import hashlib
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path

from life_assistant import atomic_write, lock

DIRECTORIES = ("00_收件箱", "01_日常记录", "02_生活领域", "03_目标与项目", "04_知识与资源", "05_复盘与计划", "90_归档", "99_系统与模板")

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.state = self.root / ".local"
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.secrets = self.root / ".secrets"
        self.secrets.mkdir(exist_ok=True, mode=0o700)
        self.path = self.state / "state.sqlite3"
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tokens(hash TEXT PRIMARY KEY, kind TEXT NOT NULL, owner TEXT NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY, owner TEXT NOT NULL, conversation TEXT NOT NULL, body TEXT NOT NULL, reply TEXT, status TEXT NOT NULL, created REAL NOT NULL, archived INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS limits(key TEXT PRIMARY KEY, count INTEGER NOT NULL, until REAL NOT NULL);
            ''')
        if os.name != "nt":
            os.chmod(self.path, 0o600)

    @contextlib.contextmanager
    def db(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get(self, key, default=None):
        with self.db() as db:
            row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def put(self, key, value):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False)))

    def limited(self, key, maximum=8, seconds=300):
        now = time.time()
        with self.db() as db:
            row = db.execute("SELECT count,until FROM limits WHERE key=?", (key,)).fetchone()
            count, until = (row[0] + 1, row[1]) if row and row[1] > now else (1, now + seconds)
            db.execute("INSERT OR REPLACE INTO limits VALUES (?,?,?)", (key, count, until))
            return count > maximum

    def secret(self, name, value=None):
        if name not in {"model", "smtp", "weather", "gateway", "bridge"}:
            raise ValueError("invalid_secret")
        path = self.secrets / name
        if value is not None:
            if not value.strip() or len(value) > 8192 or "\x00" in value:
                raise ValueError("invalid_secret")
            atomic_write(path, value)
            if os.name != "nt":
                path.chmod(0o600)
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def bootstrap(self):
        if self.get("admin"):
            raise ValueError("already_initialized")
        token = secrets.token_urlsafe(32)
        self.put("bootstrap", {"hash": digest(token), "expires": time.time() + 1800})
        return token

    def initialize(self):
        templates = {"SOUL.md": "# 生活助手\n\n## 语气\n自然、准确、简洁。区分用户事实、建议与待确认信息，不冒充真人。\n",
                     "总览.md": "# 总览\n\n## 当前重点\n尚未设置。\n",
                     "02_生活领域/个人资料.md": "# 个人资料\n\n## 简报可用\n尚未填写。只填写愿意交给模型处理的信息。\n",
                     "03_目标与项目/计划.md": "# 计划\n\n## 安排\n| 日期 | 安排 | 状态 |\n| --- | --- | --- |\n",
                     "99_系统与模板/知识笔记.md": "# 标题\n\n## 内容\n\n## 来源与待核验信息\n"}
        for directory in DIRECTORIES:
            (self.root / directory).mkdir(exist_ok=True)
        with lock(self.root, "initialize"):
            for name, text in templates.items():
                path = self.root / name
                if not path.exists():
                    atomic_write(path, text)
        for key in ("gateway", "bridge"):
            if not self.secret(key):
                self.secret(key, secrets.token_urlsafe(32))

    def note(self, relative):
        if not relative or "\\" in relative or "\x00" in relative:
            raise ValueError("invalid_note_path")
        rel = Path(relative)
        if rel.is_absolute() or any(p.startswith(".") or ":" in p for p in rel.parts):
            raise ValueError("invalid_note_path")
        if rel.suffix != ".md" or (rel.parts[0] not in DIRECTORIES and relative not in {"SOUL.md", "总览.md"}):
            raise ValueError("invalid_note_path")
        path = self.root / rel
        current = self.root
        for part in rel.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("symlink_refused")
        if not path.resolve().is_relative_to(self.root):
            raise ValueError("invalid_note_path")
        return path

    def notes(self):
        result = []
        for base in (*DIRECTORIES, "SOUL.md", "总览.md"):
            path = self.root / base
            candidates = path.rglob("*.md") if path.is_dir() else [path]
            for item in candidates:
                relative = item.relative_to(self.root).as_posix()
                try:
                    if self.note(relative).is_file():
                        result.append(relative)
                except ValueError:
                    continue
        return sorted(result)[:2000]
