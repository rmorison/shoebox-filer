---
date: 2026-08-01
topic: always-on-service
---

# Always-On Receipt Service

## Problem Frame

Receipt ingestion today is a manual, laptop-bound CLI run. New receipts (email, GDrive uploads) sit unprocessed until the operator runs `receipt-index ingest`, and the `data/receipts` PDF store lives only on the dev machine. Move the pipeline to a home Ubuntu server as an unattended Docker service: poll sources on a schedule, process new receipts within ~15–30 minutes of arrival, and mirror `data/receipts` to Dropbox for off-machine access and backup. This is a deployment/operations change — no new application features.

---

## Key Flows

- F1. Scheduled ingest
  - **Trigger:** Scheduler fires every 15 minutes (configurable).
  - **Steps:** Run existing `receipt-index ingest` against all configured sources → new items extracted, rendered, indexed; skips/failures land in `ingest_log` DLQ as today → run summary logged.
  - **Outcome:** New receipts searchable and stored under `data/receipts` without human action.
  - **Covered by:** R2, R3, R9

- F2. Dropbox mirror
  - **Trigger:** After each ingest cycle (or on its own short interval).
  - **Steps:** rclone additively copies `data/receipts` to a dedicated Dropbox folder.
  - **Outcome:** Every rendered receipt PDF is visible in Dropbox within minutes; server remains source of truth.
  - **Covered by:** R4

- F3. Search from laptop
  - **Trigger:** The operator needs a receipt during reconciliation.
  - **Steps:** SSH wrapper runs `receipt-index search`/`show`/`failures` inside the server container; receipt PDFs open from the Dropbox mirror.
  - **Outcome:** Full search workflow without the laptop hosting any service state.
  - **Covered by:** R7

---

## Requirements

**Service & scheduling**
- R1. All components run as a single docker compose project on the server; `docker compose up -d` brings up the full service and it survives server reboots (restart policies + Docker enabled at boot).
- R2. Ingest is the existing one-shot CLI run by a scheduler container every 15 minutes (interval configurable); no new daemon code in the application.
- R3. Ingest runs never overlap — if a cycle is still running, the next tick is skipped.

**Dropbox sync**
- R4. `data/receipts` is mirrored one-way (server → Dropbox) via rclone, additive copy only: deletions/renames on the server are never propagated as deletions in Dropbox. Sync provider is an rclone remote config detail, not application code.

**Database & search access**
- R5. Postgres 18 runs in the compose project with a persistent volume; existing production data (currently native pg on the dev machine, port 5435) is migrated once at cutover via dump/restore.
- R6. Schema migrations (golang-migrate, both migration tables) are applied as an explicit deploy step against the server DB.
- R7. Search/show/failures remain available from the laptop via an SSH wrapper into the running container; no web UI in this phase.

**Operations**
- R8. Secrets (IMAP, Anthropic key, GDrive token, DB password) live in a server-side `.env` provisioned from 1Password; never baked into the image or committed. rclone's Dropbox token lives in a server-side rclone config file, generated once via interactive OAuth on the laptop.
- R9. Each ingest cycle logs a one-line summary (counts: processed / skipped / failed); DLQ remains inspectable via `receipt-index failures`; container logs are the single place to look (`docker compose logs`).

---

## Success Criteria

- A receipt emailed or dropped in the GDrive source folder appears in search results and in the Dropbox mirror within ~30 minutes, with no laptop involvement.
- The server can be rebooted and the service resumes unattended.
- `ce-plan` can produce a deploy plan directly from this doc without inventing product behavior — only technical choices (scheduler image, rclone invocation details, image distribution) remain.

---

## Scope Boundaries

- No new ingestion trigger mechanisms (IMAP IDLE push, GDrive webhooks) — polling only.
- No web UI, API, or remote search endpoint — SSH access is sufficient.
- No bidirectional Dropbox sync; Dropbox is a read-only mirror, not an ingest source (a `dropbox` source type is a possible future compounding step, not this phase).
- No CI/CD or registry-based image delivery required for v1 — building on the server from a git clone is acceptable.
- The O(N) IMAP enumeration issue is not solved here; it bounds how cheap frequent polling is, not whether this works.

---

## Key Decisions

- Poll, don't push: 15–30 min latency confirmed acceptable; existing one-shot CLI reused verbatim.
- Scheduler-in-compose over host cron: keeps the entire deployment declarative in the repo; nothing to configure on the host beyond Docker + the `.env`/rclone config.
- Dropbox via rclone sidecar: provider becomes swappable config; additive copy protects against accidental deletion cascades.
- Database moves to the server: the service owns its data; the dev-machine pg 5435 instance is retired for this project after cutover.

---

## Dependencies / Assumptions

- The server has (or will get) Docker Engine + compose plugin and outbound HTTPS/IMAPS access.
- GDrive OAuth token refresh continues to work headlessly once provisioned (existing `receipt-index auth gdrive` flow).
- Anthropic API spend at 96 polls/day is dominated by new-receipt extraction, not polling itself (polling costs no LLM calls; IMAP/GDrive enumeration is the only per-poll cost).

---

## Outstanding Questions

### Deferred to Planning

- [Affects R2][Technical] Scheduler container choice (supercronic vs ofelia vs loop-wrapper) and how R3's no-overlap guarantee is enforced.
- [Affects R4][Technical] rclone cadence and coupling (post-ingest hook vs independent timer) and Dropbox destination folder naming.
- [Affects R5][Technical] Whether to expose Postgres on a host port for ad-hoc laptop `psql`, or keep it compose-internal only.
- [Affects R9][Technical] Tame pdfminer/fontTools DEBUG noise in the CLI entrypoint (existing open issue) so container logs stay readable.

---

## Next Steps

-> `/ce-plan` for structured implementation planning
