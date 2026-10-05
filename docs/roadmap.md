# Implementation stages

1. **Skeleton (complete):** document module boundaries and create the folder structure without application code.
2. **Profile and vacancies (complete):** store candidate preferences and import vacancies from public Greenhouse job boards; normalize, deduplicate, and track jobs that disappear from a board.
3. **Matching and drafts:** add explicit filters, a transparent score, and template-based messages using only verified profile facts.
4. **Review queue:** show vacancy details and drafts, allow editing, and approve a selected batch.
5. **Sending and history:** add one supported sending path, delivery outcomes, retry protection, limits, and an audit trail.
6. **Additional sources:** add adapters one at a time after validating their access rules and reliability.

Each stage should be reviewed and tested before beginning the next one.
