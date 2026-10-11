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

CONFIG = os.environ.get('SSH_ALERT_CONFIG', '/etc/ssh-email-alerts/config.env')
INTERVAL = 15
WINDOW = 600
THRESHOLD = 5
DISPLAY_TIMEZONE = datetime.timezone.utc


def settings():
    values = {}
    for line in Path(CONFIG).read_text().splitlines():
        if line.strip() and not line.lstrip().startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip().strip('\"\'')
    if not all(values.get(key) for key in ('SMTP_PASSWORD', 'SMTP_USER', 'EMAIL_FROM', 'EMAIL_TO')):
        raise ValueError('Missing mail settings')
    return values


def send(subject, body):
    values = settings()
    msg = EmailMessage()
    msg['From'] = values['EMAIL_FROM']
    msg['To'] = values['EMAIL_TO']
    msg['Subject'] = subject
    msg.set_content(body)
    port = int(values.get('SMTP_PORT', '465'))
    context = ssl.create_default_context()
    host = values.get('SMTP_HOST', 'smtp.resend.com')
    connection = (smtplib.SMTP_SSL(host, port, timeout=20, context=context)
                  if port == 465 else smtplib.SMTP(host, port, timeout=20))
    with connection as client:
        if port != 465:
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        client.login(values['SMTP_USER'], values['SMTP_PASSWORD'])
        client.send_message(msg)


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
                enqueue(conn, 'gap-' + cursor, '[SSH Alert] SSH kayıt yoğunluğu',
                        'Bir kontrolde 50 SSH kaydı görüldü. İnceleme sınırı nedeniyle bazı '
                        'kayıtlar atlanmış olabilir. Sunucu üzerinde ayrıca inceleme gerekir.')
            successes = []
            conn.execute('DELETE FROM failures WHERE at < ?', (now - WINDOW,))
            conn.execute('DELETE FROM cooldown WHERE at < ?', (now - WINDOW,))
            for event in entries:
                parsed = classify(event.get('MESSAGE', ''))
                if not parsed:
                    continue
                kind, source, user, method = parsed
                timestamp = int(event.get('__REALTIME_TIMESTAMP', int(now * 1e6))) / 1e6
                if kind == 'success':
                    when = datetime.datetime.fromtimestamp(timestamp, DISPLAY_TIMEZONE)
                    successes.append(f'{when.isoformat()} | Kullanıcı: {user} | Kaynak IP: {source} | Yöntem: {method}')
                elif timestamp >= now - WINDOW:
                    conn.execute('INSERT INTO failures VALUES (?, ?)', (source, timestamp))
                    count = conn.execute('SELECT count(*) FROM failures WHERE source=? AND at>=?',
                                         (source, now - WINDOW)).fetchone()[0]
                    cooling = conn.execute('SELECT 1 FROM cooldown WHERE source=?', (source,)).fetchone()
                    if count >= THRESHOLD and not cooling:
                        enqueue(conn, 'failure-' + event['__CURSOR'], '[SSH Alert] Tekrarlanan SSH başarısızlığı',
                                f'Kaynak IP: {source}\nSon 10 dakikada en az {count} başarısız SSH göstergesi.\n'
                                'Bu bildirim IP adresini engellemez. Aynı kaynak için 10 dakika tekrar bildirilmez.\n'
                                'Bazı bağlantılar birden fazla başarısızlık göstergesi üretebilir.')
                        conn.execute('INSERT OR REPLACE INTO cooldown VALUES (?, ?)', (source, now))
            if successes:
                enqueue(conn, 'success-' + cursor, '[SSH Alert] Başarılı SSH girişi',
                        'SSH giriş kaydı algılandı. Zamanlar UTC olarak gösterilir.\n\n' + '\n'.join(successes))
        # First start establishes a baseline; historical logins are not emailed.
        conn.execute("INSERT OR REPLACE INTO state VALUES ('cursor', ?)", (cursor,))


def deliver(conn):
    rows = conn.execute('SELECT id, subject, body FROM queue ORDER BY rowid LIMIT 5').fetchall()
    for identifier, subject, body in rows:
        try:
            send(subject, body)
        except Exception:
            print('Email delivery failed; queued for retry.', flush=True)
            return False
        with conn:
            conn.execute('DELETE FROM queue WHERE id=?', (identifier,))
        print('SSH notification accepted by SMTP server.', flush=True)
    return True


def main():
    global CONFIG
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default=CONFIG)
    parser.add_argument('--test-email', action='store_true')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--state-dir', default='/var/lib/ssh-email-alerts')
    args = parser.parse_args()
    CONFIG = args.config
    if args.test_email:
        try:
            send('[SSH Alert] SSH bildirim testi',
                 'SMTP üzerinden SSH e-posta gönderim testi. Bu ileti gerçek bir saldırı bildirimi değildir.\n'
                 'Başarılı girişler en geç yaklaşık 15 saniyede kuyruğa alınır; başarısızlık eşiği 10 dakikada 5 göstergedir.')
        except Exception:
            print('Test email failed; secret/error details suppressed.')
            raise SystemExit(1)
        print('Test email accepted by SMTP server; inbox delivery not yet confirmed.')
        return
    try:
        settings()
    except Exception:
        print('Configuration could not be loaded; details suppressed.')
        raise SystemExit(1)
    conn = database(Path(args.state_dir))
    retry_at = 0
    while True:
        try:
            poll(conn)
            if time.time() >= retry_at:
                retry_at = 0 if deliver(conn) else time.time() + 60
        except Exception:
            print('SSH notification check failed; retrying without exposing log details.', flush=True)
        if args.once:
            return
        time.sleep(INTERVAL)


if __name__ == '__main__':
    main()
