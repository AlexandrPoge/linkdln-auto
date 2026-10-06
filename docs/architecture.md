# Architecture

## Shape

Use a modular monolith: one Python backend for the API, business rules, and scheduled work; one web frontend for the candidate. The backend owns application state and exposes it through an API. Background jobs reuse the same business modules.

## Main flow

1. Source adapters fetch vacancies, normalize them into a common representation, and label Belarus-only versus broader international remote search scope.
2. The vacancies module deduplicates records and tracks their source URLs.
3. Matching applies explicit candidate preferences and skill rules, recording why a vacancy passed or failed.
4. Templates create a draft application from verified candidate facts and vacancy fields.
5. Applications prepares editable candidates with user approval or configured deterministic automatic approval. Approval itself is not delivery.
6. The scheduled pipeline claims approved applications and sends through TLS SMTP with transactional rolling 24-hour limits and one-attempt history.
7. Read-only IMAP collects alert links separately; a durable notification outbox sends new matches and leads to the candidate.

## Boundaries

- `candidate` owns the resume and preferences; other modules read them through its interface.
- `vacancies` owns normalized vacancy records. Source-specific parsing stays under `integrations/job_sources`.
- `matching` makes deterministic decisions and has no network access.
- `templates` generates text but does not send it.
- `applications` orchestrates application decisions. Every send is claimed in the database first; uncertain results are not automatically retried.
- `integrations` owns external communication and cannot approve an application.
- `infrastructure` provides persistence and job scheduling; business decisions stay in domain modules.

## Initial technology direction

The backend is Python with PostgreSQL persistence. Stage 4 adds a localhost-only review page using Python's standard-library HTTP server, so no separate frontend runtime is needed yet. The CLI handles profile storage, public Greenhouse and Ashby board adapters, a targeted Himalayas search adapter, and deterministic matching. Source and board identity isolate imports so that a refreshed board cannot close another source's vacancies. Search results are not treated as complete board snapshots. The `search_track` field filters Belarus-only and international-scope vacancies and their review queues; it does not determine the employer's country or guarantee hiring eligibility from Belarus. A richer frontend and scheduled jobs can be added later if needed. Matching makes no automatic decision about cross-border employment eligibility; the user reviews geography and salary before approving an application.

External platform access must use permitted mechanisms. Browser actions that encounter login challenges or CAPTCHA should stop and surface the issue for review.

The full pipeline now includes Remotive and an hh adapter. A PostgreSQL advisory lock prevents overlapping pipelines across processes; an atomic cooldown limits Remotive calls. Local owner-only email settings are reloaded each cycle. Application claims and notification outbox claims survive restarts. Mailbox cursors commit after leads are saved, making replay harmless. Optional macOS launchd starts/restarts the server; an awake connected computer and PostgreSQL remain prerequisites. Automatic approval is narrow and described in [automation.md](automation.md); it does not certify cross-border employment eligibility. LinkedIn DMs and ATS submissions are outside this implementation.
