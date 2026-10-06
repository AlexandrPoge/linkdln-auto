# Automated pipeline

Run the local server, open `http://127.0.0.1:8765/`, and use **Автопилот** to connect a personal Gmail account. Supply a Google **app password**, not the account password; see [Google's instructions](https://support.google.com/accounts/answer/185833). Two-step verification is required and app passwords are unavailable for some accounts. OAuth is not implemented in this version. SMTP and IMAP login are checked without sending a test message.

Choose a factual, reviewed resume PDF (absolute path, at most 5 MB). Enable only the desired checkboxes: candidate digests, mailbox alerts, approved applications, and automatic approval. After connection, the next cycle searches, reads alerts, prepares drafts, sends eligible configured applications, and emails new proposals to the connected account. **Искать сейчас** starts an extra cycle. Passwords never appear in dashboard responses or delivery error logs; settings are saved atomically in Git-ignored `data/automation.json`, owner-only (0600), but not encrypted. Keep the checkout/data directory private.

## Sources and eligibility

Greenhouse/Ashby are company-specific public boards. Himalayas and Remotive are aggregators, not confirmation of employer hiring eligibility. Remotive listings link to and identify the source; its public feed is delayed by 24 hours. A persistent cooldown, including failed attempts/manual runs, limits Remotive to one request every six hours, as recommended in its [official API documentation](https://github.com/remotive-com/remote-jobs-api). The configuration accepts one Remotive query.

The hh adapter uses the [official API](https://api.hh.ru/openapi/redoc), Belarus area 16 and `work_format=REMOTE`, then checks full vacancy details. The live check on this machine returned HTTP 403: the adapter is implemented/tested but hh search is currently unavailable here. The error is shown, not bypassed. Job-alert email intake is a separate path. Search imports are bounded/incomplete and never close missing listings; successful full company-board snapshots can close missing jobs.

Automatic approval is deliberately explicit: only Greenhouse/Ashby, seen in the last 24 hours, a current role match with score at least 75, at least two matched skills, employer-stated remote Belarus/worldwide scope, no senior/office/description-only-remote warning, pristine generated text, and one application email clearly published in the description. Edited drafts belong to the user and are not autoapproved. Aggregator/location-search classifications are excluded. This does not certify legal cross-border hiring eligibility. Other jobs remain in the queue for manual review, approval, and an explicit application address.

Templates mention candidate-provided facts only. The sender attaches the selected PDF and records an application claim before SMTP. Default maximum: 5 attempts per rolling 24 hours, configurable from 1 to 10; uncertain attempts also count. A source/job identity gets one attempt even if imported from multiple queries. Uncertain outcomes are not automatically retried: inspect the mailbox before resolving them. Candidate digests use a separate durable outbox, up to 25 new items per cycle, also without automatic retries after an uncertain result.

## LinkedIn/hh alerts

Enable native job alerts with email delivery to the connected Gmail. The IMAP reader selects INBOX read-only, uses BODY.PEEK, and does not mark messages read or delete anything. Initially it checks 14 days; subsequent runs use persisted UID/UIDVALIDITY cursors. At most 100 alert messages are processed per cycle. Only sender domains LinkedIn/hh/rabota.by and direct HTTPS job links are recognized; sender identity and labels remain unverified. Redirect-only alerts may yield no links. Full messages and tracking data are not stored. Canonical links appear as separate leads and in candidate digests; a link label alone cannot establish company, geography, or work eligibility.

This is not universal website automation: direct LinkedIn messages, recruiter conversation reading, and ATS-form submission are not implemented. Most public jobs have no application email. There is no invented recipient or fake successful send.

## CLI and background service

```sh
cd backend
# Search and queue only, never sends:
../.venv/bin/python -m app sync --config ../data/searches.json
# Full cycle, may send according to locally saved enabled settings:
../.venv/bin/python -m app run-automation --config ../data/searches.json
# Stop the foreground server first; DATABASE_URL must be set:
../.venv/bin/python -m app install-service --config ../data/searches.json
```

The optional macOS service is `com.alexandrpoge.linkdln-auto`. Its user LaunchAgent starts the server at login and restarts it after failure. Logs stay in ignored `data/service*.log`. PostgreSQL/Docker must be running; the service waits by restarting when the database is unavailable. An offline, sleeping, or powered-off Mac cannot search/send. Moving the checkout or venv breaks the service; this is not an always-on cloud deployment.

To stop the background process:

```sh
launchctl bootout gui/$(id -u)/com.alexandrpoge.linkdln-auto
```

The saved plist remains in `~/Library/LaunchAgents/com.alexandrpoge.linkdln-auto.plist` and will load at the next login. Start it sooner with `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.alexandrpoge.linkdln-auto.plist`. Installation refuses to overwrite an existing definition. Disabling the sending checkboxes pauses external email stages without disabling discovery.
