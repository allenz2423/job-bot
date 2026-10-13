# Agent instructions

## Project context

- Read [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) before changing product behavior. It is the source of product requirements and safety boundaries.
- Use [README.md](README.md), [DOCKER.md](DOCKER.md), and [GMAIL_SETUP.md](GMAIL_SETUP.md) for the behavior and setup that exist today.
- This repository currently contains local Python adapters and a SQLite application ledger. The browser application worker, control UI, per-user system, and other roadmap items in the plan are not necessarily implemented. Check the code before relying on a planned feature, and describe roadmap work as such.
- Keep documentation in sync when changing setup, configuration, or user-visible behavior. Prefer updating the focused setup document as well as the README when relevant.

## Application integrity

- Use only candidate facts the user has supplied and approved. Never invent qualifications, work history, eligibility, or screening answers. Keep unknown, not applicable, and withheld values distinct; pause for a required answer that is unknown or unapproved.
- In Submit eligible mode, allow automatic submissions across queued job hosts without per-host authorization. Stop for CAPTCHAs, verification challenges, site blocks, signatures, legal attestations, or other human-only steps. Do not evade a site's controls.
- Treat the exact full job URL and a confirmed employer job ID as duplicate keys. Company/title similarity is a review warning, not proof of identity.
- Record a job before beginning an application. Mark it applied only after confirmed submission. On restart or an uncertain outcome, inspect the site and durable history before retrying; never blindly submit again.
- Preserve an audit trail for application state changes and make repeat operations safe where possible.

## Data and credentials

- Keep OAuth tokens, passwords, API credentials, topic names, and other secrets in ignored local secret/config files or the designated secret store. Never commit secrets, print them, log them, include them in model prompts, or put them in notifications.
- Keep credentials inside the integration adapter that needs them. Request only the necessary OAuth scopes and avoid sending mailbox or profile data to unrelated services.
- Gmail sync is metadata-only: keep it read-only, fetch headers rather than bodies/snippets, sanitize stored subjects, and never log or persist verification codes. Codes are for human handoff; do not automatically enter them to pass site verification or anti-bot steps.
- Do not store high-risk identity, tax, banking, health, or identity-document data in the general profile. Pause and hand off securely if a site requests it.
- Treat all user records, browser sessions, OAuth tokens, and integration state as private. Any future multi-user feature must scope storage, queries, credentials, browser sessions, and notifications to the owning user; do not infer that current single-user storage is already isolated.

## Existing code boundaries

- `scripts/application_store.py` is the durable application ledger. Preserve its URL validation, idempotency, duplicate-review behavior, status transitions, and event history when modifying storage behavior.
- `scripts/application_tools.py` is the narrow JSON tool interface around the ledger. Keep tool inputs validated and outputs structured; do not expose arbitrary SQL, filesystem access, raw spreadsheet ranges, or secrets to an LLM.
- `scripts/sheets_tracker.py` treats SQLite as the recovery record and Google Sheets as an editable view. Sync bot-owned fields without overwriting user-owned Priority, Notes, or Review Decision values.
- Keep Gmail API and OAuth handling in the Gmail/auth adapters. The Gmail sync is disabled unless explicitly enabled in local configuration.
- Keep local credentials and runtime data under ignored paths such as `config/`, `secrets/`, `state/`, and `data/`. Update the matching `.example` configuration when adding a setting; never put a real value there.
- For future application workers, use the separate discovery and application roles, durable per-user queue/recovery behavior, fresh task context, and human handoffs described in the implementation plan. Do not claim submission support until the browser workflow confirms it.

## Implementation approach

- Inspect the existing adapter and database behavior before changing it; keep responsibilities narrow and avoid duplicating ledger or credential logic.
- Validate external input at adapter boundaries. Keep command-line machine-readable output on stdout and diagnostics on stderr where the existing command follows that convention.
- Make retries safe and external side effects explicit. Do not silently broaden OAuth scopes, data collection, automation permissions, or autonomy.
- Keep changes focused, and update the relevant setup or design documentation when behavior or configuration changes.

## Delegation

- Create subagents when parallel investigation or implementation would help complete the task.
- Set delegated agents to run on Gemini 3.8 Flash on high reasoning effort. Give each agent a bounded task and integrate its findings before finishing.
