"""Container test entrypoint only: no real supplier or SMTP credentials."""
import io
import json
import time
import uvicorn
from life_fleet.store import Fleet
from life_fleet.broker import create_broker
from life_fleet.mail import MailRelay


def upstream(request, timeout):
    payload = json.loads(request.data)
    time.sleep(0.03)
    result = {"choices": [{"message": {"role": "assistant", "content": "FICTIONAL_REPLY"}}],
              "usage": {"prompt_tokens": 12, "completion_tokens": 4}}
    if payload.get("stream"):
        chunks = [{"id": "fixture", "object": "chat.completion.chunk", "created": 1, "model": "fixture-model",
                   "choices": [{"index": 0, "delta": {"role": "assistant", "content": "FICTIONAL_REPLY"}, "finish_reason": None}]},
                  {"id": "fixture", "object": "chat.completion.chunk", "created": 1, "model": "fixture-model",
                   "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": result["usage"]}]
        return io.BytesIO(("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode())
    return io.BytesIO(json.dumps(result).encode())


if __name__ == "__main__":
    fleet = Fleet("/operator")
    app = create_broker(fleet, "https://fixture.invalid/v1", "fictional-only", {"fixture-model", "fixture-asr"},
                        opener=upstream, mail_relay=MailRelay(fleet, lambda *args: None))
    uvicorn.run(app, host="0.0.0.0", port=18933, access_log=False, log_level="warning")
