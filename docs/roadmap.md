# Implementation stages

1. **Skeleton (current):** document module boundaries and create the folder structure without application code.
2. **Profile and vacancies:** store candidate preferences and import vacancies from one permitted source; normalize and deduplicate them.
3. **Matching and drafts:** add explicit filters, a transparent score, and template-based messages using only verified profile facts.
4. **Review queue:** show vacancy details and drafts, allow editing, and approve a selected batch.
5. **Sending and history:** add one supported sending path, delivery outcomes, retry protection, limits, and an audit trail.
6. **Additional sources:** add adapters one at a time after validating their access rules and reliability.

Each stage should be reviewed and tested before beginning the next one.
