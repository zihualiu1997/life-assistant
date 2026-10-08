"""Verified-recipient relay with durable, tenant-scoped delivery uncertainty."""
import hashlib
import json
import re
import secrets
import time
from email.utils import parseaddr


class MailRelay:
    def __init__(self, fleet, send):
        self.fleet, self.send = fleet, send
        with fleet.db() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS recipients (
                tenant TEXT PRIMARY KEY REFERENCES tenants(id), email TEXT NOT NULL,
                verified INTEGER NOT NULL, code_hash TEXT, expires REAL, attempts INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS mail_receipts (
                id TEXT PRIMARY KEY, tenant TEXT NOT NULL REFERENCES tenants(id),
                kind TEXT NOT NULL, day TEXT NOT NULL, content_hash TEXT NOT NULL,
                status TEXT NOT NULL, updated REAL NOT NULL);
            ''')

    def request_verification(self, tenant, email):
        if len(email) > 254 or parseaddr(email)[1] != email or not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", email):
            raise ValueError("invalid_email")
        code = str(secrets.randbelow(1_000_000)).zfill(6)
        with self.fleet.db() as db:
            old = db.execute("SELECT expires FROM recipients WHERE tenant=?", (tenant,)).fetchone()
            if old and old[0] and old[0] > time.time() + 540: raise ValueError("verification_rate_limited")
            db.execute("INSERT OR REPLACE INTO recipients VALUES (?,?,?,?,?,?)",
                       (tenant, email, 0, hashlib.sha256(code.encode()).hexdigest(), time.time() + 600, 0))
        # This is the only delivery permitted to an unverified address, with fixed content.
        self.send(email, "生活助手邮箱验证", "验证码：" + code + "。十分钟内有效。", "verify-" + secrets.token_hex(16))
        return {"status": "smtp_accepted", "verified": False}

    def verify(self, tenant, code):
        accepted = False
        with self.fleet.db() as db:
            row = db.execute("SELECT * FROM recipients WHERE tenant=?", (tenant,)).fetchone()
            if row and row["expires"] and row["expires"] > time.time() and row["attempts"] < 5:
                accepted = secrets.compare_digest(hashlib.sha256(code.encode()).hexdigest(), row["code_hash"] or "")
                db.execute("UPDATE recipients SET attempts=attempts+1,verified=?,code_hash=?,expires=? WHERE tenant=?",
                           (int(accepted), None if accepted else row["code_hash"], 0 if accepted else row["expires"], tenant))
        if not accepted: raise ValueError("invalid_verification_code")
        return {"status": "verified"}

    def deliver(self, tenant, kind, day, subject, body):
        import datetime
        if kind not in {"morning", "evening", "test"} or datetime.date.fromisoformat(day).isoformat() != day:
            raise ValueError("invalid_mail_identity")
        if not body or len(body) > 50000 or not subject or len(subject) > 200 or "\n" in subject or "\r" in subject:
            raise ValueError("invalid_mail_content")
        key = hashlib.sha256((tenant + ":" + kind + ":" + day).encode()).hexdigest()
        content_hash = hashlib.sha256(json.dumps([subject, body]).encode()).hexdigest()
        with self.fleet.db() as db:
            if not db.execute("SELECT 1 FROM tenants WHERE id=? AND status='active'", (tenant,)).fetchone():
                raise ValueError("tenant_not_active")
            recipient = db.execute("SELECT email FROM recipients WHERE tenant=? AND verified=1", (tenant,)).fetchone()
            if not recipient: raise ValueError("recipient_not_verified")
            old = db.execute("SELECT * FROM mail_receipts WHERE id=?", (key,)).fetchone()
            if old:
                if old["content_hash"] != content_hash: raise ValueError("mail_content_conflict")
                return dict(old)  # sending/unknown is never a license to retry.
            db.execute("INSERT INTO mail_receipts VALUES (?,?,?,?,?,?,?)",
                       (key, tenant, kind, day, content_hash, "sending", time.time()))
        try:
            self.send(recipient[0], subject, body, key)
            status = "smtp_accepted"
        except Exception:
            status = "unknown"
        with self.fleet.db() as db:
            db.execute("UPDATE mail_receipts SET status=?,updated=? WHERE id=?", (status, time.time(), key))
        return {"id": key, "status": status, "inbox_confirmed": False}

    def receipts(self, tenant):
        with self.fleet.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM mail_receipts WHERE tenant=? ORDER BY updated DESC LIMIT 100", (tenant,))]
