"""Private model gateway. Upstream secrets and request bodies never enter the ledger."""
import asyncio
import json
import secrets
import urllib.request
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def create_broker(fleet, base_url, api_key, models, opener=None, mail_relay=None, voice_config=None):
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
        raise ValueError("invalid_operator_upstream")
    if voice_config:
        voice_url = urlsplit(voice_config["base_url"])
        if voice_url.scheme != "https" or not voice_url.hostname or voice_url.username or voice_url.query or voice_url.fragment:
            raise ValueError("invalid_voice_upstream")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    global_slots = asyncio.Semaphore(5)
    local_slots = {}
    opener = opener or urllib.request.build_opener(NoRedirect()).open
    purposes = {"chat", "image", "voice", "proactive", "memory", "briefing", "test"}

    def identity(request):
        value = request.headers.get("authorization", "")
        if not value.startswith("Bearer "):
            raise HTTPException(401, "unauthorized")
        name = fleet.authenticate(value[7:])
        if not name: raise HTTPException(401, "unauthorized")
        return name

    @app.get("/v1/usage")
    async def usage(request: Request):
        return {"entries": fleet.usage(identity(request)), "cost_is_estimate": True}

    @app.post("/v1/mail/{action}")
    async def mail(action: str, request: Request):
        name = identity(request)
        if mail_relay is None: raise HTTPException(503, "mail_not_configured")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 200000: raise HTTPException(413, "request_too_large")
        try:
            data = json.loads(raw)
            keys = {"request-verification": {"email"}, "verify": {"code"},
                    "submit": {"kind", "day", "subject", "body"}, "receipts": set()}
            if action not in keys or not isinstance(data, dict) or set(data) != keys[action]: raise ValueError()
            if not all(isinstance(value, str) for value in data.values()): raise ValueError()
            if action == "request-verification": return await run_in_threadpool(mail_relay.request_verification, name, data["email"])
            if action == "verify": return mail_relay.verify(name, data["code"])
            if action == "receipts": return {"items": mail_relay.receipts(name)}
            return await run_in_threadpool(mail_relay.deliver, name, **data)
        except ValueError: raise HTTPException(400, "invalid_mail_request") from None

    @app.post("/v1/chat/completions")
    async def completions(request: Request):
        name = identity(request)
        purpose = request.headers.get("x-life-purpose", "chat")
        if purpose not in purposes: raise HTTPException(400, "invalid_purpose")
        upstream_base, upstream_key = base_url, api_key
        if purpose == "voice" and voice_config:
            upstream_base, upstream_key = voice_config["base_url"], voice_config["api_key"]
        elif purpose == "voice" and parsed.hostname == "api.deepseek.com":
            raise HTTPException(503, "voice_provider_not_configured")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 10_000_000: raise HTTPException(413, "request_too_large")
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict): raise ValueError()
            allowed = {"model", "messages", "tools", "tool_choice", "stream", "stream_options",
                       "temperature", "top_p", "max_tokens", "max_completion_tokens", "parallel_tool_calls"}
            if set(payload) - allowed: raise ValueError()
            model = payload.get("model")
            if model not in models: raise ValueError()
            if voice_config and ((purpose == "voice") != (model == voice_config["model"])): raise ValueError()
            if not isinstance(payload.get("messages"), list) or not payload["messages"]: raise ValueError()
            # The bounded request body limits memory. A short-message count cap
            # rejects healthy long sessions before the gateway's token-based
            # compaction can run; never truncate conversation/tool pairs here.
            if not isinstance(payload.get("stream", False), bool): raise ValueError()
            for key in ("max_tokens", "max_completion_tokens"):
                if key in payload and (type(payload[key]) is not int or payload[key] < 1): raise ValueError()
            if "max_tokens" in payload and "max_completion_tokens" in payload: raise ValueError()
            cap = "max_completion_tokens" if "max_completion_tokens" in payload else "max_tokens"
            if purpose != "voice": payload[cap] = min(payload.get(cap, 4096), 4096)
            # Attachments must be private inline data, never fetch arbitrary URLs.
            for message in payload["messages"]:
                if not isinstance(message, dict): raise ValueError()
                content = message.get("content")
                if isinstance(content, list):
                    for part in content:
                        if part.get("type") == "image_url":
                            if not part.get("image_url", {}).get("url", "").startswith("data:image/"): raise ValueError()
                            if purpose == "chat": purpose = "image"
                        elif part.get("type") == "input_audio":
                            if not part.get("input_audio", {}).get("data", "").startswith("data:audio/"): raise ValueError()
                        elif part.get("type") not in {"text", "input_audio"}: raise ValueError()
            if purpose != "voice":
                if parsed.hostname == "api.deepseek.com": payload["thinking"] = {"type": "disabled"}
                else: payload["enable_thinking"] = False
            if payload.get("stream"): payload["stream_options"] = {"include_usage": True}
            last_user = max((i for i, m in enumerate(payload["messages"]) if m.get("role") == "user"), default=0)
            if sum(1 for m in payload["messages"][last_user:] if m.get("role") == "tool") >= 8:
                raise HTTPException(429, "tool_loop_limit")
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(400, "invalid_request") from None
        slots = local_slots.setdefault(name, asyncio.Semaphore(2))
        acquired_local = acquired_global = False
        try:
            await asyncio.wait_for(slots.acquire(), timeout=1)
            acquired_local = True
            await asyncio.wait_for(global_slots.acquire(), timeout=1)
            acquired_global = True
        except (TimeoutError, asyncio.CancelledError) as exc:
            if acquired_local: slots.release()
            if isinstance(exc, asyncio.CancelledError): raise
            raise HTTPException(429, "concurrency_limit") from None
        request_id = secrets.token_hex(16)
        try:
            fleet.record(request_id, name, purpose, model, "in_flight")
        except BaseException:
            slots.release()
            global_slots.release()
            raise
        response = None
        finished = False

        def finish(status, measured=None):
            nonlocal finished
            if finished: return
            finished = True
            try:
                fleet.record(request_id, name, purpose, model, status, measured)
            finally:
                if response is not None: response.close()
                slots.release()
                global_slots.release()

        upstream = urllib.request.Request(upstream_base.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode(), headers={"Authorization": "Bearer " + upstream_key,
            "Content-Type": "application/json"}, method="POST")
        try:
            response = await run_in_threadpool(opener, upstream, timeout=60)
            if not payload.get("stream"):
                raw = await run_in_threadpool(response.read, 4_000_001)
                if len(raw) > 4_000_000: raise ValueError("upstream_too_large")
                result = json.loads(raw)
                measured = result.get("usage")
                finish("measured" if isinstance(measured, dict) else "pending_reconciliation", measured)
                return result
        except BaseException as exc:
            finish("upstream_error")
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)): raise
            raise HTTPException(502, "upstream_failed") from None

        async def events():
            measured = None
            completed = False
            try:
                while True:
                    line = await run_in_threadpool(response.readline, 1_000_001)
                    if not line: break
                    if len(line) > 1_000_000: raise ValueError("event_too_large")
                    if line.startswith(b"data:"):
                        data = line[5:].strip()
                        if data == b"[DONE]": completed = True
                        elif data:
                            item = json.loads(data)
                            if isinstance(item.get("usage"), dict): measured = item["usage"]
                    yield line
            finally:
                status = ("measured" if measured else "pending_reconciliation") if completed else "interrupted"
                finish(status, measured)
        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    return app
