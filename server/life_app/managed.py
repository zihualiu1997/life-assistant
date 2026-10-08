"""Operator-owned connection settings, never writable through user routes."""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from life_service.core import ServiceError


def enabled():
    return os.environ.get("LIFE_MANAGED") == "1"


def connection():
    if not enabled(): return None
    try:
        if os.environ.get("LIFE_BROKER_TOKEN_FILE"):
            value = {"token": Path(os.environ["LIFE_BROKER_TOKEN_FILE"]).read_text(encoding="utf-8"),
                     "broker_url": "http://broker:18933/v1", "model": os.environ["LIFE_OPERATOR_MODEL"], "asr_model": os.environ["LIFE_OPERATOR_ASR_MODEL"]}
        else:
            value = json.loads(Path(os.environ.get("LIFE_INSTANCE_CONFIG", "/run/life/instance.json")).read_text(encoding="utf-8"))
        parsed = urlsplit(value["broker_url"])
        if parsed.scheme != "http" or parsed.hostname != "broker" or parsed.port != 18933 or parsed.path != "/v1" or parsed.username or parsed.query or parsed.fragment:
            raise ValueError()
        if not value["token"] or not value["model"]: raise ValueError()
        return value
    except (OSError, KeyError, ValueError): raise ServiceError("managed_connection_unavailable") from None


def call(action, payload=None, purpose="chat"):
    from .integrations import request_json
    c = connection()
    if not c: raise ServiceError("managed_mode_required")
    return request_json(c["broker_url"] + "/" + action, payload,
                        {"Authorization": "Bearer " + c["token"], "X-Life-Purpose": purpose})


def model_request(store, payload, purpose):
    from .config import settings
    from .integrations import request_json
    c = connection()
    if c:
        return call("chat/completions", {**payload, "model": c["model"]}, purpose)
    s = settings(store)
    if urlsplit(s.base_url).hostname == "api.deepseek.com":
        payload = {**payload, "thinking": {"type": "disabled"}}
    return request_json(s.base_url + "/chat/completions", payload,
                        {"Authorization": "Bearer " + store.secret("model")})
