import importlib.util
from pathlib import Path
import tempfile
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('alerts', Path(__file__).parents[1] / 'ssh_email_alerts.py')
alerts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(alerts)


class AlertsTests(unittest.TestCase):
    def test_login_fields(self):
        self.assertEqual(alerts.classify('Accepted publickey for alice from 2001:db8::1 port 42'),
                         ('success', '2001:db8::1', 'alice', 'publickey'))
        self.assertEqual(alerts.classify('Failed password for invalid user alice from 192.0.2.1 port 42'),
                         ('failure', '192.0.2.1', '', ''))
        self.assertIsNone(alerts.classify('Accepted publickey for alice from invalid port 42'))

    def test_failed_delivery_keeps_queue_and_retry_removes_it(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = alerts.database(Path(directory))
            with conn:
                alerts.enqueue(conn, 'event', 'subject', 'body')
                alerts.enqueue(conn, 'event', 'subject', 'body')
            with patch.object(alerts, 'send', side_effect=RuntimeError('private detail')):
                self.assertFalse(alerts.deliver(conn))
            self.assertEqual(conn.execute('SELECT count(*) FROM queue').fetchone()[0], 1)
            with patch.object(alerts, 'send') as sender:
                self.assertTrue(alerts.deliver(conn))
                sender.assert_called_once_with('subject', 'body')
            self.assertEqual(conn.execute('SELECT count(*) FROM queue').fetchone()[0], 0)
            conn.close()

    def test_language_timezone_and_failure_threshold(self):
        for language, timezone, expected, offset in (
            ('en', 'UTC', 'Successful SSH login', '+00:00'),
            ('tr', 'Europe/Istanbul', 'Başarılı SSH girişi', '+03:00'),
        ):
            with self.subTest(language=language), tempfile.TemporaryDirectory() as directory:
                config = Path(directory) / 'config.env'
                config.write_text('SMTP_USER=example\nSMTP_PASSWORD=YOUR_PASSWORD\nEMAIL_FROM=alerts@example.com\nEMAIL_TO=you@example.com\n'
                                  + f'LANGUAGE={language}\nTIMEZONE={timezone}\n')
                with patch.object(alerts, 'CONFIG', str(config)):
                    conn = alerts.database(Path(directory))
                    entries = [{'__CURSOR': 'baseline', 'MESSAGE': ''}]
                    def journal(*args, **kwargs):
                        return SimpleNamespace(returncode=0, stdout='\n'.join(json.dumps(e) for e in entries))
                    with patch.object(alerts.subprocess, 'run', side_effect=journal), patch.object(alerts.time, 'time', return_value=1800000000):
                        alerts.poll(conn)
                        self.assertEqual(conn.execute('SELECT count(*) FROM queue').fetchone()[0], 0)
                        entries[:] = [{'__CURSOR': 'login', '__REALTIME_TIMESTAMP': '1800000000000000',
                                       'MESSAGE': 'Accepted publickey for alice from 192.0.2.1 port 42'}]
                        alerts.poll(conn)
                        subject, body = conn.execute('SELECT subject, body FROM queue').fetchone()
                        self.assertIn(expected, subject)
                        self.assertIn(offset, body)
                        entries[:] = [{'__CURSOR': f'failed{i}', '__REALTIME_TIMESTAMP': '1800000000000000',
                                       'MESSAGE': 'Failed password for alice from 192.0.2.2 port 42'} for i in range(5)]
                        alerts.poll(conn)
                        self.assertEqual(conn.execute('SELECT count(*) FROM queue').fetchone()[0], 2)
                        entries[:] = [{'__CURSOR': 'another', '__REALTIME_TIMESTAMP': '1800000000000000',
                                       'MESSAGE': 'Failed password for alice from 192.0.2.2 port 42'}]
                        alerts.poll(conn)
                        self.assertEqual(conn.execute('SELECT count(*) FROM queue').fetchone()[0], 2)
                    conn.close()

    def test_invalid_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.env'
            base = 'SMTP_USER=example\nSMTP_PASSWORD=YOUR_PASSWORD\nEMAIL_FROM=alerts@example.com\nEMAIL_TO=you@example.com\n'
            for invalid in ('LANGUAGE=xx', 'TIMEZONE=Invalid/Zone', 'SMTP_PORT=0', 'SMTP_SECURITY=none', 'POLL_INTERVAL=0', 'FAILURE_WINDOW=-1', 'FAILURE_THRESHOLD=x', 'ALERT_COOLDOWN=86401', 'EMAIL_TO=bad-address', 'EMAIL_FROM=bad-address'):
                config.write_text(base + invalid + '\n')
                with patch.object(alerts, 'CONFIG', str(config)), self.assertRaises(Exception):
                    alerts.settings()


if __name__ == '__main__':
    unittest.main()
