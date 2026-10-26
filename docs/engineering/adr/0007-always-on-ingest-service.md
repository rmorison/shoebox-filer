# ADR-0007: Always-On Ingest Service

## Status

Accepted

Replaces product spec RD-06 ("One-shot CLI command. Future expansion via workflow system (e.g., Dagster), not daemon mode."). It also replaces three points from the always-on service brainstorm (`docs/brainstorms/2026-08-01-always-on-service-requirements.md`):
- the key decision "Poll, don't push: 15–30 min latency confirmed acceptable";
- the scope boundary "No new ingestion trigger mechanisms (IMAP IDLE push, GDrive webhooks)";
- requirement R2's "no new daemon code in the application".

Design: [Always-On Ingest](../designs/always-on-ingest.md).

## Context

Production runs the one-shot `receipt-index ingest` every 15 minutes from a cron runner (supercronic) in the compose stack. That was right for getting the service off a laptop. It no longer fits what is asked of it:

- The operator wants new receipts searchable within about five minutes. The main downstream consumer, an agent that builds claim and reconciliation packets, would accept hours, but it needs completeness and kept originals, which today's runs don't give: failed items are never retried, and nothing reports when ingest stops.
- Retries, health reporting, single-instance locking, an IMAP IDLE connection and, later, push or HTTP endpoints all need a process that stays up. A cron job can approximate some of them, and can't hold a connection at all.
- RD-06 expected a workflow system to be the next step. Nothing in this pipeline (two source types, one item at a time) needs a DAG engine, a scheduler service or a UI.

## Decision

1. **A long-running `receipt-index serve` process** is the production ingest path. It runs a watcher per source, a single worker that sends items through the existing pipeline one at a time, a retry scheduler and a health loop. The one-shot `ingest` stays, for development, manual catch-up and rollback, and shares the same sync state and locks.
2. **IMAP: IDLE (RFC 2177) as the trigger, polling as the fallback.** One IDLE connection per watched folder, re-issued every 10 minutes by default. A catch-up poll after every connect, a safety poll every 4 minutes by default, and polling alone when the server has no `IDLE` capability. The library is `imapclient` 4.x (PyPI: `requires_python >=3.8`; its README lists 3.8–3.14), because `imaplib` gains IDLE only in Python 3.14.
3. **Drive: poll the changes feed** (`changes.list` with a saved page token) every 2 minutes by default. **No Drive push (`changes.watch`) for now.** It would need a public HTTPS endpoint and channel renewal, and it would pay off only for a target under about a minute.
4. **The default latency target is 5 minutes** from arrival to searchable, with the source and the LLM API reachable. It's a default, not a fixed requirement: every interval that sets it (IDLE re-issue, IMAP safety poll, Drive poll and settle time, health thresholds) is a config value. The operator can trade latency for load without a code change, up to hourly polling from the same process.
5. **Postgres advisory locks replace the scheduler's no-overlap guarantee.** A lock per source for whoever advances its cursor, and a lock per item while it's processed. `backfill` and `retry` can therefore run next to `serve`, and a stray cron `ingest` exits instead of racing.
6. **Health is part of the service.** A container healthcheck (`status --check`), a `status` command and a dead-man's-switch ping to an external monitor that the operator chooses.

## Consequences

### Positive

- New mail is indexed in seconds while IDLE works, and within the safety-poll interval when it doesn't. Drive files within about 4 minutes.
- One process gives one place for locking, retries, health and, later, push or an HTTP interface.
- Ingest stopping becomes visible: an unhealthy container, a failed `status --check`, an alert when the pings stop.
- Rollback is a deploy change: revert the cutover commit, which removes the service and restores the cron line, then `docker compose up -d --remove-orphans`. Ingest's state lives in additive tables that the one-shot command also reads.

### Negative

- A long-running process has to be kept healthy (connections, memory, a wedged worker). The heartbeat, the healthcheck and the external ping exist for this, and the renderer keeps launching a fresh Chromium per item.
- A new runtime dependency, `imapclient`.
- More configuration surface: intervals and health thresholds, all with defaults.

### Alternatives Considered

- **Cron every 5 minutes.** It can't hold an IDLE connection, and every run reconnects and re-authenticates. With incremental reads ([ADR-0008](0008-incremental-source-sync-state.md)), cron at an hour remains a valid low-cost mode, but the configurable `serve` covers that too.
- **A workflow system (Dagster), as RD-06 planned.** A service, a UI and a scheduler for a single pipeline, rejected as more operational weight than the problem has.
- **Drive push now.** Public exposure, certificates and channel renewal for a gain of about two minutes. Deferred until a sub-minute target exists.
- **asyncio with `aioimaplib`.** It would mean converting a synchronous pipeline (psycopg, Playwright sync API, Pydantic-AI) for a handful of watchers. Threads are simpler.
- **A raw IDLE command on `imaplib`.** It re-implements response parsing and timeouts that `imapclient` already maintains and tests.
