# linkdln-auto

Personal job-search assistant. The first version will collect vacancies, filter them against a candidate profile, prepare application drafts from templates, and present a batch for approval before sending. It does not use AI.

The repository currently contains an architecture skeleton only. No application code, dependencies, credentials, or automation are configured yet.

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
