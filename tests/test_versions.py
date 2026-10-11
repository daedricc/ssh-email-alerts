import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('version_alerts', ROOT / 'ssh_email_alerts.py')
alerts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(alerts)


class VersionTests(unittest.TestCase):
    def test_version_requires_no_configuration(self):
        result = subprocess.run([sys.executable, str(ROOT / 'ssh_email_alerts.py'), '--version'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn(alerts.VERSION, result.stdout)

    def test_numeric_comparison(self):
        self.assertGreater(alerts.version_tuple('v1.10.0'), alerts.version_tuple('1.9.0'))
        for invalid in ('v1.0.0-beta', '../1.0.0', '1.0', '01.0.0'):
            with self.assertRaises(ValueError):
                alerts.version_tuple(invalid)

    def test_latest_release_uses_fixed_link_and_timeout(self):
        response = io.BytesIO(json.dumps({'tag_name': 'v1.1.0', 'html_url': 'https://untrusted.example'}).encode())
        with patch.object(alerts.urllib.request, 'urlopen', return_value=response) as fetch:
            version, url = alerts.latest_release()
        self.assertEqual(version, '1.1.0')
        self.assertEqual(url, alerts.RELEASE_URL + '/tag/v1.1.0')
        self.assertEqual(fetch.call_args.kwargs['timeout'], 10)

    def test_prerelease_and_oversized_response_rejected(self):
        for data in (json.dumps({'tag_name': 'v1.1.0', 'prerelease': True}).encode(), b'x' * 65537):
            with patch.object(alerts.urllib.request, 'urlopen', return_value=io.BytesIO(data)), self.assertRaises(ValueError):
                alerts.latest_release()

    def test_update_command_never_reads_smtp_or_sends_email(self):
        for latest in ('1.0.0', '1.1.0'):
            with patch.object(sys, 'argv', ['alerts', '--check-update']), patch.object(alerts, 'latest_release', return_value=(latest, alerts.RELEASE_URL)), patch.object(alerts, 'settings') as settings, patch.object(alerts, 'send') as send, patch('sys.stdout', new_callable=io.StringIO) as output:
                alerts.main()
                settings.assert_not_called()
                send.assert_not_called()
                self.assertIn(latest, output.getvalue())

    def test_failed_check_exits_nonzero_without_details(self):
        with patch.object(sys, 'argv', ['alerts', '--check-update']), patch.object(alerts, 'latest_release', side_effect=RuntimeError('private detail')), patch('sys.stdout', new_callable=io.StringIO) as output, self.assertRaises(SystemExit) as result:
            alerts.main()
        self.assertEqual(result.exception.code, 1)
        self.assertNotIn('private detail', output.getvalue())
