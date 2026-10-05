# Architecture

## Shape

Use a modular monolith: one Python backend for the API, business rules, and scheduled work; one web frontend for the candidate. The backend owns application state and exposes it through an API. Background jobs reuse the same business modules.

## Main flow

1. Source adapters fetch vacancies and normalize them into a common representation.
2. The vacancies module deduplicates records and tracks their source URLs.
3. Matching applies explicit candidate preferences and skill rules, recording why a vacancy passed or failed.
4. Templates create a draft application from verified candidate facts and vacancy fields.
5. Applications manages review, batch approval, sending status, and the action history.
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

Python/FastAPI backend, PostgreSQL persistence, Next.js/TypeScript frontend, and a background worker. Choose concrete libraries and deployment settings when the first vertical slice is implemented; this skeleton does not install or configure them.

External platform access must use permitted mechanisms. Browser actions that encounter login challenges or CAPTCHA should stop and surface the issue for review.
