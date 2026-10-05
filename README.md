# linkdln-auto

Personal job-search assistant. The first version will collect vacancies, filter them against a candidate profile, prepare application drafts from templates, and present a batch for approval before sending. It does not use AI.

Candidate profiles and vacancies are stored in PostgreSQL. The local CLI imports public Greenhouse and Ashby job boards and searches Himalayas, deterministic rules rank vacancies, and a localhost review queue lets the candidate edit drafts and approve a selected batch. A first delivery path sends approved applications by email **only when an employer application address is entered explicitly**. It is disabled unless `send-approved --execute` is run with SMTP and resume PDF settings. Nothing is submitted on import, match, queue refresh, or approval.

## Structure

- `backend/app/api/` — HTTP entry points for the user interface.
- `backend/app/candidate/` — resume data and search preferences.
- `backend/app/vacancies/` — normalized vacancies, deduplication, and search workflow.
- `backend/app/matching/` — deterministic filtering and ranking rules.
- `backend/app/applications/` — draft, approval, sending, and history state.
- `backend/app/templates/` — message and cover-letter templates.
- `backend/app/integrations/` — source adapters, browser actions, and notifications.
- `backend/app/infrastructure/` — database access and background job plumbing.
- `backend/tests/` — backend tests.
- `frontend/` — review queue, vacancy cards, profile, and application history.
- `docs/` — architecture and implementation stages.

See [docs/architecture.md](docs/architecture.md) for module boundaries and [docs/roadmap.md](docs/roadmap.md) for the staged implementation.

## Run stages 2–4 locally

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
../.venv/bin/python -m app vacancies --limit 20 --track belarus
../.venv/bin/python -m app matches --limit 50 --track international
../.venv/bin/python -m app serve --port 8765
../.venv/bin/python -m app send-approved --track international
```

The `data/` directory is ignored by Git. A local `data/profile.json` based on the confirmed LinkedIn/hh.ru facts is already prepared in this checkout; review it before loading. In a fresh clone, create it from `docs/profile.example.json` and replace all example values. `countries` contains target search regions, while `residence_country` records where the candidate actually lives for eligibility warnings. The profile is stored in local PostgreSQL; do not commit personal profile files or expose this database to the internet. `YOUR_BOARD_TOKEN` is the final segment of a company's Greenhouse board URL. The Greenhouse importer uses its [public Job Board API](https://docs.greenhouse.io/job-board.html), fetches full descriptions, and skips prospect posts. The Ashby importer uses its [public Job Postings API](https://developers.ashbyhq.com/docs/public-job-posting-api), imports only listed jobs, and preserves the board's stated remote locations. Both update existing jobs and mark jobs missing from a successful full-board fetch as closed, independently per source and board.

The Himalayas importer uses its [public Remote Jobs API](https://himalayas.app/docs/remote-jobs-api). The current CLI search supports the saved Belarus profile with country code `BY`. By default it excludes jobs the aggregator labels only as worldwide and keeps those whose published location restrictions explicitly include Belarus. `--include-worldwide` is optional, but it does **not** verify employer eligibility. Himalayas is an aggregator, so its classification is never treated as confirmation from the employer; the review page shows source attribution and a warning. Search imports do not close missing jobs because search results can shift or be incomplete; verify that a posting is still live before approval. Do not republish these listings to other job sites.

Search is split into `belarus` and `international` tracks. The Belarus track holds remote vacancies whose published location scope is Belarus only; the international track holds broader or unknown scopes. This is **not** a classification of the employer's country. The candidate's residence remains Belarus in both tracks, and neither track proves that an employer can hire from Belarus. The local review page has separate tabs and refreshes only the selected track. Existing vacancies keep their review history; reimport source listings to reclassify them from the old default.

To run unit tests: `cd backend && ../.venv/bin/python -m unittest discover -s tests`. Repository integration tests additionally require `TEST_DATABASE_URL` pointing to a disposable PostgreSQL database whose name ends in `_test`.

`matches` reads the saved candidate profile and the newest active vacancies. It returns a transparent score, reasons, warnings, and an **unsent** draft for vacancies worth reviewing. Use `--include-rejected` to inspect why other vacancies were filtered out. The rules reject unrelated titles, QA or management roles absent from the target profile, and, for a remote-only profile, listings without confirmed remote work, office/hybrid locations, or country-specific remote locations that omit the candidate's residence. They also reject explicit residence exclusions in descriptions. A description-only remote claim is flagged for review. Other geography, work-from-residence eligibility, and salary remain manual checks; a high score is not permission to apply. Drafts mention only matching skills explicitly listed in the candidate profile and never invent achievements or send messages.

Open `http://127.0.0.1:8765/` after `serve` starts, then click **Обновить очередь**. The queue stores one review per vacancy. Refreshing it does not overwrite edited text or duplicate reviews. You can inspect the vacancy, edit or reject a draft, and approve up to 100 selected active drafts in one transaction. An approved status means **ready for optional email delivery**, not sent. Closed vacancies cannot be edited or approved. The server binds only to `127.0.0.1`, requires a form token for changes, and does not expose a public API.

## Email delivery (opt-in)

For an approved draft, enter **the application email explicitly published by that employer** in the local review page. Do not guess HR addresses. Configure `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY` (`ssl` or `starttls`), `SMTP_USERNAME`, `SMTP_PASSWORD`, and the absolute `RESUME_PDF_PATH` locally; the resume must be a PDF of at most 5 MB. `SMTP_FROM` may be set explicitly or taken from the locally saved profile's `contact_email`. Never commit credentials or the personal profile file. The sender email must be an address you control, so employers can reply. The generated resume is separate from the repository at `../pdf/Aliaksandr_Poge_AI_Automation_Resume.pdf`; review it before using it in real applications.

`python -m app send-approved --track international` is a **preview** with no network writes. Once the approved text, recipient, resume, and email account have been checked, `python -m app send-approved --track international --limit 10 --execute` sends up to 10 emails and records their outcomes. The database claims each approved application before delivery, blocks duplicate source/job identities, and never automatically retries an attempt with an uncertain result; inspect the employer mailbox before any manual retry. Current Greenhouse/Ashby public listing APIs and Himalayas search data do not provide a universal job-seeker send path. This email channel does not submit ATS forms or send LinkedIn messages.
