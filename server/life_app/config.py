import json
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
from pydantic import BaseModel, ConfigDict, Field, field_validator
from life_assistant import atomic_write

PROVIDERS = {"qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1", "deepseek": "https://api.deepseek.com/v1", "openai": "https://api.openai.com/v1", "custom": ""}

class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str = Field(default="生活助手用户", min_length=1, max_length=60)
    timezone: str = "Asia/Shanghai"
    provider: str = "deepseek"
    base_url: str = PROVIDERS["deepseek"]
    model: str = Field(default="deepseek-flash", max_length=100)
    city: str = Field(default="", max_length=100)
    latitude: float = Field(default=0, ge=-90, le=90)
    longitude: float = Field(default=0, ge=-180, le=180)
    smtp_host: str = Field(default="", max_length=253)
    smtp_port: int = Field(default=465, ge=1, le=65535)
    smtp_tls: str = "ssl"
    smtp_username: str = Field(default="", max_length=254)
    sender: str = Field(default="", max_length=254)
    recipient: str = Field(default="", max_length=254)
    morning: str = "08:30"
    evening: str = "21:00"
    capture: str = "22:30"
    model_context_approved: bool = False
    archive_enabled: bool = False
    enabled: bool = False

    @field_validator("timezone")
    @classmethod
    def timezone_valid(cls, v):
        try: ZoneInfo(v)
        except Exception: raise ValueError("invalid_timezone")
        return v

    @field_validator("provider")
    @classmethod
    def provider_valid(cls, v):
        if v not in PROVIDERS: raise ValueError("invalid_provider")
        return v

    @field_validator("base_url")
    @classmethod
    def url_valid(cls, v):
        u = urlsplit(v)
        if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ValueError("https_base_url_required")
        return v.rstrip("/")

    @field_validator("smtp_tls")
    @classmethod
    def tls_valid(cls, v):
        if v not in {"ssl", "starttls"}: raise ValueError("tls_required")
        return v

    @field_validator("morning", "evening", "capture")
    @classmethod
    def time_valid(cls, v):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", v): raise ValueError("invalid_time")
        return v

    @field_validator("display_name", "smtp_host", "smtp_username", "sender", "recipient", "model")
    @classmethod
    def single_line(cls, v):
        if any(ord(c) < 32 for c in v): raise ValueError("invalid_control_character")
        return v

def settings(store):
    return Settings(**store.get("settings", {}))

def service_config(store):
    s = settings(store)
    c = {"version": 1, "workspace": str(store.root), "enabled": s.enabled, "timezone": s.timezone,
         "display_name": s.display_name, "schedule": {"morning": [s.morning, "24:00"], "evening": [s.evening, "24:00"]},
         "approved": {"model_context": s.model_context_approved, "morning": s.enabled, "evening": s.enabled},
         "model": {"base_url": s.base_url, "name": s.model, "secret": "file:.secrets/model", "timeout": 60},
         "mail": {"host": s.smtp_host, "port": s.smtp_port, "tls": s.smtp_tls, "username": s.smtp_username,
                  "sender": s.sender, "recipient": s.recipient, "secret": "file:.secrets/smtp", "connection_receipt": ".local/mail/connection.json"},
         "context": {"persona": "SOUL.md", "max_chars": 24000, "sources": [
             {"path": "总览.md", "sections": ["当前重点"]},
             {"path": "02_生活领域/个人资料.md", "sections": ["简报可用"]},
             {"path": "03_目标与项目/计划.md", "table_section": "安排", "sections": []}],
             "journal_dir": "01_日常记录", "journal_sections": ["简报可用"], "journals": []},
         "weather": {"enabled": bool(store.secret("weather") and s.city), "city": s.city, "latitude": s.latitude,
                     "longitude": s.longitude, "secret": "file:.secrets/weather"}, "whoop": {"enabled": False}}
    # Morning catch-up lasts at most 150 minutes, never to the following day.
    minutes = int(s.morning[:2]) * 60 + int(s.morning[3:]) + 150
    c["schedule"]["morning"][1] = "24:00" if minutes >= 1440 else f"{minutes//60:02}:{minutes%60:02}"
    path = store.state / "service.json"
    from . import managed
    if managed.enabled():
        operator = managed.connection()
        c["model"]["name"] = operator["model"]
        c["mail"]["recipient"] = store.get("verified_email", "")
    atomic_write(path, json.dumps(c, ensure_ascii=False, indent=2))
    from life_service.core import load_config
    return load_config(path)
