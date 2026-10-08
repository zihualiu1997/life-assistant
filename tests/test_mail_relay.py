import re
import tempfile
import unittest
from life_fleet.store import Fleet
from life_fleet.mail import MailRelay


class MailTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fleet = Fleet(self.temp.name)
        for name in ("alice", "bob"):
            self.fleet.create(name)
            self.fleet.state(name, "active")
        self.sent = []
        self.relay = MailRelay(self.fleet, lambda *args: self.sent.append(args))

    def verified(self, name):
        self.relay.request_verification(name, name + "@example.com")
        code = re.search(r"\d{6}", self.sent[-1][2])[0]
        self.relay.verify(name, code)

    def test_recipient_scope_and_idempotency(self):
        with self.assertRaises(ValueError): self.relay.deliver("alice", "morning", "2026-10-06", "标题", "内容")
        self.verified("alice")
        self.verified("bob")
        for name in ("alice", "bob"):
            self.relay.deliver(name, "morning", "2026-10-06", "标题", "内容")
        self.assertEqual([r[0] for r in self.sent[-2:]], ["alice@example.com", "bob@example.com"])
        self.assertNotEqual(self.sent[-1][3], self.sent[-2][3])
        self.relay.deliver("alice", "morning", "2026-10-06", "标题", "内容")
        self.assertEqual(len(self.sent), 4)
        with self.assertRaises(ValueError): self.relay.deliver("alice", "morning", "2026-10-06", "标题", "changed")

    def test_uncertain_result_never_retries(self):
        self.verified("alice")
        attempts = []
        def fail(*args):
            attempts.append(args)
            raise TimeoutError()
        self.relay.send = fail
        self.assertEqual(self.relay.deliver("alice", "evening", "2026-10-06", "标题", "内容")["status"], "unknown")
        self.relay.deliver("alice", "evening", "2026-10-06", "标题", "内容")
        self.assertEqual(len(attempts), 1)

    def test_code_is_single_use_and_attempts_bounded(self):
        self.relay.request_verification("alice", "alice@example.com")
        code = re.search(r"\d{6}", self.sent[-1][2])[0]
        for _ in range(5):
            with self.assertRaises(ValueError): self.relay.verify("alice", "wrong")
        with self.assertRaises(ValueError): self.relay.verify("alice", code)
        self.verified("bob")
        code = re.search(r"\d{6}", self.sent[-1][2])[0]
        with self.assertRaises(ValueError): self.relay.verify("bob", code)
