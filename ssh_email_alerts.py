#!/usr/bin/env python3
"""SSH email notifications from the systemd journal; never prints logs or secrets."""
import argparse
import datetime
import ipaddress
import json
import os
from pathlib import Path
import re
import smtplib
import sqlite3
import ssl
import subprocess
import time
from email.message import EmailMessage
from email.utils import formataddr, parseaddr, getaddresses
from zoneinfo import ZoneInfo

CONFIG = os.environ.get('SSH_ALERT_CONFIG', '/etc/ssh-email-alerts/config.env')
INTERVAL = 15
WINDOW = 600
THRESHOLD = 5
COOLDOWN = 600
MESSAGES = {
    'en': {
        'gap_subject': '[SSH Alert] High SSH log volume',
        'gap_body': '50 SSH records were seen in one check. Some records may have been skipped; review the server logs.',
        'success_subject': '[SSH Alert] Successful SSH login',
        'success_body': 'SSH login detected. Timezone: {timezone}.',
        'login': '{time} | User: {user} | Source IP: {source} | Method: {method}',
        'failure_subject': '[SSH Alert] Repeated SSH failures',
        'failure_body': 'Source IP: {source}\nAt least {count} failure indicators in the last {window} seconds.\nThis alert does not block IPs. Alerts for this source are muted for {cooldown} seconds.\nOne connection may produce multiple failure indicators.',
        'test_subject': '[SSH Alert] Email test',
        'test_body': 'SMTP test message; this is not an attack alert.\nSuccessful logins are queued approximately every {interval} seconds; the failure threshold is {threshold} indicators in {window} seconds.\nTimezone: {timezone}.',
        'delivery_failed': 'Email delivery failed; queued for retry.',
        'delivered': 'SSH notification accepted by SMTP server.',
        'test_failed': 'Test email failed; secret/error details suppressed.',
        'test_ok': 'Test email accepted by SMTP server; inbox delivery not yet confirmed.',
        'config_ok': 'Configuration is valid; no email sent.',
        'config_failed': 'Configuration could not be loaded; details suppressed.',
        'check_failed': 'SSH notification check failed; retrying without exposing log details.',
    },
    'tr': {
        'gap_subject': '[SSH Alert] SSH kayıt yoğunluğu',
        'gap_body': 'Bir kontrolde 50 SSH kaydı görüldü. Bazı kayıtlar atlanmış olabilir; sunucu günlüklerini inceleyin.',
        'success_subject': '[SSH Alert] Başarılı SSH girişi',
        'success_body': 'SSH giriş kaydı algılandı. Saat dilimi: {timezone}.',
        'login': '{time} | Kullanıcı: {user} | Kaynak IP: {source} | Yöntem: {method}',
        'failure_subject': '[SSH Alert] Tekrarlanan SSH başarısızlığı',
        'failure_body': 'Kaynak IP: {source}\nSon {window} saniyede en az {count} başarısız SSH göstergesi.\nBu bildirim IP engellemez. Aynı kaynak için {cooldown} saniye tekrar bildirilmez.\nBir bağlantı birden fazla başarısızlık göstergesi üretebilir.',
        'test_subject': '[SSH Alert] E-posta testi',
        'test_body': 'SMTP test iletisi; bu gerçek bir saldırı bildirimi değildir.\nBaşarılı girişler yaklaşık {interval} saniyede kuyruğa alınır; başarısızlık eşiği {window} saniyede {threshold} göstergedir.\nSaat dilimi: {timezone}.',
        'delivery_failed': 'E-posta gönderilemedi; tekrar denemek için kuyrukta tutuluyor.',
        'delivered': 'SSH bildirimi SMTP sunucusu tarafından kabul edildi.',
        'test_failed': 'Test iletisi gönderilemedi; gizli bilgiler ve hata ayrıntıları gösterilmiyor.',
        'test_ok': 'Test iletisi SMTP sunucusu tarafından kabul edildi; gelen kutusuna teslim henüz doğrulanmadı.',
        'config_ok': 'Ayarlar geçerli; e-posta gönderilmedi.',
        'config_failed': 'Ayarlar yüklenemedi; ayrıntılar gösterilmiyor.',
        'check_failed': 'SSH kontrolü başarısız; günlük ayrıntıları gösterilmeden tekrar denenecek.',
    },
}
LANGUAGE = 'en'


def message(key, **values):
    return MESSAGES[LANGUAGE][key].format(**values)


def settings():
    global LANGUAGE, INTERVAL, WINDOW, THRESHOLD, COOLDOWN
    values = {}
    for line in Path(CONFIG).read_text().splitlines():
        if line.strip() and not line.lstrip().startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip().strip('\"\'')
    if not all(values.get(key) for key in ('SMTP_PASSWORD', 'SMTP_USER', 'EMAIL_FROM', 'EMAIL_TO')):
        raise ValueError('Missing mail settings')
    language = values.get('LANGUAGE', 'en').lower()
    if language not in MESSAGES:
        raise ValueError('Unsupported language')
    ZoneInfo(values.get('TIMEZONE', 'UTC'))
    port = int(values.get('SMTP_PORT', '465'))
    if not 1 <= port <= 65535:
        raise ValueError('Invalid SMTP port')
    if values.get('SMTP_SECURITY', 'auto') not in ('auto', 'ssl', 'starttls'):
        raise ValueError('Invalid SMTP security')
    for key in ('EMAIL_FROM', 'EMAIL_TO', 'SENDER_NAME', 'SMTP_HOST'):
        if any(c in values.get(key, '') for c in ('\r', '\n')):
            raise ValueError('Invalid header or host')
    for address in [values['EMAIL_FROM'], *values['EMAIL_TO'].split(',')]:
        parsed = getaddresses([address])
        if len(parsed) != 1 or not re.fullmatch(r'[^\s@<>;,]+@[^\s@<>;,]+', parsed[0][1]):
            raise ValueError('Invalid email address')
    numbers = {}
    for key, default in (('POLL_INTERVAL', 15), ('FAILURE_WINDOW', 600),
                         ('FAILURE_THRESHOLD', 5), ('ALERT_COOLDOWN', 600)):
        value = int(values.get(key, default))
        if not 1 <= value <= 86400:
            raise ValueError('Invalid positive numeric setting')
        numbers[key] = value
    LANGUAGE = language
    INTERVAL = numbers['POLL_INTERVAL']
    WINDOW = numbers['FAILURE_WINDOW']
    THRESHOLD = numbers['FAILURE_THRESHOLD']
    COOLDOWN = numbers['ALERT_COOLDOWN']
    return values


def send(subject, body):
    values = settings()
    msg = EmailMessage()
    msg['From'] = (formataddr((values['SENDER_NAME'], parseaddr(values['EMAIL_FROM'])[1]))
                   if values.get('SENDER_NAME') else values['EMAIL_FROM'])
    msg['To'] = values['EMAIL_TO']
    msg['Subject'] = subject
    msg.set_content(body)
    port = int(values.get('SMTP_PORT', '465'))
    context = ssl.create_default_context()
    host = values.get('SMTP_HOST', 'smtp.resend.com')
    security = values.get('SMTP_SECURITY', 'auto')
    use_ssl = security == 'ssl' or (security == 'auto' and port == 465)
    connection = (smtplib.SMTP_SSL(host, port, timeout=20, context=context)
                  if use_ssl else smtplib.SMTP(host, port, timeout=20))
    with connection as client:
        if not use_ssl:
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        client.login(values['SMTP_USER'], values['SMTP_PASSWORD'])
        refused = client.send_message(msg)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)


def classify(message):
    # Only structured login fields go into emails, never full journal messages.
    success = re.search(r'Accepted (\S+) for (\S+) from (\S+) port (\d+)', message)
    failed = re.search(r'Failed \S+ for (?:invalid user )?\S+ from (\S+) port \d+', message)
    # Count final invalid-user disconnects when no Failed message is emitted.
    invalid = re.search(r'Connection (?:closed|reset) by invalid user \S+ (\S+) port \d+', message)
    match = success or failed or invalid
    if not match:
        return None
    source = match.group(3) if success else match.group(1)
    try:
        source = str(ipaddress.ip_address(source))
    except ValueError:
        return None
    if success:
        return ('success', source, success.group(2)[:64], success.group(1)[:32])
    return ('failure', source, '', '')


def database(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    conn = sqlite3.connect(path / 'state.sqlite3')
    conn.executescript('''
      CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
      CREATE TABLE IF NOT EXISTS failures (source TEXT, at REAL);
      CREATE TABLE IF NOT EXISTS cooldown (source TEXT PRIMARY KEY, at REAL);
      CREATE TABLE IF NOT EXISTS queue (id TEXT PRIMARY KEY, subject TEXT, body TEXT);
    ''')
    return conn


def enqueue(conn, identifier, subject, body):
    conn.execute('INSERT OR IGNORE INTO queue VALUES (?, ?, ?)', (identifier, subject, body))


def poll(conn):
    values = settings()
    timezone = values.get('TIMEZONE', 'UTC')
    display_timezone = ZoneInfo(timezone)
    previous = conn.execute("SELECT value FROM state WHERE key='cursor'").fetchone()
    args = ['journalctl', '-u', 'ssh.service', '-u', 'sshd.service', '-n', '50',
            '-o', 'json', '--no-pager']
    if previous:
        args.extend(['--after-cursor', previous[0]])
    result = subprocess.run(args, capture_output=True, text=True, timeout=10)
    if result.returncode:
        # Do not print stderr, which could contain unexpected sensitive content.
        raise RuntimeError('Journal query failed')
    entries = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
    if not entries:
        return
    cursor = entries[-1]['__CURSOR']
    now = time.time()
    with conn:
        if previous:
            if len(entries) == 50:
                enqueue(conn, 'gap-' + cursor, message('gap_subject'), message('gap_body'))
            successes = []
            conn.execute('DELETE FROM failures WHERE at < ?', (now - WINDOW,))
            conn.execute('DELETE FROM cooldown WHERE at <= ?', (now - COOLDOWN,))
            for event in entries:
                parsed = classify(event.get('MESSAGE', ''))
                if not parsed:
                    continue
                kind, source, user, method = parsed
                timestamp = int(event.get('__REALTIME_TIMESTAMP', int(now * 1e6))) / 1e6
                if kind == 'success':
                    when = datetime.datetime.fromtimestamp(timestamp, display_timezone)
                    successes.append(message('login', time=when.isoformat(), user=user, source=source, method=method))
                elif timestamp >= now - WINDOW:
                    conn.execute('INSERT INTO failures VALUES (?, ?)', (source, timestamp))
                    count = conn.execute('SELECT count(*) FROM failures WHERE source=? AND at>=?',
                                         (source, now - WINDOW)).fetchone()[0]
                    cooling = conn.execute('SELECT 1 FROM cooldown WHERE source=?', (source,)).fetchone()
                    if count >= THRESHOLD and not cooling:
                        enqueue(conn, 'failure-' + event['__CURSOR'], message('failure_subject'),
                                message('failure_body', source=source, count=count, window=WINDOW, cooldown=COOLDOWN))
                        conn.execute('INSERT OR REPLACE INTO cooldown VALUES (?, ?)', (source, now))
            if successes:
                enqueue(conn, 'success-' + cursor, message('success_subject'),
                        message('success_body', timezone=timezone) + '\n\n' + '\n'.join(successes))
        # First start establishes a baseline; historical logins are not emailed.
        conn.execute("INSERT OR REPLACE INTO state VALUES ('cursor', ?)", (cursor,))


def deliver(conn):
    rows = conn.execute('SELECT id, subject, body FROM queue ORDER BY rowid LIMIT 5').fetchall()
    for identifier, subject, body in rows:
        try:
            send(subject, body)
        except Exception:
            print(message('delivery_failed'), flush=True)
            return False
        with conn:
            conn.execute('DELETE FROM queue WHERE id=?', (identifier,))
        print(message('delivered'), flush=True)
    return True


def main():
    global CONFIG
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default=CONFIG)
    parser.add_argument('--check-config', action='store_true')
    parser.add_argument('--test-email', action='store_true')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--state-dir', default='/var/lib/ssh-email-alerts')
    args = parser.parse_args()
    CONFIG = args.config
    if args.check_config:
        try:
            settings()
        except Exception:
            print(message('config_failed'))
            raise SystemExit(1)
        print(message('config_ok'))
        return
    if args.test_email:
        try:
            values = settings()
            send(message('test_subject'), message('test_body', timezone=values.get('TIMEZONE', 'UTC'), interval=INTERVAL, threshold=THRESHOLD, window=WINDOW))
        except Exception:
            print(message('test_failed'))
            raise SystemExit(1)
        print(message('test_ok'))
        return
    try:
        settings()
    except Exception:
        print(message('config_failed'))
        raise SystemExit(1)
    try:
        conn = database(Path(args.state_dir))
    except Exception:
        print(message('check_failed'))
        raise SystemExit(1)
    retry_at = 0
    try:
        while True:
            failed = False
            try:
                poll(conn)
            except Exception:
                print(message('check_failed'), flush=True)
                failed = True
            if time.time() >= retry_at:
                try:
                    delivered = deliver(conn)
                    retry_at = 0 if delivered else time.time() + 60
                    failed = failed or not delivered
                except Exception:
                    print(message('check_failed'), flush=True)
                    failed = True
                    retry_at = time.time() + 60
            if args.once:
                if failed:
                    raise SystemExit(1)
                return
            time.sleep(INTERVAL)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
