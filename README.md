# SSH Email Alerts

Email notifications for successful SSH logins and repeated authentication failures on Linux servers. Built with the Python standard library, the systemd journal, and TLS-enabled SMTP. No third-party Python packages required.

## Features

- Checks the latest 50 SSH service records every 15 seconds.
- Reports successful logins with username, source IP, authentication method, and timestamp in your preferred timezone.
- Alerts after 5 failure indicators from the same IP within 10 minutes, with a 10-minute notification cooldown per IP.
- Persists the journal cursor and email queue in SQLite. Failed deliveries are retried after 60 seconds.
- Establishes a baseline on first startup without emailing historical events.
- Configurable SMTP server, TLS mode, sender address/name, and recipient.
- English by default, with Turkish available. Language and timezone are independent.
- Suppresses raw journal messages, SMTP credentials, and exception details in application output.

## Requirements

Python 3.9 or later, Linux with systemd, SSH records under `ssh.service` or `sshd.service`, an SMTP account, and system timezone data (usually the `tzdata` package).

The supplied service runs as root to read the system journal and applies systemd hardening settings.

## Installation

Clone this repository and run the following commands from its directory:

```bash
sudo install -d -m 755 /usr/local/lib/ssh-email-alerts
sudo install -m 644 ssh_email_alerts.py /usr/local/lib/ssh-email-alerts/ssh_email_alerts.py
sudo install -d -m 700 /etc/ssh-email-alerts
sudo install -m 600 .env.example /etc/ssh-email-alerts/config.env
sudoedit /etc/ssh-email-alerts/config.env
sudo install -m 644 systemd/ssh-email-alerts.service /etc/systemd/system/ssh-email-alerts.service
sudo systemctl daemon-reload
sudo python3 /usr/local/lib/ssh-email-alerts/ssh_email_alerts.py --test-email
sudo systemctl enable --now ssh-email-alerts.service
```

Set your SMTP credentials, sender, and recipient before sending a test email or starting the service. The sample uses Resend; for Resend, use your API key as `SMTP_PASSWORD`, `resend` as `SMTP_USER`, and a sender on your verified domain. Other SMTP providers are supported with their own settings.

The configuration uses simple `KEY=value` lines. Shell commands, variable expansion, and multiline values are not supported. Keep the real configuration private; never commit it to GitHub.

## Configuration

Each installation has its own configuration file. The example contains placeholders only.

| Setting | Purpose | Example / default |
|---|---|---|
| `SMTP_HOST` | SMTP server hostname | `smtp.example.com`; default `smtp.resend.com` |
| `SMTP_PORT` | SMTP server port | `465` (default) or `587` |
| `SMTP_SECURITY` | TLS connection mode | `auto` (default), `ssl`, `starttls` |
| `SMTP_USER` | SMTP username | `YOUR_SMTP_USER` |
| `SMTP_PASSWORD` | SMTP password or provider app password | `YOUR_SMTP_PASSWORD` |
| `EMAIL_FROM` | Sender email address | `alerts@example.com` |
| `SENDER_NAME` | Optional sender display name | `SSH Alerts` |
| `EMAIL_TO` | Recipient email address | `you@example.com` |
| `LANGUAGE` | Email and runtime message language | `en` (default) or `tr` |
| `TIMEZONE` | IANA timezone for login timestamps | `UTC` (default), `Europe/Istanbul` |

`SMTP_SECURITY=auto` uses implicit TLS on port 465 and mandatory STARTTLS on other ports. Set `ssl` or `starttls` explicitly for a custom port. Certificate verification is always enabled. Use a sender address your SMTP provider permits.

For example:

```ini
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_SECURITY=starttls
SMTP_USER=YOUR_SMTP_USER
SMTP_PASSWORD=YOUR_SMTP_PASSWORD
EMAIL_FROM=alerts@example.com
SENDER_NAME=SSH Alerts
EMAIL_TO=you@example.com
LANGUAGE=en
TIMEZONE=Europe/Berlin
```

For Turkish messages and Istanbul time, set `LANGUAGE=tr` and `TIMEZONE=Europe/Istanbul`. Timezones apply daylight-saving rules automatically where applicable. Unsupported languages, invalid timezones, invalid ports, and unsupported TLS modes are rejected.

Configuration is reread during checks and email delivery. Queued messages retain the language and timestamps used when they were created. CLI help is in English.

## Status and command-line options

```bash
sudo systemctl is-active ssh-email-alerts.service
sudo journalctl -u ssh-email-alerts.service -n 50 --no-pager
python3 ssh_email_alerts.py --help
```

- `--config PATH`: configuration file, default `/etc/ssh-email-alerts/config.env`. The `SSH_ALERT_CONFIG` environment variable is also supported.
- `--state-dir PATH`: persistent state directory, default `/var/lib/ssh-email-alerts`.
- `--once`: perform one check and delivery attempt.
- `--test-email`: send a test message and exit.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Tests cover login parsing, queue retention and retry, first-start baseline behavior, language and timezone formatting, failure thresholds, notification cooldowns, and invalid configuration.

## Limitations

This tool sends notifications; it does not block IPs. One connection can produce multiple failure indicators, so the threshold is not a count of unique connections.

Only the newest 50 journal records are read per check. High traffic or a long interruption can cause intermediate events to be skipped; a log-volume alert is queued when a check reaches this limit. If journal records are removed or the cursor becomes invalid, the service reports a check failure; automatic cursor recovery is not implemented.

SMTP acceptance does not guarantee inbox delivery. A process interruption after SMTP acceptance may cause a duplicate message. The queue can grow during extended SMTP outages. An attacker with root access can alter the service and logs.

## License

No license has been selected yet. This repository currently grants no explicit open-source license.
