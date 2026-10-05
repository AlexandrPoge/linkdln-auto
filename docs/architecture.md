# Architecture

## Shape

Use a modular monolith: one Python backend for the API, business rules, and scheduled work; one web frontend for the candidate. The backend owns application state and exposes it through an API. Background jobs reuse the same business modules.

## Main flow

1. Source adapters fetch vacancies, normalize them into a common representation, and label Belarus-only versus broader international remote search scope.
2. The vacancies module deduplicates records and tracks their source URLs.
3. Matching applies explicit candidate preferences and skill rules, recording why a vacancy passed or failed.
4. Templates create a draft application from verified candidate facts and vacancy fields.
5. Applications prepares review candidates; persistence stores editable drafts and an atomic batch approval state. Approval is not sending.
6. Integrations perform external actions only for approved applications and report outcomes back to applications.

## Boundaries

- `candidate` owns the resume and preferences; other modules read them through its interface.
- `vacancies` owns normalized vacancy records. Source-specific parsing stays under `integrations/job_sources`.
- `matching` makes deterministic decisions and has no network access.
- `templates` generates text but does not send it.
- `applications` is the only module that changes application status. Every send attempt must be recorded and safe to retry without creating a duplicate application.
- `integrations` owns external communication and cannot approve an application.
- `infrastructure` provides persistence and job scheduling; business decisions stay in domain modules.

## Initial technology direction

The backend is Python with PostgreSQL persistence. Stage 4 adds a localhost-only review page using Python's standard-library HTTP server, so no separate frontend runtime is needed yet. The CLI handles profile storage, public Greenhouse and Ashby board adapters, a targeted Himalayas search adapter, and deterministic matching. Source and board identity isolate imports so that a refreshed board cannot close another source's vacancies. Search results are not treated as complete board snapshots. The `search_track` field filters Belarus-only and international-scope vacancies and their review queues; it does not determine the employer's country or guarantee hiring eligibility from Belarus. A richer frontend and scheduled jobs can be added later if needed. Matching makes no automatic decision about cross-border employment eligibility; the user reviews geography and salary before approving an application.

External platform access must use permitted mechanisms. Browser actions that encounter login challenges or CAPTCHA should stop and surface the issue for review.
