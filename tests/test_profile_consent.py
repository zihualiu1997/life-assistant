"""Managed consent must guard newly registered profile/data tools as well."""
import contextlib
import io
import json
import unittest
from unittest.mock import patch

from life_proactive import cli


class ProfileConsentTest(unittest.TestCase):
    def test_new_tools_do_not_open_store_without_managed_memory_consent(self):
        for action in ('profile', 'knowledge', 'preferences'):
            with self.subTest(action=action), patch('sys.argv', ['life-proactive', '--config', 'fixture.json', action]), \
                 patch.object(cli, 'load_config', return_value={'managed': True, 'memory_allowed': False}), \
                 patch.object(cli, 'Store') as store, contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(), 0)
                self.assertEqual(json.loads(output.getvalue())['reason'], 'memory_consent_required')
                store.assert_not_called()
