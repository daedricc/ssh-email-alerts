import importlib.util
from pathlib import Path
import tempfile
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


if __name__ == '__main__':
    unittest.main()
