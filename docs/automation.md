# Automated pipeline

Run the local server, open `http://127.0.0.1:8765/`, and use **Автопилот → Gmail без пароля приложения** to connect a personal Gmail account through OAuth and the Gmail API over HTTPS (port 443). No app password or SMTP/IMAP connection is needed. Search runs independently even before connection. See [Gmail OAuth setup](gmail-oauth.md). Existing SMTP settings remain compatible, but the dashboard now offers OAuth as its primary connection method.

Choose a factual, reviewed resume PDF (absolute path, at most 5 MB). After OAuth connection, all email stages are paused until you enable the desired checkboxes and save: candidate digests, mailbox alerts, approved applications, and automatic approval. Saving then starts a full cycle and may send emails. **Искать сейчас** starts an extra cycle. Credentials never appear in dashboard responses or delivery error logs; settings and tokens are saved atomically in Git-ignored `data/` files, owner-only (0600), but not encrypted. Keep the checkout/data directory private.

## Sources and eligibility

Greenhouse/Ashby are company-specific public boards. Himalayas and Remotive are aggregators, not confirmation of employer hiring eligibility. Remotive listings link to and identify the source; its public feed is delayed by 24 hours. A persistent cooldown, including failed attempts/manual runs, limits Remotive to one request every six hours, as recommended in its [official API documentation](https://github.com/remotive-com/remote-jobs-api). The configuration accepts one Remotive query.

The hh adapter uses the [official API](https://api.hh.ru/openapi/redoc), Belarus area 16 and `work_format=REMOTE`, then checks full vacancy details. The live check on this machine returned HTTP 403: the adapter is implemented/tested but hh search is currently unavailable here. The error is shown, not bypassed. Job-alert email intake is a separate path. Search imports are bounded/incomplete and never close missing listings; successful full company-board snapshots can close missing jobs.

Automatic approval additionally requires the strict description screening below. Only Greenhouse/Ashby, seen in the last 24 hours, a current role match with score at least 75, at least two matched skills, employer-stated remote Belarus/worldwide scope, pristine generated text, and one application email clearly published in the description can be autoapproved. The database rechecks screening evidence and the current profile before approval. Edited drafts belong to the user and are not autoapproved. Aggregator/location-search classifications are excluded. This does not certify legal cross-border hiring eligibility. Other jobs remain in the queue for manual review, approval, and an explicit application address.

## Strict description screening

Every full automation cycle screens up to 500 active stored vacancies, even when mail is disconnected. The deterministic filter has three outcomes:

- `matched`: target engineer role, at least 80 characters of description, business/workflow responsibilities, two profile skills in the description, confirmed remote format and explicit residence/worldwide scope in the published text.
- `manual`: insufficient description/skills, ambiguous remote mode, Europe/EMEA scope, unverified experience years, work authorization or minimum salary. These are retained for review, not automatically sent as a digest or autoapproved.
- `rejected`: unrelated/QA/industrial/management roles, Senior/Lead outside the configured focus, explicit residence exclusions, EU/EEA-only or other country requirements.

For hh and Himalayas, a publication region or inferred worldwide classification is **not** scope evidence; their full descriptions must explicitly state remote worldwide/Belarus. Descriptions can be incomplete or outdated; text screening is not legal eligibility certification. Existing drafts and user edits are not overwritten or rejected automatically. The review UI shows their current auto-filter result separately.

The screening cache stores the exact vacancy evidence and candidate profile. Digest claims recheck both plus active status and 24-hour freshness. Changes invalidate old screening without deleting history. Only `matched` descriptions enter candidate digests. Legacy pending link-only notifications remain held, not deleted or automatically resent. Source/job identities are deduplicated across search queries; sent and uncertain history remains untouched.

Templates mention candidate-provided facts only. The sender attaches the selected PDF and records an application claim before Gmail API/SMTP. Default maximum: 5 attempts per rolling 24 hours, configurable from 1 to 10; uncertain attempts also count. A source/job identity gets one attempt even if imported from multiple queries and even if the transport changes. Uncertain outcomes are not automatically retried: inspect the mailbox before resolving them. Candidate digests use a separate durable outbox, up to 25 new items per cycle, also without automatic retries after an uncertain result.

## LinkedIn/hh alerts

Enable native job alerts with email delivery to the connected Gmail. The Gmail API reader searches INBOX job alerts from the last 14 days, scans up to 500 IDs and processes up to 100 previously unseen messages per cycle. Account-scoped IDs are persisted privately (last 10,000) only after successful processing; retries deduplicate extracted links in PostgreSQL. This is a bounded scan, not a guarantee of complete mailbox coverage. It does not mark messages read, change labels, or delete anything. The legacy IMAP reader remains read-only with BODY.PEEK and persisted UID cursors. Only sender domains LinkedIn/hh/rabota.by and direct HTTPS job links are recognized; sender identity and labels remain unverified. Redirect-only alerts may yield no links. Full messages and tracking data are not stored. Canonical links appear as separate leads; a link label alone cannot establish company, geography, or work eligibility. Link-only leads no longer enter candidate digests.

Up to 500 pending leads are classified per cycle. Recognizable unrelated/Senior titles are provisionally filtered based on unverified alert labels; generic action labels remain unknown. LinkedIn links with no full description remain `manual`; the app makes no LinkedIn website requests. hh links can resolve through fixed `GET https://api.hh.ru/vacancies/{numeric_id}` endpoints, never arbitrary email URLs. This stage has a persisted six-hour cooldown, at most eight detail requests per batch, and stops requests after the first source error. Blocked/cooldown leads stay available for a later check. Public API redirects are not followed. Resolved hh descriptions are imported without closing other boards and pass through the same strict filter. Profile changes reopen non-resolved alert classifications.

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
