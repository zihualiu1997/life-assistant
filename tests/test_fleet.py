import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from fastapi.testclient import TestClient
from life_fleet.store import Fleet
from life_fleet.broker import create_broker


class FleetTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fleet = Fleet(self.temp.name)
        self.fleet.create("alice")
        self.fleet.state("alice", "active")
        self.token = (Path(self.temp.name) / "instances/alice/broker-token").read_text()
        self.headers = {"Authorization": "Bearer " + self.token}

    def client(self, data):
        def open_(request, timeout):
            self.assertEqual(request.headers["Authorization"], "Bearer upstream-secret")
            self.assertFalse(json.loads(request.data)["enable_thinking"])
            return io.BytesIO(data)
        return TestClient(create_broker(self.fleet, "https://model.example/v1", "upstream-secret", {"test-model"}, open_))

    def test_capacity_idempotency_and_freeze(self):
        self.assertFalse(self.fleet.create("alice")["created"])
        for i in range(4): self.fleet.create("user" + str(i))
        with self.assertRaisesRegex(ValueError, "capacity"): self.fleet.create("sixth")
        self.fleet.state("alice", "frozen")
        self.assertIsNone(self.fleet.authenticate(self.token))
        with self.assertRaisesRegex(ValueError, "capacity"): self.fleet.create("sixth")
        with self.assertRaisesRegex(ValueError, "invalid"): self.fleet.create("../outside")

    def test_compose_isolation(self):
        self.fleet.create("bob")
        spec = self.fleet.compose("registry.example/life@sha256:" + "a"*64, "pilot.example")
        a, b = spec["services"]["alice"], spec["services"]["bob"]
        self.assertNotEqual(a["networks"], b["networks"])
        self.assertNotEqual(a["volumes"], b["volumes"])
        self.assertNotIn("ports", a)
        self.assertEqual(a["mem_limit"], "2g")
        with self.assertRaises(ValueError): self.fleet.compose("life:latest", "pilot.example")

    def test_accounting_and_identity(self):
        client = self.client(json.dumps({"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 7}}).encode())
        payload = {"model": "test-model", "messages": [{"role": "user", "content": "private sentence"}]}
        self.assertEqual(client.post("/v1/chat/completions", json=payload, headers=self.headers).status_code, 200)
        self.fleet.create("bob")
        self.assertEqual(self.fleet.usage("bob"), [])
        entry = self.fleet.usage("alice")[0]
        self.assertEqual(entry["input_tokens"], 5)
        self.assertNotIn("private sentence", json.dumps(entry))
        self.assertIsNone(entry["estimated_cost"])
        self.assertEqual(client.post("/v1/chat/completions", json={**payload, "tenant": "bob"}, headers=self.headers).status_code, 400)
        self.assertEqual(client.get("/v1/usage").status_code, 401)

    def test_stream_and_missing_usage(self):
        client = self.client(b'data: {"choices":[],"usage":{"prompt_tokens":9,"completion_tokens":2}}\n\ndata: [DONE]\n\n')
        payload = {"model": "test-model", "messages": [{"role": "user", "content": "hello"}], "stream": True}
        result = client.post("/v1/chat/completions", json=payload, headers=self.headers)
        self.assertIn("[DONE]", result.text)
        self.assertEqual(self.fleet.usage("alice")[0]["input_tokens"], 9)
        client = self.client(b'{"choices":[]}')
        payload["stream"] = False
        self.assertEqual(client.post("/v1/chat/completions", json=payload, headers=self.headers).status_code, 200)
        self.assertEqual(self.fleet.usage("alice")[-1]["status"], "pending_reconciliation")

    def test_published_price_table_and_unknown_usage_are_distinct(self):
        price_file = self.fleet.root / 'prices.json'
        template = Path(__file__).resolve().parents[1] / 'deploy/prices-conservative-20261007.json'
        price_file.write_bytes(template.read_bytes())
        self.fleet.record('chat', 'alice', 'chat', 'deepseek-flash', 'measured',
                          {'prompt_tokens': 1_000_000, 'completion_tokens': 40_000})
        self.fleet.record('voice', 'alice', 'voice', 'qwen3-asr-flash', 'measured',
                          {'prompt_tokens': 150, 'completion_tokens': 20, 'seconds': 600})
        self.fleet.record('unknown', 'alice', 'voice', 'qwen3-asr-flash', 'measured',
                          {'prompt_tokens': 150, 'completion_tokens': 20})
        rows = {row['id']: row for row in self.fleet.usage('alice')}
        self.assertAlmostEqual(rows['chat']['estimated_cost'], 2.32)
        self.assertAlmostEqual(rows['voice']['estimated_cost'], 0.132)
        self.assertIsNone(rows['unknown']['estimated_cost'])
        original = rows['chat']['price_version']
        self.assertTrue(original.startswith('2026-10-07-conservative-peak-no-cache:'))
        changed = json.loads(price_file.read_text())
        changed['models']['deepseek-flash']['input_per_million'] = 3
        price_file.write_text(json.dumps(changed))
        self.fleet.record('new', 'alice', 'chat', 'deepseek-flash', 'measured',
                          {'prompt_tokens': 1_000_000, 'completion_tokens': 40_000})
        rows = {row['id']: row for row in self.fleet.usage('alice')}
        self.assertEqual(rows['chat']['price_version'], original)
        self.assertAlmostEqual(rows['chat']['estimated_cost'], 2.32)
        self.assertNotEqual(rows['new']['price_version'], original)
        self.assertAlmostEqual(rows['new']['estimated_cost'], 3.32)

    def test_deepseek_parameters_and_separate_voice_credentials(self):
        requests = []
        def open_(request, timeout):
            requests.append((request.full_url, request.headers["Authorization"], json.loads(request.data)))
            return io.BytesIO(b'{"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":1}}')
        voice = {"base_url":"https://voice.example/v1", "api_key":"voice-only", "model":"asr"}
        client = TestClient(create_broker(self.fleet, "https://api.deepseek.com/v1", "deepseek-only", {"deepseek-flash","asr"}, open_, voice_config=voice))
        payload = {"model":"deepseek-flash", "messages":[{"role":"user","content":"fictional probe"}]}
        self.assertEqual(client.post('/v1/chat/completions', json=payload, headers=self.headers).status_code, 200)
        self.assertEqual(requests[-1][1], 'Bearer deepseek-only')
        self.assertEqual(requests[-1][2]['thinking'], {'type':'disabled'})
        self.assertNotIn('enable_thinking', requests[-1][2])
        voice_payload = {**payload, 'model':'asr'}
        self.assertEqual(client.post('/v1/chat/completions', json=voice_payload, headers=self.headers).status_code, 400)
        self.assertEqual(client.post('/v1/chat/completions', json=voice_payload, headers={**self.headers,'x-life-purpose':'voice'}).status_code, 200)
        self.assertEqual(requests[-1][0], 'https://voice.example/v1/chat/completions')
        self.assertEqual(requests[-1][1], 'Bearer voice-only')
        disabled = TestClient(create_broker(self.fleet, "https://api.deepseek.com/v1", "deepseek-only", {"deepseek-flash"}, open_))
        self.assertEqual(disabled.post('/v1/chat/completions', json=payload, headers={**self.headers,'x-life-purpose':'voice'}).status_code, 503)

    def test_long_short_message_history_is_preserved_with_byte_limit(self):
        seen = []
        def open_(request, timeout):
            seen.append(json.loads(request.data)['messages'])
            return io.BytesIO(b'{"choices":[],"usage":{"prompt_tokens":1500,"completion_tokens":1}}')
        client = TestClient(create_broker(self.fleet,'https://model.example/v1','fixture',{'test-model'},open_))
        history = [{'role':'system','content':'fictional'}]
        for index in range(300):
            history.extend([{'role':'user','content':str(index)}, {'role':'assistant','content':'OK'}])
        history.append({'role':'user','content':'next'})
        payload = {'model':'test-model','messages':history}
        self.assertEqual(client.post('/v1/chat/completions',json=payload,headers=self.headers).status_code,200)
        self.assertEqual(seen[0],history)
        payload['messages']=[{'role':'user','content':'x'*10_000_001}]
        self.assertEqual(client.post('/v1/chat/completions',json=payload,headers=self.headers).status_code,413)
        self.assertEqual(len(seen),1)


if __name__ == "__main__": unittest.main()
