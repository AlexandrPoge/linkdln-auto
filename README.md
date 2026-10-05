# linkdln-auto

Personal job-search assistant. The first version will collect vacancies, filter them against a candidate profile, prepare application drafts from templates, and present a batch for approval before sending. It does not use AI.

Stage 2 is implemented: candidate profiles and vacancies are stored in PostgreSQL, and a local CLI imports public Greenhouse job boards. Matching, application drafts, the web interface, and sending are planned for later stages.

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

## Run stage 2 locally

Requires Python 3.11+ and Docker. The compose file exposes PostgreSQL only on localhost with development credentials; do not use them in a deployed environment.

```sh
docker compose up -d db
python3 -m venv .venv
.venv/bin/pip install -e ./backend
export DATABASE_URL='postgresql://jobsearch:local_only_password@127.0.0.1:54329/jobsearch'
mkdir -p data
cp docs/profile.example.json data/profile.json
# Edit data/profile.json with your real information before continuing.
cd backend
../.venv/bin/python -m app profile set ../data/profile.json
../.venv/bin/python -m app profile show
../.venv/bin/python -m app import greenhouse YOUR_BOARD_TOKEN
../.venv/bin/python -m app vacancies --limit 20
```

The `data/` directory is ignored by Git. The profile is stored in local PostgreSQL; do not commit personal profile files or expose this database to the internet. `YOUR_BOARD_TOKEN` is the final segment of a company's Greenhouse board URL. The importer uses Greenhouse's [public Job Board API](https://docs.greenhouse.io/job-board.html), fetches full descriptions, skips prospect posts, updates existing jobs, and marks jobs missing from a successful full-board fetch as closed.

To run unit tests: `cd backend && ../.venv/bin/python -m unittest discover -s tests`. Repository integration tests additionally require `TEST_DATABASE_URL` pointing to a disposable PostgreSQL database whose name ends in `_test`.
