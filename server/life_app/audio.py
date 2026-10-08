import base64
import io
from pathlib import Path
import wave
from . import managed
from .config import settings
from life_service.core import ServiceError


def transcribe(store, filename):
    if not settings(store).model_context_approved: raise ServiceError("model_context_not_approved")
    path = Path(filename).resolve()
    if not path.is_relative_to(store.root) or not path.is_file(): raise ServiceError("audio_path_refused")
    if path.stat().st_size > 7_000_000: raise ServiceError("audio_too_large")
    raw = path.read_bytes()
    try:
        with wave.open(io.BytesIO(raw)) as audio:
            if audio.getframerate() <= 0 or audio.getnframes() / audio.getframerate() > 300: raise ValueError()
    except (wave.Error, EOFError, ValueError): raise ServiceError("audio_decode_failed_or_unsupported") from None
    operator = managed.connection()
    if not operator or not operator.get("asr_model"):
        raise ServiceError("voice_provider_not_configured")
    result = managed.call("chat/completions", {"model": operator["asr_model"], "messages": [{"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": "data:audio/wav;base64," + base64.b64encode(raw).decode()}}]}], "stream": False}, "voice")
    try:
        text = result["choices"][0]["message"]["content"]
        if not isinstance(text, str) or not text.strip() or len(text) > 12000: raise ValueError()
        return {"text": text, "source_kind": "voice_transcript", "confirmed": False}
    except (ValueError, KeyError, IndexError, TypeError): raise ServiceError("audio_transcription_failed") from None
