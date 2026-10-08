import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
import zipfile
from fastapi.testclient import TestClient
from life_app.app import create_app
from life_app.memory import Preferences
from life_app import integrations


class PilotIsolationTest(unittest.TestCase):
    def test_capture_preserves_native_provenance_and_rejects_conflicting_replay(self):
        from life_app.store import digest
        with tempfile.TemporaryDirectory() as root:
            app = create_app(root, background=False, secure=False)
            store = app.state.store
            store.initialize()
            with TestClient(app) as client:
                headers = {"Authorization": "Bearer " + store.secret("bridge")}
                value = {"message_id": "fictional-native-id", "conversation": "fictional-session", "body": "虚构用户原话", "reply": "模型建议", "source_kind": "voice_transcript"}
                self.assertEqual(client.post("/internal/capture", json=value, headers=headers).status_code, 200)
                key = digest("wechat:" + value["message_id"])
                self.assertEqual(store.get("source-kind:" + key), "voice_transcript")
                self.assertEqual(store.get("source-message:" + key), value["message_id"])
                self.assertEqual(client.post("/internal/capture", json={**value, "source_kind": "user_text"}, headers=headers).status_code, 200)
                self.assertEqual(store.get("source-kind:" + key), "voice_transcript")
                self.assertEqual(client.post("/internal/capture", json={**value, "body": "替换原话"}, headers=headers).status_code, 409)
                self.assertEqual(client.post("/internal/capture", json={**value, "source_kind": "invented"}, headers=headers).status_code, 400)

    def test_managed_https_cookie_cannot_be_set_for_parent_domain(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"LIFE_MANAGED": "1"}):
            app = create_app(root, background=False, secure=True)
            with TestClient(app, base_url="https://alice.pilot.example") as client:
                response = client.post("/api/bootstrap", json={"token": app.state.store.bootstrap(), "password": "fictional-password-long"})
                cookie = response.headers["set-cookie"]
                self.assertIn("__Host-life_session=", cookie)
                self.assertIn("Secure", cookie)
                self.assertIn("HttpOnly", cookie)
                self.assertNotIn("Domain=", cookie)
                self.assertEqual(client.get("/api/session").status_code, 200)

    def test_five_users_cannot_reuse_sessions_credentials_or_exports(self):
        with tempfile.TemporaryDirectory() as root:
            clients, stores = [], []
            for index in range(5):
                app = create_app(Path(root) / str(index), background=False, secure=False)
                store = app.state.store
                client = TestClient(app)
                self.addCleanup(client.close)
                response = client.post("/api/bootstrap", json={"token": store.bootstrap(), "password": "fictional-password-long"})
                client.headers["x-csrf-token"] = response.json()["csrf"]
                response = client.post("/v1/journal", json={"message_id": str(uuid4()), "date": "2026-10-06", "body": f"FICTIONAL_OWNER_{index}"})
                self.assertEqual(response.status_code, 200)
                clients.append(client)
                stores.append(store)
            for index, client in enumerate(clients):
                other = (index + 1) % 5
                denied = TestClient(client.app)
                self.addCleanup(denied.close)
                denied.cookies.set("life_session", clients[other].cookies["life_session"])
                self.assertEqual(denied.get("/api/export").status_code, 401)
                response = client.post("/internal/search", headers={"Authorization": "Bearer " + stores[other].secret("bridge")}, json={"query": "FICTIONAL"})
                self.assertEqual(response.status_code, 401)
                result = client.get("/api/export")
                archive = zipfile.ZipFile(io.BytesIO(result.content))
                all_text = "\n".join(archive.read(name).decode("utf-8") for name in archive.namelist())
                self.assertIn(f"FICTIONAL_OWNER_{index}", all_text)
                self.assertNotIn(f"FICTIONAL_OWNER_{other}", all_text)
                self.assertNotIn(stores[index].secret("bridge"), all_text)

    def test_managed_settings_never_disclose_operator_config(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"LIFE_MANAGED": "1"}):
            app = create_app(root, background=False, secure=False)
            store = app.state.store
            client = TestClient(app)
            self.addCleanup(client.close)
            response = client.post("/api/bootstrap", json={"token": store.bootstrap(), "password": "fictional-password-long"})
            client.headers["x-csrf-token"] = response.json()["csrf"]
            result = client.get("/api/settings").json()
            self.assertTrue(result["managed"])
            for key in ("base_url", "smtp_host", "smtp_username", "sender", "model_key"):
                self.assertNotIn(key, result["settings"])
            self.assertEqual(client.post("/api/pairing", json={}).status_code, 404)
            prefs = Preferences(cloud_processing=True, automatic_memory=True, paused=True)
            self.assertEqual(client.put("/api/preferences", json=prefs.model_dump()).status_code, 200)
            with patch.object(integrations, "model_text") as model:
                self.assertEqual(integrations.archive_pending(store)["status"], "disabled")
                model.assert_not_called()
