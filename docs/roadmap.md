# Implementation stages

1. **Skeleton (complete):** document module boundaries and create the folder structure without application code.
2. **Profile and vacancies (complete):** store candidate preferences and import vacancies from public Greenhouse job boards; normalize, deduplicate, and track jobs that disappear from a board.
3. **Matching and drafts (complete):** explicit title/remote filters, a transparent score, eligibility warnings, and template-based unsent messages using only candidate-provided skills.
4. **Review queue (complete):** localhost page shows vacancy details, reasons and drafts; edits are preserved, closed jobs are blocked, and selected drafts can be approved together without sending.
4a. **Source expansion (complete for Ashby):** import public listed Ashby vacancies, retain the board's named remote locations, and avoid creating drafts for country-specific locations that omit the candidate's residence. Additional sources still require individual validation.
4b. **Targeted source expansion (complete for Himalayas):** search its public API by Belarus and role terms, distinguish explicit Belarus restrictions from unverified worldwide listings, and display source attribution. Search results are not assumed to be a complete board snapshot.
5. **Sending and history (email path implemented, not configured):** approved drafts can be emailed only to an explicitly entered employer address after local SMTP and resume settings are supplied. Preview is the default, actual delivery requires `--execute`, and one-attempt claims plus outcome history block automatic retries. ATS-form and LinkedIn submissions are not implemented.
6. **Additional sources:** add further adapters one at a time after validating their access rules and reliability.
7. **LinkedIn-assisted workflow (drafts implemented):** generate a short, unsent recruiter message for matched vacancies; use LinkedIn's own Job Alerts for discovery. The app does not scrape LinkedIn, read its messages, or send messages on the member's behalf. Connecting alerts to the local queue would require a supported data path and a real alert sample for validation.

Each stage should be reviewed and tested before beginning the next one.
