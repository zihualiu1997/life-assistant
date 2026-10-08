import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave
from life_app.store import Store
from life_app.audio import transcribe
from life_service.core import ServiceError


class AudioTest(unittest.TestCase):
    def test_private_wav_and_unconfirmed_transcript(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.put("settings", {"model_context_approved": True})
            path = Path(root) / "fixture.wav"
            with wave.open(str(path), "wb") as audio:
                audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(16000)
                audio.writeframes(b"\0\0" * 160)
            with patch("life_app.audio.managed.connection", return_value={"asr_model": "test-asr"}), patch("life_app.audio.managed.call", return_value={"choices": [{"message": {"content": "虚构语音"}}]}) as call:
                result = transcribe(store, str(path))
                self.assertFalse(result["confirmed"])
                payload = call.call_args.args[1]
                self.assertTrue(payload["messages"][0]["content"][0]["input_audio"]["data"].startswith("data:audio/wav;base64,"))
                self.assertEqual(call.call_args.args[2], "voice")

    def test_missing_consent_and_unsupported_audio_fail_before_cloud(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            path = Path(root) / "fixture.silk"
            path.write_bytes(b"#!SILK_V3")
            with patch("life_app.audio.managed.call") as call:
                with self.assertRaises(ServiceError): transcribe(store, str(path))
                store.put("settings", {"model_context_approved": True})
                with self.assertRaises(ServiceError): transcribe(store, str(path))
                with self.assertRaises(ServiceError): transcribe(store, str(Path(root).parent / "outside.wav"))
                call.assert_not_called()
