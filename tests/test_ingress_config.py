import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from life_fleet.ingress import configure_ingress
from life_fleet.store import Fleet


class IngressConfigTest(unittest.TestCase):
    def test_local_secret_preserves_operator_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            fleet = Fleet(root)
            target = fleet.root / 'operator.json'
            original = {'api_key': 'fictional-model', 'smtp': {'password': 'fictional-mail'}}
            target.write_text(json.dumps(original))
            images = {'caddy_image': 'caddy@sha256:' + 'a'*64,
                      'tunnel_image': 'cloudflare/cloudflared@sha256:' + 'b'*64}
            token = 'fictional-token-' + 'x'*40
            result = configure_ingress(fleet, images, token)
            config = json.loads(target.read_text())
            self.assertEqual(config['smtp'], original['smtp'])
            self.assertEqual(config['api_key'], original['api_key'])
            self.assertNotIn(token, json.dumps(result) + target.read_text())
            secret = Path(config['ingress']['token_file'])
            self.assertEqual(secret.read_text(), token)
            if os.name != 'nt':
                self.assertEqual(secret.stat().st_mode & 0o777, 0o600)
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            before = target.read_bytes()
            with self.assertRaisesRegex(ValueError, 'ingress_exists'):
                configure_ingress(fleet, images, 'y'*40)
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(secret.read_text(), token)

    def test_invalid_input_and_failed_config_leave_no_token(self):
        with tempfile.TemporaryDirectory() as root:
            fleet = Fleet(root)
            target = fleet.root / 'operator.json'
            target.write_text('{}')
            images = {'caddy_image': 'caddy@sha256:' + 'a'*64,
                      'tunnel_image': 'cloudflare/cloudflared@sha256:' + 'b'*64}
            for token in ('', 'docker run --token ' + 'a'*40, 'a'*40+'\nmore'):
                with self.assertRaises(ValueError): configure_ingress(fleet, images, token)
            with self.assertRaises(ValueError): configure_ingress(fleet, {'caddy_image':'latest'}, 'a'*40)
            from life_assistant import atomic_write
            def fail_config(path, data):
                if path == target: raise OSError('fictional disk failure')
                atomic_write(path, data)
            with patch('life_fleet.ingress.atomic_write', side_effect=fail_config):
                with self.assertRaises(OSError): configure_ingress(fleet, images, 'a'*40)
            self.assertEqual(target.read_text(), '{}')
            self.assertFalse((Path(root)/'tunnel-token').exists())

