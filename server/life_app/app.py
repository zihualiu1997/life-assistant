import contextlib
import datetime as dt
import hashlib
from functools import wraps
import hmac
import io
import json
import os
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo
import zipfile

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from life_assistant import atomic_write, journal, lock, valid_date
from life_service.core import ServiceError
from .config import PROVIDERS, Settings, service_config, settings
from .store import Store, digest
from . import integrations as integration
from .memory import Memories, Preferences, preferences, no_save
from . import managed

class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")

class Login(Input):
    password: str = Field(min_length=12, max_length=200)
    token: str = Field(default="", max_length=200)

class Pair(Input):
    code: str = Field(min_length=12, max_length=100)
    name: str = Field(default="桌面宠物", min_length=1, max_length=80)

class Message(Input):
    message_id: str
    conversation_id: str = "default"
    body: str = Field(min_length=1, max_length=6000)

class Record(Input):
    message_id: str
    date: str
    body: str = Field(min_length=1, max_length=6000)

class Note(Input):
    path: str
    text: str = Field(max_length=200000)
    revision: str

class Setup(Input):
    settings: Settings
    model_key: str = Field(default="", max_length=8192)
    smtp_password: str = Field(default="", max_length=8192)
    weather_key: str = Field(default="", max_length=8192)

class Enable(Input):
    mail_received: bool
    previews_approved: bool

class MemoryCorrection(Input):
    revision: int = Field(ge=1)
    text: str = Field(default="", max_length=4000)
    forget: bool = False

class EmailVerification(Input):
    email: str = Field(default="", max_length=254)
    code: str = Field(default="", max_length=6)

def password_hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600000).hex()

def create_app(root=None, background=True, secure=True):
    store = Store(root or os.environ.get("LIFE_DATA_DIR", "data"))
    cookie_name = "__Host-life_session" if secure and managed.enabled() else "life_session"
    login_flow = integration.WechatLogin(store)
    stop = threading.Event()

    @contextlib.asynccontextmanager
    async def lifespan(app):
        thread = None
        if background and os.environ.get("LIFE_MAINTENANCE") != "1":
            thread = threading.Thread(target=integration.scheduler, args=(store, stop), daemon=True)
            thread.start()
        yield
        stop.set()
        if thread: thread.join(timeout=5)
        if login_flow.process and login_flow.process.poll() is None: login_flow.process.terminate()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    memories = Memories(store)

    def configured(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            with lock(store.root, "settings"):
                return fn(*args, **kwargs)
        return wrapped

    @app.middleware("http")
    async def guard(request, call_next):
        if os.environ.get("LIFE_MAINTENANCE") == "1" and request.url.path != "/health/ready":
            return JSONResponse({"detail": "maintenance"}, 503)
        expected = os.environ.get("LIFE_PUBLIC_ORIGIN", "")
        if request.method in {"POST", "PUT", "DELETE"}:
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != (expected or str(request.base_url).rstrip("/")):
                return JSONResponse({"detail": "origin_refused"}, 403)
            data = bytearray()
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > 2_000_000: return JSONResponse({"detail": "request_too_large"}, 413)
            request._body = bytes(data)
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY", "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    @app.exception_handler(ServiceError)
    async def service_error(request, exc): return JSONResponse({"detail": exc.code}, 503)

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"detail": "invalid_input_or_conflict"}, 400)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc): return JSONResponse({"detail": "invalid_input"}, 422)

    @app.exception_handler(Exception)
    async def unknown_error(request, exc): return JSONResponse({"detail": "operation_failed"}, 500)

    def rate(request, purpose, maximum=8):
        # The reverse proxy is loopback-only; forwarded addresses are configured by uvicorn.
        ip = request.client.host if request.client else "unknown"
        if store.limited(purpose + ":" + ip, maximum): raise HTTPException(429, "try_later")

    def token_owner(token, kind):
        with store.db() as db:
            row = db.execute("SELECT * FROM tokens WHERE hash=? AND kind=? AND expires>?", (digest(token), kind, time.time())).fetchone()
            if not row: raise HTTPException(401, "authentication_required")
            if kind == "device":
                device = db.execute("SELECT enabled FROM devices WHERE id=?", (row["owner"],)).fetchone()
                if not device or not device[0]: raise HTTPException(401, "device_revoked")
            return row["owner"]

    def admin(request: Request):
        token = request.cookies.get(cookie_name, "")
        owner = token_owner(token, "admin")
        if request.method not in {"GET", "HEAD"} and not hmac.compare_digest(request.headers.get("x-csrf-token", ""), digest(token + ":csrf")):
            raise HTTPException(403, "csrf_required")
        return owner

    def device(request: Request):
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "): raise HTTPException(401, "authentication_required")
        return token_owner(auth[7:], "device")

    def either(request: Request):
        if request.headers.get("authorization"): return device(request)
        return admin(request)

    def issue_session(response):
        token = secrets.token_urlsafe(32)
        with store.db() as db: db.execute("INSERT INTO tokens VALUES (?,?,?,?)", (digest(token), "admin", "admin", time.time() + 43200))
        response.set_cookie(cookie_name, token, httponly=True, secure=secure, samesite="strict", max_age=43200, path="/")
        return {"csrf": digest(token + ":csrf")}

    @app.get("/api/bootstrap")
    def bootstrap_status(): return {"initialized": bool(store.get("admin"))}

    @app.get("/health/ready")
    def ready():
        with store.db() as db: db.execute("SELECT count(*) FROM kv").fetchone()
        return {"ready": True, "data_format": 1, "maintenance": os.environ.get("LIFE_MAINTENANCE") == "1"}

    @app.post("/api/bootstrap")
    def bootstrap(value: Login, request: Request, response: Response):
        rate(request, "bootstrap")
        with lock(store.root, "bootstrap"):
            credential = store.get("bootstrap", {})
            if store.get("admin") or credential.get("expires", 0) < time.time() or not hmac.compare_digest(credential.get("hash", ""), digest(value.token)):
                raise HTTPException(403, "invalid_bootstrap_token")
            salt = secrets.token_hex(16)
            store.initialize()
            store.put("admin", {"salt": salt, "hash": password_hash(value.password, salt)})
            store.put("bootstrap", {})
        return issue_session(response)

    @app.post("/api/login")
    def login(value: Login, request: Request, response: Response):
        rate(request, "login")
        saved = store.get("admin", {})
        actual = password_hash(value.password, saved.get("salt", "00" * 16))
        if not hmac.compare_digest(saved.get("hash", ""), actual): raise HTTPException(401, "invalid_password")
        return issue_session(response)

    @app.get("/api/session")
    def session(request: Request, owner=Depends(admin)):
        return {"csrf": digest(request.cookies[cookie_name] + ":csrf")}

    @app.post("/api/logout")
    def logout(request: Request, response: Response, owner=Depends(admin)):
        with store.db() as db: db.execute("DELETE FROM tokens WHERE hash=?", (digest(request.cookies[cookie_name]),))
        response.delete_cookie(cookie_name, path="/", secure=secure, httponly=True, samesite="strict")
        return {"status": "ok"}

    @app.get("/api/settings")
    def get_settings(owner=Depends(admin)):
        if managed.enabled():
            values = settings(store).model_dump()
            return {"managed": True, "settings": {key: values[key] for key in ("display_name", "timezone", "morning", "evening", "enabled")}}
        return {"settings": settings(store).model_dump(), "providers": PROVIDERS, "secrets": {k: bool(store.secret(k)) for k in ("model", "smtp", "weather")}, "checks": store.get("checks", {})}

    @app.get("/api/usage")
    def user_usage(owner=Depends(admin)):
        return managed.call("usage")

    @app.post("/api/email/{action}")
    def user_email(action: str, value: EmailVerification, owner=Depends(admin)):
        if action not in {"request-verification", "verify"}: raise HTTPException(404)
        result = managed.call("mail/" + action, {"email": value.email} if action == "request-verification" else {"code": value.code})
        if action == "request-verification":
            store.put("email_pending", value.email)
            store.put("email_verified", False)
        else:
            store.put("email_verified", True)
            store.put("verified_email", store.get("email_pending"))
        return result

    @app.get("/api/preferences")
    def get_preferences(owner=Depends(admin)):
        return preferences(store).model_dump()

    @app.put("/api/preferences")
    def save_preferences(value: Preferences, owner=Depends(admin)):
        with lock(store.root, "settings"):
            store.put("preferences", value.model_dump())
            s = settings(store)
            s.timezone = value.timezone
            s.display_name, s.morning, s.evening = value.display_name, value.morning, value.evening
            s.model_context_approved = value.cloud_processing
            s.archive_enabled = value.cloud_processing and value.automatic_memory and not value.paused
            if value.paused or not value.cloud_processing: s.enabled = False
            if not value.cloud_processing:
                store.put("gateway_enabled", False)
            store.put("settings", s.model_dump())
            store.put("checks", {})
        return {"status": "saved"}

    @app.get("/api/memories")
    def memory_list(owner=Depends(admin)):
        return {"items": memories.items()}

    @app.put("/api/memories/{key}")
    def memory_correct(key: str, value: MemoryCorrection, owner=Depends(admin)):
        try: return memories.correct(key, value.revision, value.text, value.forget)
        except ValueError: raise HTTPException(409, "memory_changed_reload_first")

    @app.put("/api/settings")
    def save_settings(value: Setup, owner=Depends(admin)):
        if managed.enabled(): raise HTTPException(403, "operator_managed_settings")
        with lock(store.root, "settings"):
            before = integration.fingerprint(store)
            value.settings.enabled = False  # Activation is a separate, checked operation.
            if store.get("preferences") is not None:
                pref = preferences(store)
                value.settings.model_context_approved = pref.cloud_processing
                value.settings.archive_enabled = pref.cloud_processing and pref.automatic_memory and not pref.paused
            store.put("settings", value.settings.model_dump())
            for name, credential in (("model", value.model_key), ("smtp", value.smtp_password), ("weather", value.weather_key)):
                if credential: store.secret(name, credential)
            if before != integration.fingerprint(store): store.put("checks", {})
            service_config(store)
            if value.settings.model and store.secret("model"): integration.write_gateway(store)
        return {"status": "saved", "enabled": False}

    @app.post("/api/test/{kind}")
    @configured
    def test_integration(kind: str, owner=Depends(admin)):
        c = service_config(store)
        if kind == "model":
            result = integration.test_model(store)
        elif kind == "mail":
            from life_service.mail import test_connection
            result = test_connection(c)
        elif kind == "weather":
            from openweather_sync import sync
            if not c["weather"]["enabled"]: raise ServiceError("weather_not_configured")
            result = sync(store.root, {"key": store.secret("weather")}, force=True, location=c["weather"], timezone=c["tz"])
            if result["status"] not in {"synced", "cached"}: raise ServiceError("weather_unavailable")
            result = {"status": result["status"]}
        else: raise HTTPException(404)
        checks = store.get("checks", {})
        checks[kind] = integration.fingerprint(store)
        store.put("checks", checks)
        return result

    @app.get("/api/cities")
    def cities(q: str, owner=Depends(admin)):
        if not 1 <= len(q) <= 100: raise HTTPException(400, "invalid_city")
        raw = integration.request_json("https://api.openweathermap.org/geo/1.0/direct?" + urlencode({"q": q, "limit": 5, "appid": store.secret("weather")}))
        return {"items": [{"name": p.get("local_names", {}).get("zh", p["name"]), "country": p["country"], "latitude": p["lat"], "longitude": p["lon"]} for p in raw]}

    @app.post("/api/preview/{kind}")
    @configured
    def preview(kind: str, owner=Depends(admin)):
        if kind not in {"morning", "evening"}: raise HTTPException(404)
        from life_service.context import collect
        from life_service.model import generate
        c = service_config(store)
        now = dt.datetime.now(c["tz"])
        target = now.date() + dt.timedelta(days=kind == "evening")
        text = generate(c, kind, collect(c, kind, target, now))
        checks = store.get("checks", {})
        checks[kind] = integration.fingerprint(store)
        store.put("checks", checks)
        return {"body": text, "sent": False}

    @app.post("/api/activate")
    @configured
    def activate(value: Enable, owner=Depends(admin)):
        if store.get("preferences") is not None and preferences(store).paused:
            raise HTTPException(409, "resume_before_activation")
        checks = store.get("checks", {})
        required = ["model", "mail", "morning", "evening"]
        if service_config(store)["weather"]["enabled"]: required.append("weather")
        if not value.mail_received or not value.previews_approved or any(checks.get(k) != integration.fingerprint(store) for k in required):
            raise HTTPException(409, "complete_tests_and_previews_first")
        s = settings(store)
        s.enabled = True
        store.put("settings", s.model_dump())
        service_config(store)
        return {"status": "enabled"}

    @app.get("/api/status")
    def status(owner=Depends(admin)):
        from life_service.runner import status
        return {**status(service_config(store)), "heartbeat": store.get("heartbeat", {}), "archive": store.get("capture_run", {}), "wechat": login_flow.status}

    @app.post("/api/wechat/login")
    def wechat_login(owner=Depends(admin)): return login_flow.start()

    @app.get("/api/wechat/status")
    def wechat_status(owner=Depends(admin)): return login_flow.status

    @app.get("/api/wechat/qr")
    def wechat_qr(owner=Depends(admin)):
        import segno
        url = login_flow.status.get("url", "")
        if not url.startswith("https://liteapp.weixin.qq.com/"): raise HTTPException(404)
        output = io.BytesIO()
        segno.make(url).save(output, kind="png", scale=6, border=4)
        return Response(output.getvalue(), media_type="image/png")

    @app.post("/api/wechat/start")
    def wechat_start(owner=Depends(admin)):
        integration.restart_gateway(store)
        return {"status": "started", "verified_reply": False}

    @app.post("/api/pairing")
    def create_pair(owner=Depends(admin)):
        if managed.enabled(): raise HTTPException(404)
        code = secrets.token_urlsafe(18)
        with store.db() as db: db.execute("INSERT INTO tokens VALUES (?,?,?,?)", (digest(code), "pair", "", time.time() + 600))
        return {"code": code, "expires_in": 600}

    @app.post("/v1/pair")
    def pair(value: Pair, request: Request):
        rate(request, "pair")
        with store.db() as db:
            row = db.execute("SELECT * FROM tokens WHERE hash=? AND kind='pair' AND expires>?", (digest(value.code), time.time())).fetchone()
            if not row: raise HTTPException(403, "invalid_pairing_code")
            device_id, token = str(uuid4()), secrets.token_urlsafe(32)
            db.execute("DELETE FROM tokens WHERE hash=?", (digest(value.code),))
            db.execute("INSERT INTO devices VALUES (?,?,1)", (device_id, value.name))
            db.execute("INSERT INTO tokens VALUES (?,?,?,?)", (digest(token), "device", device_id, time.time() + 86400 * 3650))
        return {"device_id": device_id, "token": token}

    @app.get("/api/devices")
    def list_devices(owner=Depends(admin)):
        with store.db() as db: return {"items": [dict(r) for r in db.execute("SELECT * FROM devices")]}

    @app.delete("/api/devices/{device_id}")
    def revoke_device(device_id: str, owner=Depends(admin)):
        with store.db() as db: db.execute("UPDATE devices SET enabled=0 WHERE id=?", (device_id,))
        return {"status": "revoked"}

    @app.get("/v1/health")
    def health(owner=Depends(either)): return {"status": "ok", "api_version": 1}

    @app.post("/v1/journal")
    def record(value: Record, owner=Depends(either)):
        if str(UUID(value.message_id)) != value.message_id: raise ValueError()
        if no_save(value.body): return {"status": "not_saved"}
        try: result = journal(store.root, value.date, "desktop", owner + ":" + value.message_id, value.body, ZoneInfo(settings(store).timezone))
        except ValueError as e:
            if str(e) == "message_id_conflict": raise HTTPException(409, "message_id_conflict")
            raise
        return {"status": result["status"], "source": Path(result["path"]).relative_to(store.root).as_posix(), "date": value.date}

    @app.get("/v1/query")
    def query(kind: str, date: str, owner=Depends(either)):
        valid_date(date)
        if kind == "plan":
            from life_service.context import source_text
            c = service_config(store)
            item = source_text(c, c["context"]["sources"][2], dt.date.fromisoformat(date))
            return {"status": "found" if item and item.get("status") == "selected" else "missing", "date": date, "kind": kind, "items": [{**(item or {}), "date": date, "status": "found" if item and item.get("status") == "selected" else "missing"}]}
        folder = {"journal": "01_日常记录", "morning": "05_复盘与计划/晨报", "evening": "05_复盘与计划/明日安排"}.get(kind)
        if not folder: raise HTTPException(400, "invalid_kind")
        relative = f"{folder}/{date[:4]}/{date}.md"
        path = store.note(relative)
        found = path.is_file()
        if found and path.stat().st_size > 1_000_000: raise HTTPException(413)
        return {"status": "found" if found else "missing", "date": date, "kind": kind, "items": [{"status": "found" if found else "missing", "date": date, "source": relative, "text": path.read_text(encoding="utf-8") if found else ""}]}

    @app.post("/v1/chat")
    def chat(value: Message, request: Request, owner=Depends(either)):
        rate(request, "chat:" + owner, 60)
        UUID(value.message_id)
        UUID(value.conversation_id)
        try: return integration.chat(store, owner, value.conversation_id, value.message_id, value.body)
        except ValueError: raise HTTPException(409, "message_id_conflict")

    @app.get("/v1/history")
    def history(owner=Depends(either)):
        with store.db() as db:
            return {"items": [dict(r) for r in db.execute("SELECT id,conversation,body,reply,status,created FROM messages ORDER BY created DESC LIMIT 100")]}

    @app.get("/v1/notes")
    def notes(owner=Depends(either)): return {"items": store.notes()}

    @app.get("/v1/note")
    def read_note(path: str, owner=Depends(either)):
        p = store.note(path)
        if not p.exists(): return {"text": "", "revision": "", "path": path}
        if p.stat().st_size > 200000: raise HTTPException(413)
        text = p.read_text(encoding="utf-8")
        return {"text": text, "revision": digest(text), "path": path}

    @app.put("/v1/note")
    def write_note(value: Note, owner=Depends(either)):
        p = store.note(value.path)
        with lock(store.root, "notes"):
            old = p.read_text(encoding="utf-8") if p.exists() else None
            if (digest(old) if old is not None else "") != value.revision: raise HTTPException(409, "note_changed_reload_first")
            if old is not None:
                atomic_write(store.state / "note-history" / digest(value.path) / (str(time.time_ns()) + ".md"), old)
            atomic_write(p, value.text)
        return {"revision": digest(value.text), "status": "saved"}

    @app.get("/api/export")
    def export(owner=Depends(admin)):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            total = 0
            for name in store.notes():
                path = store.note(name)
                total += path.stat().st_size
                if total > 50_000_000: raise HTTPException(413, "use_server_backup")
                archive.write(path, name)
            personal = settings(store).model_dump(include={"display_name", "timezone", "morning", "evening", "capture", "enabled", "archive_enabled", "model_context_approved"})
            exports = {"个人设置.json": personal, "隐私偏好.json": preferences(store).model_dump(), "记忆.json": memories.items()}
            with store.db() as db:
                exports["对话记录.json"] = [dict(row) for row in db.execute("SELECT conversation,body,reply,status,created FROM messages ORDER BY created")]
            for name, value in exports.items():
                encoded = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
                total += len(encoded)
                if total > 50_000_000: raise HTTPException(413, "use_server_backup")
                archive.writestr(name, encoded)
            for path in (store.root / "01_日常记录").glob("????/附件/**/*"):
                if not path.is_file(): continue
                if path.is_symlink() or not path.resolve().is_relative_to(store.root / "01_日常记录"): raise HTTPException(400, "unsafe_attachment")
                total += path.stat().st_size
                if total > 50_000_000: raise HTTPException(413, "use_server_backup")
                archive.write(path, path.relative_to(store.root).as_posix())
        return Response(buffer.getvalue(), media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="life-notes.zip"'})

    @app.post("/api/import")
    def import_notes(items: list[Note], owner=Depends(admin)):
        if len(items) > 100: raise HTTPException(413)
        # Validate every path before any write. Existing notes are never overwritten by import.
        paths = [store.note(v.path) for v in items]
        if len(set(paths)) != len(paths): raise HTTPException(400, "duplicate_path")
        with lock(store.root, "notes"):
            if any(p.exists() for p in paths): raise HTTPException(409, "import_would_overwrite")
            for value, p in zip(items, paths): atomic_write(p, value.text)
        return {"status": "imported", "count": len(paths)}

    @app.post("/api/import-chat")
    def import_chat(items: list[Message], owner=Depends(admin)):
        if len(items) > 100: raise HTTPException(413)
        count = 0
        with store.db() as db:
            for item in items:
                UUID(item.message_id)
                key = digest("import:" + item.message_id)
                old = db.execute("SELECT body FROM messages WHERE id=?", (key,)).fetchone()
                if old and old[0] != item.body: raise HTTPException(409, "import_id_conflict")
                count += db.execute("INSERT OR IGNORE INTO messages(id,owner,conversation,body,reply,status,created) VALUES (?,?,?,?,?,?,?)", (key, "import", item.conversation_id, item.body, "外部导入，仅用户原文。", "completed", time.time())).rowcount
        return {"status": "imported", "count": count}

    @app.post("/internal/{action}")
    def bridge(action: str, request: Request, value: dict):
        if request.client and request.client.host not in {"127.0.0.1", "::1", "testclient"}: raise HTTPException(403)
        expected = "Bearer " + store.secret("bridge")
        if not store.secret("bridge") or not hmac.compare_digest(request.headers.get("authorization", ""), expected): raise HTTPException(401)
        if action == "transcribe":
            from .audio import transcribe
            return transcribe(store, str(value.get("path", "")))
        if action == "search": return {"items": integration.search(store, str(value.get("query", ""))[:6000])}
        if action == "journal":
            if no_save(str(value.get("body", ""))): return {"status": "not_saved"}
            timezone = ZoneInfo(settings(store).timezone)
            day = value.get("date") or dt.datetime.now(timezone).date().isoformat()
            result = journal(store.root, day, "wechat", value["message_id"], value["body"], timezone)
            return {"status": result["status"], "source": Path(result["path"]).relative_to(store.root).as_posix()}
        if action == "note":
            if "text" in value: return write_note(Note(**value), "bridge")
            return read_note(value["path"], "bridge")
        if action == "capture":
            # Called by the trusted gateway hook after a completed WeChat turn.
            body, reply = str(value.get("body", ""))[:6000], str(value.get("reply", ""))[:12000]
            if not body or not value.get("message_id"): raise HTTPException(400)
            key = digest("wechat:" + str(value["message_id"]))
            kind = value.get("source_kind", "unclassified")
            if kind not in ("user_text", "voice_transcript", "image_interpretation", "unclassified"):
                raise HTTPException(400, "invalid_source_kind")
            conversation = str(value.get("conversation", "wechat"))
            with store.db() as db:
                old = db.execute("SELECT body,conversation FROM messages WHERE id=?", (key,)).fetchone()
                if old and (old["body"] != body or old["conversation"] != conversation):
                    raise HTTPException(409, "capture_id_conflict")
                if not old:
                    db.execute("INSERT INTO messages(id,owner,conversation,body,reply,status,created) VALUES (?,?,?,?,?,?,?)", (key, "wechat", conversation, body, reply, "completed", time.time()))
                    db.execute("INSERT INTO kv VALUES (?,?)", ("source-kind:" + key, json.dumps(kind)))
                    db.execute("INSERT INTO kv VALUES (?,?)", ("source-message:" + key, json.dumps(str(value["message_id"]))))
            return {"status": "accepted"}
        raise HTTPException(404)

    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    def home(): return FileResponse(static / ("pilot.html" if managed.enabled() else "index.html"))

    @app.get("/memory")
    def memory_page(): return FileResponse(static / "memory.html")

    return app
