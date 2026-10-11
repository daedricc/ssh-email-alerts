# Sandbox verification

Verified on October 11, 2026 using Python 3.9.6 on macOS.

## Environment

The process-level sandbox creates a temporary configuration, SQLite state directory, synthetic journal executable, and loopback-only SMTP server. No production server, real journal, real credential, or external email recipient is used. Temporary test certificates are generated with OpenSSL and trusted only inside test child processes. TLS certificate and hostname verification remain enabled.

## Results

All 13 tests pass with:

```bash
python3 -m unittest discover -s tests -v
```

Verified behaviors:

- Successful SSH parsing, including IPv6, and rejection of invalid IP addresses.
- First-run baseline without historical email alerts.
- Persistent journal cursor and email queue across separate program processes.
- English and Turkish messages; UTC and Istanbul timestamp formatting.
- Configurable threshold and counting window, with cooldown suppression.
- Implicit TLS and STARTTLS email delivery, plus sender display name.
- Delivery to multiple SMTP recipients.
- Temporary SMTP rejection preserves the queue; a later run delivers it.
- Partial recipient rejection preserves the message for retry, with documented duplicate risk.
- Queued email can be delivered even if the current journal query fails.
- Untrusted TLS certificates are rejected and credentials are not printed.
- Journal failure returns a nonzero status in `--once` mode without printing journal stderr.
- Configuration validation sends nothing, creates no state, and rejects invalid settings.
- A 50-record batch generates a log-volume alert.
- Private state directory and database permissions in program processes.

The sample configuration also passes `--check-config`. This confirms syntax, not SMTP credentials or provider acceptance.

## Fixes made during verification

`--once` previously returned success after a journal or email failure. It now returns status 1. Journal failures previously prevented pending email delivery; the two steps are now independent. Partial SMTP recipient rejection is now treated as a delivery failure so the queue is retained.

## Verification boundaries

This is a process-level sandbox, not a Linux virtual machine or container. Real Linux systemd startup, distribution-specific SSH journal routing, external SMTP provider behavior, and inbox delivery have not been verified for this standalone project. The integration tests replace the journal command with synthetic input. A Linux installation smoke test is still needed before claiming complete systemd deployment validation.

The 60-second retry delay in continuous service mode is not measured by this suite; delivery retry is verified through separate program runs. Extended outages, queue growth, journal cursor recovery, and traffic beyond the 50-record limit remain documented limitations.
