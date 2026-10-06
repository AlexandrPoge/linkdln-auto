# linkdln-auto

Local job-search automation for a remote Automation Engineer based in Belarus. It searches configured public sources, filters vacancies, prepares factual messages, reads job-alert emails, sends candidate digests, and delivers configured email applications. It does not use AI.

Candidate profiles, vacancies, and delivery history are stored in PostgreSQL. Sources include Greenhouse, Ashby, Himalayas, Remotive, and an hh adapter. The dashboard supports local Gmail connection, read-only LinkedIn/hh alert intake, scheduled email delivery, and optional narrow deterministic automatic approval using explicitly published application addresses. Direct LinkedIn messages and ATS-form submission are not implemented. Gmail must be connected before email can run. See [automation setup and limits](docs/automation.md).

## Structure

- `backend/app/api/` — local dashboard, scheduled search, and review queue.
- `backend/app/candidate/` — resume data and search preferences.
- `backend/app/vacancies/` — normalized vacancies, deduplication, and search workflow.
- `backend/app/matching/` — deterministic filtering and ranking rules.
- `backend/app/applications/` — draft, approval, sending, and history state.
- `backend/app/templates/` — message and cover-letter templates.
- `backend/app/integrations/` — public source adapters and optional email delivery.
- `backend/app/infrastructure/` — database access.
- `backend/tests/` — backend tests.
- `docs/` — architecture and implementation stages.

See [docs/architecture.md](docs/architecture.md) for module boundaries and [docs/roadmap.md](docs/roadmap.md) for the staged implementation.

## Run locally

Requires Python 3.11+ and Docker. The compose file exposes PostgreSQL only on localhost with development credentials; do not use them in a deployed environment.

```sh
docker compose up -d db
python3 -m venv .venv
.venv/bin/pip install -e ./backend
export DATABASE_URL='postgresql://jobsearch:local_only_password@127.0.0.1:54329/jobsearch'
# Review the prepared data/profile.json before importing it.
cd backend
../.venv/bin/python -m app profile set ../data/profile.json
../.venv/bin/python -m app profile show
../.venv/bin/python -m app import greenhouse YOUR_BOARD_TOKEN
../.venv/bin/python -m app import ashby n8n --company n8n
../.venv/bin/python -m app import himalayas BY --query 'AI Automation Specialist'
../.venv/bin/python -m app sync --config ../data/searches.json
../.venv/bin/python -m app vacancies --limit 20 --track belarus
../.venv/bin/python -m app matches --limit 50 --track international
../.venv/bin/python -m app serve --port 8765 --config ../data/searches.json --interval-minutes 360
../.venv/bin/python -m app send-approved --track international
```

The `data/` directory is ignored by Git. A local `data/profile.json` based on the confirmed LinkedIn/hh.ru facts is already prepared in this checkout; review it before loading. In a fresh clone, create it from `docs/profile.example.json` and replace all example values. `countries` contains target search regions, while `residence_country` records where the candidate actually lives for eligibility warnings. The profile is stored in local PostgreSQL; do not commit personal profile files or expose this database to the internet. `YOUR_BOARD_TOKEN` is the final segment of a company's Greenhouse board URL. The Greenhouse importer uses its [public Job Board API](https://docs.greenhouse.io/job-board.html), fetches full descriptions, and skips prospect posts. The Ashby importer uses its [public Job Postings API](https://developers.ashbyhq.com/docs/public-job-posting-api), imports only listed jobs, and preserves the board's stated remote locations. Both update existing jobs and mark jobs missing from a successful full-board fetch as closed, independently per source and board.

`sync --config ../data/searches.json` is a one-shot search: it fetches each configured public source, updates the database, and refreshes the Belarus and international review queues. It never approves or sends an application. The local `data/searches.json` currently includes n8n's Ashby board and a Belarus-scoped Himalayas query; a template is in [docs/searches.example.json](docs/searches.example.json). Failed sources are reported individually and are not treated as a reason to close their previous listings. Do not put passwords in the search config.

With a valid search config, `serve` starts a full configured pipeline immediately and repeats every 360 minutes **while the process is running**. This may send emails if Gmail is connected and sending enabled. `--interval-minutes` accepts 5–1440. **Искать сейчас** runs another non-overlapping cycle. Remotive has a persistent six-hour request cooldown. The page shows source errors, stage results, drafts, alert links, and delivery history. An optional macOS background service starts the server at login and restarts it; Docker/PostgreSQL and an awake connected computer remain required. LinkedIn email alerts are part of this loop, but LinkedIn DMs are not.

The Himalayas importer uses its [public Remote Jobs API](https://himalayas.app/docs/remote-jobs-api). The current CLI search supports the saved Belarus profile with country code `BY`. By default it excludes jobs the aggregator labels only as worldwide and keeps those whose published location restrictions explicitly include Belarus. `--include-worldwide` is optional, but it does **not** verify employer eligibility. Himalayas is an aggregator, so its classification is never treated as confirmation from the employer; the review page shows source attribution and a warning. Search imports do not close missing jobs because search results can shift or be incomplete; verify that a posting is still live before approval. Do not republish these listings to other job sites.

Search is split into `belarus` and `international` tracks. The Belarus track holds remote vacancies whose published location scope is Belarus only; the international track holds broader or unknown scopes. This is **not** a classification of the employer's country. The candidate's residence remains Belarus in both tracks, and neither track proves that an employer can hire from Belarus. The local review page has separate tabs and refreshes only the selected track. Existing vacancies keep their review history; reimport source listings to reclassify them from the old default.

To run unit tests: `cd backend && ../.venv/bin/python -m unittest discover -s tests`. Repository integration tests additionally require `TEST_DATABASE_URL` pointing to a disposable PostgreSQL database whose name ends in `_test`.

`matches` reads the saved candidate profile and the newest active vacancies. It returns a transparent score, reasons, warnings, an **unsent** application draft, and an **unsent** short LinkedIn recruiter-message draft for vacancies worth reviewing. Use `--include-rejected` to inspect why other vacancies were filtered out. The rules reject unrelated titles and partial title matches of the wrong role type (for example, Automation Specialist when the target is Automation Engineer), QA or management roles absent from the target profile, and, for a remote-only profile, listings without confirmed remote work, office/hybrid locations, or country-specific remote locations that omit the candidate's residence. They also reject explicit residence exclusions in descriptions. A senior title receives an experience warning but no automatic scoring penalty. A description-only remote claim is flagged for review. Other geography, work-from-residence eligibility, and salary remain manual checks; a high score is not permission to apply. Drafts mention only matching skills explicitly listed in the candidate profile and never invent achievements or send messages.

Open `http://127.0.0.1:8765/` after `serve` starts. The queue stores one review per vacancy. Refreshing it does not overwrite edited text or duplicate reviews. You can inspect the vacancy, edit or reject a draft, and approve up to 100 selected active drafts in one transaction. An approved status means **ready for optional email delivery**, not sent. Closed vacancies cannot be edited or approved. The server binds only to `127.0.0.1`, requires a form token for changes, and does not expose a public API.

For active matched vacancies, the page also shows a short, read-only LinkedIn message based on the current saved profile. This text is separate from the editable email application and is never sent by the app. See [docs/linkedin.md](docs/linkedin.md) for LinkedIn's native job alerts and the platform boundary.

The first LinkedIn-alert integration step can inspect a locally saved `.eml` notification without logging in, fetching job pages, importing vacancies, or sending anything:

```sh
cd backend
../.venv/bin/python -m app inspect-linkedin-alert ../data/linkedin-alert.eml
```

The command needs no database or email credentials. It extracts direct LinkedIn job links and unverified anchor labels. The dashboard also supports scheduled read-only Gmail intake of these links; they remain separate unverified leads, not matched vacancies. Keep original alert emails in the Git-ignored `data/` directory because they may contain personal tracking links.

## Email delivery

The dashboard's **Автопилот** connects Gmail through Desktop OAuth + Gmail API over HTTPS, without an app password or SMTP. One-time Google Cloud client setup is required: see [OAuth setup](docs/gmail-oauth.md). Consent opens in the system browser, not an embedded browser. After connection, enable the desired email stages and save; sending stays paused until then. Tokens/settings stay in ignored `data/` files with owner-only permissions (0600), not encrypted. Defaults cap applications at 5 per rolling 24 hours. Missing application addresses leave jobs in review. See [docs/automation.md](docs/automation.md) for the full workflow, limitations, and background service.

The older environment-based CLI below remains a separate, preview-by-default delivery path; it does not use the dashboard's saved Gmail settings.

For an approved draft, enter **the application email explicitly published by that employer** in the local review page. Do not guess HR addresses. Configure `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY` (`ssl` or `starttls`), `SMTP_USERNAME`, `SMTP_PASSWORD`, and the absolute `RESUME_PDF_PATH` locally; the resume must be a PDF of at most 5 MB. `SMTP_FROM` may be set explicitly or taken from the locally saved profile's `contact_email`. Never commit credentials or the personal profile file. The sender email must be an address you control, so employers can reply. The generated resume is separate from the repository at `../pdf/Aliaksandr_Poge_AI_Automation_Resume.pdf`; review it before using it in real applications.

`python -m app send-approved --track international` is a **preview** with no network writes. Once the approved text, recipient, resume, and email account have been checked, `python -m app send-approved --track international --limit 10 --execute` sends up to 10 emails and records their outcomes. The database claims each approved application before delivery, blocks duplicate source/job identities, and never automatically retries an attempt with an uncertain result; inspect the employer mailbox before any manual retry. Current Greenhouse/Ashby public listing APIs and Himalayas search data do not provide a universal job-seeker send path. This email channel does not submit ATS forms or send LinkedIn messages.
