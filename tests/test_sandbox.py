"""Offline process-level sandbox with synthetic journal data and local TLS SMTP."""
from email import policy
from email.parser import BytesParser
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LocalSMTP:
    def __init__(self, certificate, key, implicit_tls):
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(certificate, key)
        self.implicit_tls = implicit_tls
        self.messages = []
        self.reject = False
        self.errors = []
        self.refuse_recipient = None
        self.recipients = []
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.port = self.listener.getsockname()[1]
        self.listener.listen()
        self.listener.settimeout(0.2)
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stopping.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self.handle(connection)
            except (ssl.SSLError, ConnectionError):
                # A client rejecting the untrusted certificate is expected.
                pass
            except Exception as error:
                self.errors.append(type(error).__name__)
            finally:
                connection.close()

    def handle(self, connection):
        connection.settimeout(5)
        if self.implicit_tls:
            connection = self.context.wrap_socket(connection, server_side=True)
        stream = connection.makefile('rwb')
        def reply(data):
            stream.write(data)
            stream.flush()
        reply(b'220 localhost sandbox\r\n')
        encrypted = self.implicit_tls
        while True:
            line = stream.readline()
            if not line:
                break
            command = line.split(b' ', 1)[0].strip().upper()
            if command == b'EHLO':
                reply(b'250-localhost\r\n' + (b'250-AUTH PLAIN\r\n' if encrypted else b'250-STARTTLS\r\n') + b'250 OK\r\n')
            elif command == b'STARTTLS':
                reply(b'220 Begin TLS\r\n')
                stream.close()
                connection = self.context.wrap_socket(connection, server_side=True)
                stream = connection.makefile('rwb')
                encrypted = True
            elif command == b'AUTH':
                if not encrypted:
                    raise AssertionError('Credentials sent before TLS')
                reply(b'235 Authenticated\r\n')
            elif command == b'RCPT':
                address = line.decode().split('<', 1)[-1].split('>', 1)[0]
                self.recipients.append(address)
                reply(b'550 Rejected\r\n' if address == self.refuse_recipient else b'250 OK\r\n')
            elif command in (b'MAIL', b'RSET'):
                reply(b'250 OK\r\n')
            elif command == b'DATA':
                reply(b'354 Send data\r\n')
                data = []
                while True:
                    item = stream.readline()
                    if item == b'.\r\n':
                        break
                    if not item:
                        raise ConnectionError('Incomplete message')
                    data.append(item[1:] if item.startswith(b'..') else item)
                if self.reject:
                    reply(b'451 Temporary sandbox failure\r\n')
                else:
                    self.messages.append(BytesParser(policy=policy.default).parsebytes(b''.join(data)))
                    reply(b'250 Accepted\r\n')
            elif command == b'QUIT':
                reply(b'221 Bye\r\n')
                break
            else:
                reply(b'502 Unsupported\r\n')
        stream.close()
        connection.close()

    def close(self):
        self.stopping.set()
        self.listener.close()
        self.thread.join(timeout=6)


class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ssh-alert-sandbox-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.key = self.path / 'key.pem'
        self.cert = self.path / 'cert.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-keyout', str(self.key), '-out', str(self.cert), '-days', '1',
                        '-subj', '/CN=localhost'], check=True, capture_output=True)
        self.events = self.path / 'events.json'
        self.events.write_text('[]')
        self.bin = self.path / 'bin'
        self.bin.mkdir()
        journal = self.bin / 'journalctl'
        journal.write_text('#!' + sys.executable + '\n' + '''import json, os, sys
from pathlib import Path
if Path(os.environ['SANDBOX_JOURNAL_ERROR']).exists():
    print('synthetic private error', file=sys.stderr)
    sys.exit(1)
entries = json.loads(Path(os.environ['SANDBOX_EVENTS']).read_text())
if '--after-cursor' in sys.argv:
    cursor = sys.argv[sys.argv.index('--after-cursor') + 1]
    positions = [i for i, event in enumerate(entries) if event['__CURSOR'] == cursor]
    if not positions:
        sys.exit(1)
    entries = entries[positions[-1] + 1:]
for entry in entries[-50:]:
    print(json.dumps(entry))
''')
        journal.chmod(0o700)
        self.config = self.path / 'config.env'
        self.state = self.path / 'state'
        self.env = os.environ.copy()
        self.env.update(PATH=str(self.bin) + os.pathsep + self.env['PATH'],
                        SANDBOX_EVENTS=str(self.events),
                        SANDBOX_JOURNAL_ERROR=str(self.path / 'journal-error'),
                        SSL_CERT_FILE=str(self.cert))

    def configure(self, server, language='en', timezone='UTC'):
        self.config.write_text(f'SMTP_HOST=localhost\nSMTP_PORT={server.port}\n'
                               f'SMTP_SECURITY={"ssl" if server.implicit_tls else "starttls"}\n'
                               'SMTP_USER=sandbox\nSMTP_PASSWORD=YOUR_TEST_PASSWORD\n'
                               'EMAIL_FROM=alerts@example.com\nEMAIL_TO=recipient@example.com\n'
                               f'SENDER_NAME=Sandbox Alerts\nLANGUAGE={language}\nTIMEZONE={timezone}\n')

    def run_cli(self, *arguments, env=None):
        # Add the ephemeral test certificate to the client trust store only in
        # this child process. Certificate and hostname verification remain on.
        bootstrap = """import os, runpy, ssl, sys
original = ssl.create_default_context
def sandbox_context(*args, **kwargs):
    context = original(*args, **kwargs)
    certificate = os.environ.get('SSL_CERT_FILE')
    if certificate and certificate != '/dev/null':
        context.load_verify_locations(cafile=certificate)
    return context
ssl.create_default_context = sandbox_context
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
"""
        return subprocess.run([sys.executable, '-c', bootstrap, str(ROOT / 'ssh_email_alerts.py'),
                               '--config', str(self.config), '--state-dir', str(self.state), *arguments],
                              capture_output=True, text=True, timeout=15, env=env or self.env)

    def add_events(self, records):
        events = json.loads(self.events.read_text())
        for record in records:
            events.append({'__CURSOR': str(len(events)), '__REALTIME_TIMESTAMP': str(int(time.time()*1e6)),
                           'MESSAGE': record})
        self.events.write_text(json.dumps(events))

    def server(self, implicit_tls):
        server = LocalSMTP(self.cert, self.key, implicit_tls)
        self.addCleanup(server.close)
        return server

    def test_ssl_and_starttls_cli_test_email(self):
        for implicit in (True, False):
            with self.subTest(implicit_tls=implicit):
                server = self.server(implicit)
                self.configure(server)
                result = self.run_cli('--test-email')
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertEqual(len(server.messages), 1)
                self.assertEqual(server.messages[0]['Subject'], '[SSH Alert] Email test')
                self.assertEqual(server.messages[0]['From'], 'Sandbox Alerts <alerts@example.com>')
                self.assertFalse(server.errors)

    def test_baseline_login_failures_cooldown_persistence_and_retry(self):
        server = self.server(True)
        self.configure(server, 'tr', 'Europe/Istanbul')
        self.add_events(['Accepted publickey for alice from 192.0.2.1 port 42'])
        self.assertEqual(self.run_cli('--once').returncode, 0)
        self.assertEqual(len(server.messages), 0)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.state / 'state.sqlite3').stat().st_mode & 0o777, 0o600)
        self.add_events(['Accepted publickey for bob from 2001:db8::1 port 42'])
        self.assertEqual(self.run_cli('--once').returncode, 0)
        self.assertEqual(len(server.messages), 1)
        self.assertIn('Başarılı', str(server.messages[-1]['Subject']))
        self.assertIn('+03:00', server.messages[-1].get_content())
        self.assertNotIn('alice', server.messages[-1].get_content())
        server.reject = True
        self.add_events(['Failed password for invalid user nobody from 192.0.2.2 port 42'] * 5)
        self.assertEqual(self.run_cli('--once').returncode, 1)
        self.assertEqual(len(server.messages), 1)
        server.reject = False
        self.assertEqual(self.run_cli('--once').returncode, 0)
        self.assertEqual(len(server.messages), 2)
        self.assertIn('başarısızlığı', str(server.messages[-1]['Subject']))
        self.add_events(['Failed password for nobody from 192.0.2.2 port 42'] * 5)
        self.run_cli('--once')
        self.assertEqual(len(server.messages), 2)
        self.assertFalse(server.errors)

    def test_untrusted_certificate_is_rejected(self):
        server = self.server(True)
        self.configure(server)
        env = self.env.copy()
        env['SSL_CERT_FILE'] = '/dev/null'
        result = self.run_cli('--test-email', env=env)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(server.messages), 0)
        self.assertNotIn('YOUR_TEST_PASSWORD', result.stdout + result.stderr)
        self.assertNotIn('Traceback', result.stdout + result.stderr)

    def test_journal_failure_has_nonzero_exit(self):
        server = self.server(True)
        self.configure(server)
        (self.path / 'journal-error').touch()
        result = self.run_cli('--once')
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('synthetic private error', result.stdout + result.stderr)

    def test_config_check_sends_nothing_and_creates_no_state(self):
        server = self.server(True)
        self.configure(server)
        result = self.run_cli('--check-config')
        self.assertEqual(result.returncode, 0)
        self.assertIn('Configuration is valid', result.stdout)
        self.assertFalse(self.state.exists())
        self.assertEqual(server.messages, [])
        with self.config.open('a') as file:
            file.write('FAILURE_THRESHOLD=0\n')
        result = self.run_cli('--check-config')
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('YOUR_TEST_PASSWORD', result.stdout + result.stderr)

    def test_custom_threshold_window_and_multiple_recipients(self):
        server = self.server(False)
        self.configure(server)
        with self.config.open('a') as file:
            file.write('FAILURE_THRESHOLD=2\nFAILURE_WINDOW=120\nALERT_COOLDOWN=60\nPOLL_INTERVAL=3\n'
                       'EMAIL_TO=first@example.com,second@example.com\n')
        self.add_events(['baseline'])
        self.run_cli('--once')
        self.add_events(['Failed password for alice from 192.0.2.3 port 42'])
        self.run_cli('--once')
        self.assertEqual(server.messages, [])
        self.add_events(['Failed password for alice from 192.0.2.3 port 42'])
        self.assertEqual(self.run_cli('--once').returncode, 0)
        self.assertEqual(len(server.messages), 1)
        self.assertIn('last 120 seconds', server.messages[-1].get_content())
        self.assertIn('muted for 60 seconds', server.messages[-1].get_content())
        self.assertEqual(server.recipients, ['first@example.com', 'second@example.com'])

    def test_partial_recipient_rejection_keeps_message_for_retry(self):
        server = self.server(True)
        self.configure(server)
        with self.config.open('a') as file:
            file.write('EMAIL_TO=first@example.com,second@example.com\n')
        self.add_events(['baseline'])
        self.run_cli('--once')
        server.refuse_recipient = 'second@example.com'
        self.add_events(['Accepted publickey for alice from 192.0.2.4 port 42'])
        self.assertEqual(self.run_cli('--once').returncode, 1)
        self.assertEqual(len(server.messages), 1)
        server.refuse_recipient = None
        self.assertEqual(self.run_cli('--once').returncode, 0)
        self.assertEqual(len(server.messages), 2)

    def test_pending_email_delivered_even_when_journal_query_fails(self):
        server = self.server(True)
        self.configure(server)
        self.add_events(['baseline'])
        self.run_cli('--once')
        server.reject = True
        self.add_events(['Accepted publickey for alice from 192.0.2.5 port 42'])
        self.assertEqual(self.run_cli('--once').returncode, 1)
        server.reject = False
        (self.path / 'journal-error').touch()
        self.assertEqual(self.run_cli('--once').returncode, 1)
        self.assertEqual(len(server.messages), 1)

    def test_log_volume_alert(self):
        server = self.server(True)
        self.configure(server)
        self.add_events(['baseline'])
        self.run_cli('--once')
        self.add_events(['unclassified record'] * 50)
        self.assertEqual(self.run_cli('--once').returncode, 0)
        self.assertEqual(server.messages[-1]['Subject'], '[SSH Alert] High SSH log volume')


if __name__ == '__main__':
    unittest.main()
