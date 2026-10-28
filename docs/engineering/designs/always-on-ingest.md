# Technical Design: Always-On Ingest

**Version:** 0.1 — Draft
**Date:** 2026-10-25
**Product Spec:** [receipt-search-index-spec.md](../../product/features/receipt-search-index-spec.md)

---

## Context

Ingestion runs as a one-shot `receipt-index ingest`, which supercronic starts every 15 minutes in the production compose stack (`deploy/crontab`). It works, but it scales badly, misses items for good when anything goes wrong, and nobody can tell when it stops:

- **Every run rescans everything.** The IMAP adapter runs `SEARCH ALL` and downloads every full message before checking the `Message-ID` against the processed set (`adapters/imap.py`). The Drive adapter lists the whole folder on every run. It uses each file's `modifiedTime` only as the receipt date, never to skip files it has already seen (`adapters/gdrive.py`). A run's cost grows with the whole mailbox, not with what's new.
- **A failure is final.** `get_processed_source_ids` treats any `ingest_log` row as done, so a `failed` item, maybe from a timeout or an API outage, is never tried again. A `skipped` item, one the LLM judged a non-receipt at low confidence, is never looked at again either.
- **Input folders only grow.** Nothing moves a processed message or file out of the dropoff folder, so the operator can't see which items are new.
- **Nothing reports a stop.** The scheduler container has no healthcheck. A broken token or a dead connection shows up only as log lines nobody reads.
- **No overlap protection in the app.** Only supercronic keeps two runs apart (one job line at a time). A manual `ingest` started next to the cron run is unguarded.

The main consumer is an agent that builds claim packets and reconciliation packets from receipts. It cares about completeness (no lost receipt), about correct paid amounts and about keeping originals, more than about latency. The operator wants new receipts searchable within about five minutes. This design serves the consumer's priorities first and meets the operator's target on top.

### Decisions this design replaces

| Earlier decision | Where | Replaced by |
|---|---|---|
| "One-shot CLI command. Future expansion via workflow system (e.g., Dagster), not daemon mode." | Product spec RD-06 | [ADR-0007](../adr/0007-always-on-ingest-service.md) |
| "Poll, don't push: 15–30 min latency confirmed acceptable"; "No new ingestion trigger mechanisms (IMAP IDLE push, GDrive webhooks)"; R2 "no new daemon code in the application" | Always-on service brainstorm (`docs/brainstorms/2026-08-01-always-on-service-requirements.md`) | [ADR-0007](../adr/0007-always-on-ingest-service.md) |
| "The O(N) IMAP enumeration issue is not addressed here" | Always-on service plan (`docs/plans/2026-08-01-001-feat-always-on-docker-service-plan.md`) and the brainstorm's scope boundaries | [ADR-0008](../adr/0008-incremental-source-sync-state.md) |

The brainstorm and the plan stay as they are, as the record of what was decided then. The ADRs say what has changed since.

### Related designs

- **Evidence store** (`docs/engineering/designs/evidence-store.md`, designed alongside this one). It owns the Drive OAuth scope, the storage interface, originals, hashes and provenance. This design calls its interface (see [Dropoff archive](#7-dropoff-archive) and [Backfill](#8-backfill)) and doesn't define it.
- **Receipt data model** (`docs/engineering/designs/`, designed alongside this one). It owns the receipt's own fields and the re-extraction command. No schema overlap with this design.

### Standards

- [Python Standards](https://github.com/rmorison/engineering-standards/blob/main/code/python-standards.md)
- [Database Standards](https://github.com/rmorison/engineering-standards/blob/main/code/database-standards.md)

---

## Approach

### Solution Overview

A new long-running command, `receipt-index serve`, replaces the cron job. It runs one watcher per source and a single worker:

- **IMAP watchers** hold an IDLE connection (RFC 2177) and wake when the server reports new mail. A safety poll runs even when IDLE is quiet.
- **Drive watchers** poll the Drive changes feed (`changes.list` with a saved page token).
- Each watcher reads only what is new since its stored **sync state** (IMAP UID plus `UIDVALIDITY`, Drive page token) and hands new items to the **worker**. The worker sends them one at a time through the existing extract → render → store → index path.
- Failed items are **retried** with backoff. Items whose outcome is final are **moved out of the dropoff**: to a mail archive folder, or for Drive into the evidence store's `originals/`.
- `serve` reports its **health** three ways: a container healthcheck, a `status` command and a dead-man's-switch ping to an external monitor.
- A **`backfill`** command re-reads a source over a date range. It completes the back catalogue and fetches the originals of receipts indexed before the evidence store existed.

The one-shot `ingest` stays. It reads and advances the same sync state, so it's the development entry point, a manual catch-up, and the rollback path.

### Alternatives Considered

| Alternative | Why not |
|---|---|
| **Keep cron, shorten the interval to 5 minutes** | Every run would still rescan everything unless the sync state lands too, and cron can't hold an IDLE connection. Health would still be "did the last cron line log something". With the sync state, cron at an hour is a fine low-cost option, priced under [Latency](#10-latency-target). At five minutes, a process that starts every few minutes is a long-running process done badly. |
| **A workflow system (Dagster, as RD-06 foresaw)** | It would add a service, a UI and a scheduler for one pipeline with two source types. Nothing here needs a DAG, asset lineage or a multi-step backfill UI. |
| **Drive push (`changes.watch`)** | It needs a public HTTPS endpoint with a valid certificate, and channels that expire and have to be renewed. The server sits on a home network. Polling every 2 minutes meets the target at trivial quota cost. See [Drive](#4-drive-changes-feed) for when push would pay off. |
| **asyncio (`aioimaplib`)** | The pipeline is synchronous (psycopg, Playwright's sync API, Pydantic-AI's `run_sync`) and runs one item at a time on purpose. Threads for a few watchers are simpler than turning the whole pipeline async. |
| **A raw IDLE command on `imaplib`** | `imaplib` gets IDLE only in Python 3.14, and the project supports 3.11. Hand-sending `IDLE` and parsing untagged responses means re-implementing what a maintained library already tests. |

---

## Design

### 1. Process model

```
receipt-index serve
├── watcher thread per IMAP source   (IDLE connection; safety poll)
├── watcher thread per Drive source  (changes.list poll)
├── retry scheduler                  (wakes items whose next_retry_at is due)
├── health loop                      (heartbeat file; dead-man's ping)
└── worker thread                    (one item at a time: run the existing pipeline)
        ▲
        └── in-memory queue of work items: (source, item reference)
```

- **One worker.** Items are processed one at a time, as today. That keeps LLM spend predictable, keeps Chromium to one instance and keeps cursor advancement simple (see [Sync state](#5-sync-state)). Throughput isn't the constraint: a single item takes seconds, and a normal day brings a handful.
- **The queue is in memory and disposable.** A watcher advances its source's cursor only past items that have a committed `started` row or an existing record ([§ 5 cursor rule](#5-sync-state)). If `serve` dies with items queued but not started, they're re-read on restart from the old cursor. Items it dies *during* are recovered from their `started` row by the retry scheduler.
- **Pipeline refactor.** `run_ingest` today loops over `adapter.fetch_unprocessed()`. It splits into `process_item(conn, raw, ...) -> ItemOutcome` (the body of today's loop) and thin loops around it: the one-shot `ingest`, the `serve` worker and `backfill` all call `process_item`. The adapters split the same way: `list_new(sync_state) -> list[ItemRef]` and `fetch(ItemRef) -> RawReceipt`.
- **Shutdown.** On SIGTERM the watchers stop and the cursors are saved. If the worker is in a transient pause, it abandons the pause at once. It writes the item an *uncounted* `failed` row (`counts_attempt = false`, `next_retry_at = now()`, [§ 6](#6-retries)) and releases the lock, so a shutdown during an outage costs no attempt. An item that is actually running gets until `item_timeout_seconds` to finish. The new service's `stop_grace_period` is `11m`, the item timeout plus a minute, so Docker's SIGKILL never cuts a running item short. If one is killed anyway, the dead-`started` rule in [§ 6](#6-retries) recovers it.
- **Chromium.** The renderer launches a fresh browser per item today (`renderer.py`). That stays, so a long-running process doesn't accumulate browser state.
- **One client per thread.** The Google API client (`googleapiclient` on `httplib2`) isn't thread-safe, so the Drive watcher and the worker each build their own service object. IMAP connections and database connections aren't shared between threads either.
- **Item timeout and watchdog.** A Python thread blocked in synchronous psycopg, Playwright or an LLM call can't be abandoned from outside. So the bounds come in two layers:
  - Each blocking call gets its own timeout below `item_timeout_seconds` (default 600): the extraction client's request timeout, Playwright's page and PDF timeouts, and a psycopg `statement_timeout`. A call that hits its timeout raises, and the item is recorded as an item error ([§ 6](#6-retries)).
  - A **watchdog** in the health loop covers anything those miss. If the current item has *run* longer than `item_timeout_seconds`, counting only running time and not time spent in a transient pause ([§ 6](#6-retries)), it logs the source ID and exits the process with a non-zero code. `restart: unless-stopped` then restarts the container. That is the recovery path the healthcheck alone can't provide, because Docker doesn't restart an unhealthy container. On restart, the item's leftover `started` row is recovered by the dead-`started` rule in [§ 6](#6-retries), which uses up an attempt, so an item that hangs every time still ends up terminal.

#### Locking

Two kinds of Postgres advisory lock replace "supercronic runs one job line at a time":

- **Source lock**, held by whoever advances a source's cursor: `pg_try_advisory_lock` on a key derived from `('receipt-index:source', source_name)`, held on a dedicated connection for as long as the holder runs. `serve` holds one for each source it watches. A session lock disappears silently if its connection drops (a Postgres restart, for example), so `serve` checks the lock connection on every watcher loop. On loss it stops that source's watcher, re-takes the lock, and resumes from the stored cursor; if another process took it meanwhile, the watcher stays stopped and `status --check` fails. A one-shot `ingest --source X` that can't take the lock exits with "source X is being watched by a running serve; see `receipt-index status`", with exit code 3, so a cron left in place by mistake fails loudly instead of racing.
- **Item lock**, held while one item is processed: a session-level advisory lock keyed on a hash of the `source_id`, taken before the item's `started` row is written and released after its outcome is committed. `backfill` and `retry` take item locks but never source locks, so they can run while `serve` is up. If an item is locked, the other process skips it, because someone else is handling it. **After taking any item lock, the holder re-reads the item's latest state and acts only if it still calls for that action.** That closes the check-then-act gap:
  - The retry scheduler re-checks that the latest row is still `started` (or still a due `failed`). A worker that committed and unlocked in between leaves an outcome row, and the scheduler then does nothing. A skipped item is never turned into an automatic retry.
  - The worker re-checks that the item is still unseen (for a watcher-queued item) or still due (for a retry). An item that `backfill` finished a moment earlier is dropped, with no second LLM call.

  When the worker skips a locked item, the watcher doesn't advance its cursor past it until the item has a `started` row or another record. If the lock holder dies before writing one, the item is re-read on the next poll, not lost. A hash collision only serializes two unrelated items, because the re-check runs once the lock is free.

The `UNIQUE (source_id)` constraint stays as the last line of defence.

### 2. IMAP: library and IDLE

**Library: `imapclient` 4.x.** PyPI shows `requires_python >=3.8`, and its README lists 3.8–3.14 as supported, so it works on 3.11. It provides `idle()`, `idle_check(timeout)` and `idle_done()`, plus `move()` (RFC 6851), `uid_expunge()` (UIDPLUS), capability checks and parsed `FETCH` responses. It's a thin layer over `imaplib` and adds one runtime dependency. **TLS is verified:** both connections use `imapclient`'s default context (`ssl.create_default_context()`, certificate and hostname checked). That deliberately tightens today's adapter, whose `imaplib.IMAP4_SSL(host, port)` call with no `ssl_context` doesn't verify the server certificate on Python 3.11. A long-running service logs in far more often than a cron job, which makes an unverified connection a bigger risk. A unit test asserts the client is built with a verifying context. Login behaviour is otherwise unchanged.

**Two connections per IMAP source:**

- The **watcher connection** `EXAMINE`s the folder (read-only) and sits in IDLE.
- The **worker connection** `SELECT`s the folder (read-write, needed for the archive move) and fetches and moves messages.

The worker can't use the watcher's connection, because no other command can be issued during IDLE. Two connections per source is far below any provider's per-account limit.

**Watcher loop:**

1. Connect and log in. On startup or after any reconnect, run a **catch-up poll** (step 4).
2. If the server lacks the `IDLE` capability, poll every `imap_poll_seconds` instead, and stop here.
3. Enter IDLE. Call `idle_check(timeout=…)` in a loop. On an `EXISTS` response, leave IDLE and poll (step 4). Re-issue IDLE every `idle_reissue_seconds` (default 600) even when nothing happened. RFC 2177 lets a server drop a client after 29 minutes, and home routers often drop idle TCP flows sooner. TCP keepalive is enabled on the socket too.
4. **Poll:** `UID SEARCH UID <last_uid+1>:*` (see [Sync state](#5-sync-state)). RFC 3501 treats `n:*` as covering the highest existing UID even when `n` is above it, so the result is filtered client-side to UIDs greater than `last_uid`. Queue the new UIDs and update `last_poll_at`. When the source has `archive: true`, queue an archive sweep for the worker too (see [§ 7](#7-dropoff-archive)).
5. Run a **safety poll** every `imap_poll_seconds` (default 240) whether or not IDLE fired. This covers a half-open connection where IDLE never fires, and it sets the worst-case latency.

**Reconnect.** A connection error or a missed IDLE response closes both connections. Reconnection backs off from 5 seconds to 5 minutes. A login refused with an authentication error isn't retried in a tight loop: it's recorded in `sync_state.last_error`, shows in `status`, fails the health check, and is retried every 15 minutes.

**Fetching.** The worker first fetches only the headers it needs for the source ID, `BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT DATE FROM)]`. If the ID is already known (see the two checks in [§ 6](#6-retries)), it skips the message without downloading the body. Otherwise it fetches `BODY.PEEK[]`, the exact RFC 822 bytes, which become `RawReceipt.raw_bytes` for the evidence store. `PEEK` leaves the `\Seen` flag alone, as `EXAMINE` does today.

### 3. Incremental IMAP reads and UIDVALIDITY

- **Normal case:** the sync state holds `(uidvalidity, last_uid)` for the folder. A poll asks only for UIDs above `last_uid`. A message moved into the folder by a mail filter or by hand gets a new, higher UID, so it's picked up. A message dragged back out of the archive gets a new UID, but its `Message-ID` is already processed, so the header check skips it.
- **`UIDVALIDITY` changed** (the server rebuilt the folder, or the folder was deleted and recreated): every stored UID is now meaningless. The watcher logs a warning, sets `uidvalidity` to the new value and `last_uid` to 0, and does a **full header scan**: one `UID FETCH 1:* (BODY.PEEK[HEADER.FIELDS (...)])` round trip, which is cheap even for thousands of messages. It then queues only `Message-ID`s it has never seen. Idempotency has always been keyed on `Message-ID`, not on UID, so the reset can't create duplicates. The event shows in `status` so the operator knows why a scan happened.
- **First run of a source** (no sync-state row) is the same full header scan.
- **After either scan**, `last_uid` doesn't jump to the highest UID seen. It follows the cursor rule ([§ 5](#5-sync-state)): the scan queues unseen messages in UID order, and `last_uid` advances to each queued message's UID as its `started` row is committed, or straight to the highest UID once nothing unseen is left. If the process stops mid-scan, the next start repeats the scan from the stored `last_uid`.
- **Renaming a source** in the config starts a new sync-state row: one full header scan, no duplicates. The old row is left in place and shown as orphaned in `status`.

### 4. Drive: changes feed

- **Bootstrap** (no sync-state row): call `changes.getStartPageToken` *first*, then list the folder as today (`files.list` with `'<folder>' in parents`) and queue files never seen before, then save the token. Taking the token before the listing means a file uploaded during the listing is in both, and idempotency removes the duplicate. Taking it after would lose that file.
- **Poll** every `drive_poll_seconds` (default 120): `changes.list(pageToken=…, spaces='drive', fields='nextPageToken,newStartPageToken,changes(fileId,removed,time,file(id,name,mimeType,parents,trashed,createdTime,modifiedTime,sha256Checksum))')`. Follow `nextPageToken` to the end, and keep each change whose file has the dropoff folder in `parents`, isn't trashed and isn't a folder (`application/vnd.google-apps.folder`, which drops the needs-attention subfolder). A kept file of a type the adapter can't ingest is recorded as `skipped` with the reason "unsupported file type", so it has an outcome like any other. Save `newStartPageToken` only once every kept file has a committed `started` row or outcome ([§ 5 cursor rule](#5-sync-state)).
- **The feed covers the whole Drive**, not just the dropoff folder: every edit the operator makes anywhere shows up in it. For a personal Drive that means at most a few pages per poll, and the filter is in memory.
- **Settle time.** Scanner apps sometimes create a file and then upload its content in a second step. A change whose server-assigned `time` is less than `drive_settle_seconds` (default 60) old is left for the next poll, and the token isn't advanced past it. The file's own `modifiedTime` isn't used, because a scanner app can set it from its own clock (see [ADR-0008](../adr/0008-incremental-source-sync-state.md)).
- **Invalid token** (rare): Drive v3 answers a bad or expired `pageToken` with HTTP 400 "Invalid Value", not the 404 or 410 that some other Google APIs use. A 400 that names `pageToken` reruns the bootstrap, and so does a 404 or 410, as a precaution. That's one folder listing and no duplicates. Ticket 1 confirms the exact response against the API and records it in its test fixture.
- **Quota:** a poll every 2 minutes is 720 calls a day plus paging, far inside Drive's default per-project quota.
- **Account and token.** The evidence-store design's sign-off settled the scope. The full `drive` scope is held by a **dedicated store account**, and the operator shares each dropoff folder with that account as Editor. After that design's Drive ticket, one token (`GDRIVE_STORE_TOKEN_JSON`) does everything Drive-side: it reads the changes feed and the dropoff folders, and it makes the adopt and needs-attention moves. The operator's `drive.readonly` token is retired. Until then, the operator's current `drive.readonly` token reads the feed, and Drive sources run with `archive: false`.
- **The feed belongs to an account.** A saved page token is a position in one account's change log, so it means nothing to another account. `sync_state` stores the Drive account's stable ID (`about.get(fields='user(permissionId)')`, not an email address) next to the token. When the configured token's account differs from the stored one, as at the switch to the store account, the source re-bootstraps: one folder listing, no duplicates.
- **Shared folders in the feed.** The dropoff folder is shared with the store account, not owned by it. Ticket 1 confirms, against a scratch folder, that the store account's changes feed reports files the operator adds to a folder shared with it. If it doesn't, the Drive watcher lists the dropoff folder's direct children with `files.list` on each poll instead. That's cheap once archiving is on, because the folder then holds only unfinished items.

**When would Drive push (`changes.watch`) pay off?** When the target drops below about a minute, or when the polling cost matters. Neither is true at a 2-minute poll. Push would need a public HTTPS endpoint (a tunnel or a reverse proxy with a certificate), and channel renewal before expiry (changes channels last up to a week). Polling would still be needed as a fallback, since notifications can be lost. If push is ever added, the `serve` process is where the endpoint and the renewal loop would live, which is one more reason to build it now.

### 5. Sync state

A new table holds each source's read position, one row per (source, folder):

```sql
CREATE TABLE IF NOT EXISTS receipt.sync_state (
    id                UUID NOT NULL DEFAULT uuidv7(),
    source_name       TEXT NOT NULL,
    source_type       TEXT NOT NULL,
    folder            TEXT NOT NULL,          -- IMAP folder name or Drive folder ID
    imap_uidvalidity  BIGINT,
    imap_last_uid     BIGINT,
    drive_page_token  TEXT,
    drive_account_id  TEXT,                   -- permissionId of the account whose change log the token belongs to
    archive_enabled_at TIMESTAMPTZ,           -- when archive: true was first seen for this source; NULL while off
    last_poll_at      TIMESTAMPTZ,            -- last time the source was read, success or not
    last_success_at   TIMESTAMPTZ,            -- last read that completed without error
    last_error        TEXT,
    last_error_at     TIMESTAMPTZ,
    resets            INTEGER NOT NULL DEFAULT 0,  -- UIDVALIDITY changes or token rebootstraps
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_sync_state PRIMARY KEY (id),
    CONSTRAINT uq_sync_state_source_folder UNIQUE (source_name, folder),
    CONSTRAINT ck_sync_state_source_type CHECK (source_type IN ('imap', 'gdrive')),
    CONSTRAINT ck_sync_state_imap_cursor CHECK (
        source_type <> 'imap' OR (drive_page_token IS NULL AND drive_account_id IS NULL)),
    CONSTRAINT ck_sync_state_drive_cursor CHECK (
        source_type <> 'gdrive' OR (imap_uidvalidity IS NULL AND imap_last_uid IS NULL))
);
```

- It sits in the `receipt` schema next to `ingest_log`, with the `set_updated_at` trigger from the `public` migrations. `receipt_index_dev_write` gets `SELECT, INSERT, UPDATE` and `receipt_index_dev_read` gets `SELECT`, following migrations 000002 and 000004. As every migration must be, it's mirrored in `tests/integration/conftest.py` `_run_migrations()`.
- **Cursor rule.** The cursor only ever moves to a position at or below which every item has a committed `started` row in `ingest_log` ([§ 6](#6-retries)), or an existing record: a receipt or an earlier `ingest_log` row, which is why a known message the worker skips doesn't stall it. From that row on, the item is owned by the retry machinery, not by the cursor: a crash, a `failed` outcome and a pending retry are all recovered by source ID. With a single worker, items start in queue order, so the rule is "once item N's `started` row is committed, the watcher may store N's UID or token". An item that is queued but not started is never passed, so it is re-read if `serve` stops.
- Sync state is a cache of "where to start reading". Deleting a row is always safe: the source re-bootstraps with a full header scan or folder listing, and idempotency absorbs it.

### 6. Retries

**Two kinds of failure, handled differently:**

- **Transient errors are retried without using up an attempt.** They're raised as a `TransientError` class at the boundaries that call outside services, in two groups:
  - *Service hiccups:* LLM API connection errors, 5xx responses and rate limits; network errors to IMAP or Drive; evidence-store backend errors.
  - *Source-wide refusals,* which would otherwise fail every item in turn: an LLM authentication, permission or billing error (HTTP 401, 403, or the credit-balance error), and an IMAP or Drive authentication refusal met inside the worker.

  When one is raised, the worker keeps the item's lock, puts the item back at the head of the queue and pauses with exponential backoff (30 seconds, doubling to 15 minutes). Because the lock is held for the whole pause, the retry scheduler never mistakes a paused item for one whose process died. The pause shows in `status`, and a pause longer than the worker's stale threshold fails `status --check` ([§ 9](#9-health)). An LLM outage or a lapsed key therefore delays items, raises the alarm, and fails none.
- **One item can't hold the queue for ever.** If the same item keeps raising service hiccups for longer than `transient_item_max_seconds` (default 21600, six hours) in total, the next one is recorded as an item error, and the worker moves on to the next item. That covers an item that fails the same "transient" way every time, such as a fetch that times out on a huge message. Source-wide refusals don't count towards this limit, since they aren't the item's fault.
- **Item errors use up an attempt.** These are a parse error, a render failure, an extraction that fails validation, a store write that the store reports as permanent (the store can also mark an error terminal, such as a native Doc over Drive's 10 MB export cap; that item turns terminal on its first attempt and moves to `needs-attention` with the reason), and an item that exceeds `item_timeout_seconds` ([§ 1](#1-process-model)). The item is written to `ingest_log` as `failed` with `attempt = n` and `next_retry_at = now() + backoff[n]`.

**Backoff and limits:** `retry_backoff_seconds` defaults to `[300, 1800, 7200, 43200]`: 5 minutes, 30 minutes, 2 hours, 12 hours. `retry_max_attempts` defaults to 3 (the operator lowered it from 5 at ticket 2's sign-off, to avoid paying repeatedly to fail the same item). The third failure is **terminal**: `next_retry_at` stays NULL, the item moves to `needs-attention` if the source archives, and it shows in `failures`, in `status` and on the dead-man's ping ([§ 9](#9-health)).

**Schema:** `ingest_log` stays append-only, and the latest row for a `source_id` gives the item's state. Each attempt writes a `started` row when it begins, committed before any work so a crash leaves a trace, and then a row with its outcome. Two migrations add a status value and four columns: ticket 1 adds `started` and `source_name`, which the cursor rule needs, and ticket 2 adds `attempt`, `counts_attempt` and `next_retry_at` (see [Implementation Plan](#implementation-plan)). Together:

```sql
ALTER TABLE receipt.ingest_log
    ADD COLUMN source_name    TEXT,
    ADD COLUMN attempt        INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN counts_attempt BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN next_retry_at  TIMESTAMPTZ;

ALTER TABLE receipt.ingest_log DROP CONSTRAINT ck_ingest_log_status;
ALTER TABLE receipt.ingest_log ADD CONSTRAINT ck_ingest_log_status
    CHECK (status IN ('started', 'success', 'failed', 'skipped'));

CREATE INDEX IF NOT EXISTS idx_ingest_log_next_retry_at
    ON receipt.ingest_log (next_retry_at) WHERE next_retry_at IS NOT NULL;
```

- **`source_name`** says which configured source an item came from, and so which account, folder and token a retry uses. Today's table records only `source_type`, and the spec allows several sources of one type. Every new row sets it. A historical row with a NULL `source_name` is re-fetched by trying each configured source of its `source_type`.
- **Grants:** migration 000004 grants `INSERT` on `ingest_log` column by column, so these migrations also grant `INSERT (source_name, attempt, counts_attempt, next_retry_at)` to `receipt_index_dev_write`.
- **Attempt numbering.** Each attempt's rows carry its `attempt` number. The next attempt's number is the latest `failed` row's `attempt` plus one, or the same number when that row has `counts_attempt = false`. Only a shutdown during a transient pause writes an uncounted row ([§ 1](#1-process-model)). A transient error that doesn't meet a shutdown writes nothing, because the worker keeps the item and pauses. The terminal check compares the number of counted attempts with `retry_max_attempts`. A manual `retry` starts again at 1. It's mirrored in `conftest.py`.
- **Down migration:** ticket 1's deletes the `started` rows before re-adding the original status constraint, which would otherwise fail on them. See the rollback note in [§ 13](#13-deployment-and-rollback).
- The `failures` command and other readers of `ingest_log` ignore `started` rows unless asked for them.

**Who picks an item up: two checks, one owner each.** An item has one owner at a time, so a re-read never jumps the retry schedule.

- **Watchers, bootstraps and full scans** queue only source IDs that have **no receipt and no `ingest_log` row at all**. That's today's processed-ID rule, unchanged. Re-reads (a held-back Drive token, a rebootstrap, a UIDVALIDITY scan) therefore never re-process an item that is waiting for its retry.
- **The retry scheduler** owns every item that has an `ingest_log` row but no final outcome. It queues them by source ID:
  - items whose latest row is `failed` with a due `next_retry_at`;
  - items whose latest row is `started` while nobody holds their item lock. The process that started them has died. That uses up an attempt, written as a `failed` row with the normal backoff, so an item that crashes or wedges the process every time still ends up terminal and doesn't loop.

  An item is final when it has a receipt, or when its latest row is `success`, `skipped`, or `failed` with `next_retry_at IS NULL` (terminal). When the evidence store's provenance table takes over the idempotency key (`provenance.source_id`), `provenance` stands in for `receipts` in both checks.
- **The one-shot `ingest` runs the same scheduler query once** for the sources it holds the lock for, after its incremental read. Retries therefore work under cron too, in the "hours" mode of [§ 10](#10-latency-target), and after a rollback to cron. `serve` runs the same function on a timer.

**Re-fetching an item** goes through one fetch path, shared by retries, `retry`, `backfill` and the data-model design's re-extraction. It returns the item and the folder it was found in, so a later archive move starts from the right place:

- **IMAP, by `Message-ID`:** `UID SEARCH HEADER Message-ID <id>`, in the watched folder first, then `needs_attention_folder`, then `archive_folder`. Skipped, terminal and manually retried items may already have been moved.
- **IMAP, fallback-hash IDs** (messages without a `Message-ID`, keyed by a SHA-256 of subject, date and sender): `UID SEARCH` on the subject and sender plus `SENTON <date>`, in the same folders. The hash is recomputed from each candidate's headers to confirm the match. The `started` row records which kind of key the item has.
- **Drive:** `files.get(fileId)`, which finds the file wherever it is now.

Once the evidence store keeps originals, a retry whose original is already stored reads it from the store instead.

**Existing failed rows** have no `next_retry_at` after the migration, so they start out terminal. They aren't retried automatically, because doing so at cutover would spend LLM calls without the operator's say. The cutover runbook runs `receipt-index retry --status failed --all --yes` once, by hand.

**Skipped items** (confidence below 0.5) aren't retried automatically. Re-asking the same model with the same prompt mostly returns the same answer, and costs a call. Some may still be real receipts, and completeness matters, so they're surfaced:

- `receipt-index failures --status failed|skipped|retrying|terminal [--since DATE] [--source NAME]` lists them with subject, sender, date and reason, and `--output json` is for the consumer agent. The default stays `failed`, so today's behaviour holds.
- `receipt-index retry SOURCE_ID…` or `receipt-index retry --status skipped|failed [--error-like TEXT] [--class transient|content|config] [--since DATE] [--source NAME] [--all] [--yes]` requeues items by hand. It writes a `queued` row per item (status `queued`, `attempt = 0`), which the next retry pass runs as attempt 1, and records each request in `receipt.retry_requests`. It previews unless `--yes` is given, refuses a selection with nothing narrower than `--status` unless `--all` is given, takes item locks, and reports renditions orphaned by the matched failures (`ingest_log.stored_ref`). Each failed row also records its class (`transient`, `content` or `config`), so `retry --transient` requeues what an outage failed. (Settled at ticket 2's sign-off: the design first had `retry` run the items itself.) When the data-model design's re-extraction changes the prompt, `retry --status skipped` is how the skipped back catalogue gets a second look.

### 7. Dropoff archive

**What moves where:**

| Outcome | IMAP source | Drive source |
|---|---|---|
| `success` | Mail archive folder (`archive_folder`). The evidence store keeps its own `.eml` copy. | Moved into the evidence store's `originals/` by `adopt_drive_original`, which is the original. Under the store's copy fallback (`store.drive.dropoff_archive: copy`), the store keeps a copy and the dropoff file moves to the dropoff's `processed/` subfolder. |
| `skipped` | `needs_attention_folder` | `needs_attention_folder_id` (a subfolder of the dropoff) |
| terminal `failed` | `needs_attention_folder` | `needs_attention_folder_id` |
| `failed`, retry pending | stays in place | stays in place |

**Archiving is opt-in for each source.** With `archive: false`, the default, nothing moves and the source needs only read access, which is today's behaviour. That holds for Drive too. Storing a Drive original *is* the move into `originals/`, so a Drive source with `archive: false` leaves each processed file in the dropoff, and the store records its original as `pending` (the evidence store's `provenance.original_status`). When the operator later turns `archive: true` on, `backfill --originals-only` adopts those files ([§ 8](#8-backfill)). Nothing ever deletes from `needs-attention` automatically. The operator empties it after review, and a successful manual `retry` of an item there archives it then.

**Order of a successful item.** These steps, the ingest-to-store interface, are agreed with the evidence-store design; that design owns the names, signatures and duplicate rules:

1. **Store the original** (when the evidence store is in place). IMAP: `store_email_original(raw_bytes, *, source_name, received_at) -> StoredEmail`. It stores the `.eml` and each attachment, and is content-addressed and idempotent, so a retry after a crash stores nothing twice. A Drive original is recorded at step 3, because storing it means moving the file.
2. **Whole-original duplicate check.** If the whole original (the `.eml`, or the Drive file's `sha256Checksum` from the metadata the adapter already fetches) is byte-identical to one already linked to a receipt, the store links the new provenance to that receipt and extraction is skipped. The store records that hash on the provenance row at fetch (`provenance.source_sha256`), so the check works for Drive under `archive: false` too, with nothing moved. The store's other duplicate rules (receipt document, order number) need extracted fields, so they run in step 4.
3. **Extract and render**, as today. Then, for Drive with `archive: true`, `adopt_drive_original(file_id, *, source_name, received_at, from_folder_id) -> StoredDriveOriginal` (its `file`, plus an `export` for a native Doc's PDF snapshot, otherwise None). It moves the file into `originals/YYYY/MM/…` (the file ID doesn't change), makes it read-only and records it. It's idempotent on the dropoff file ID: adopting a file that is already adopted, or already under `originals/`, returns the existing record. A native Google Doc is moved as-is, and the store also exports a PDF snapshot as its hashed evidence. Under the store's copy fallback (`store.drive.dropoff_archive: copy`, for when the store account can't move an operator-owned file), the store copies the file instead, finds an existing copy by checksum on a retry, and the dropoff file moves into the dropoff's `processed/` subfolder. Neither `processed/` nor `needs-attention` is read by the watcher, because the changes feed is filtered to direct children of the dropoff folder, and folders are dropped.
4. **Commit** the receipt, its provenance and the `ingest_log` row in one transaction. The store's remaining duplicate rules run inside this transaction.
5. **IMAP with `archive: true` only:** move the message to `archive_folder`. For Drive, step 3 already moved the file.

**Crash windows, and why each one is safe.** Every attempt starts with a `started` row in `ingest_log` ([§ 6](#6-retries)), committed before step 1, so a crash anywhere leaves a durable record of the item by source ID. Recovery never depends on a source listing finding the item again.

- **After the original is stored, before the commit** (steps 1–3).
  - IMAP: a stored file with no provenance. The retry scheduler requeues the item from its `started` row, and the content-addressed `store_email_original` reuses that file. The evidence store's `verify --orphans` reports any that are left over.
  - Drive: the file is already in `originals/` (or copied, with the dropoff file in `processed/`) and has no receipt. The changes feed can't be relied on here, because it reports a file's *current* parents, which no longer include the dropoff folder. The retry scheduler requeues the item from its `started` row instead: `files.get` by file ID finds the file wherever it is now, and the idempotent `adopt_drive_original` returns the existing record.
- **After the commit, before the IMAP move** (steps 4–5), **or after a failed move to needs-attention.** The receipt is committed but the message is still in the watched folder, and the cursor has already passed it, so an incremental poll won't see it again. The **archive sweep** handles it. When a source has `archive: true`, every safety poll queues a sweep for the **worker**, which holds the read-write connection (the watcher's is `EXAMINE`, and connections aren't shared between threads). The worker runs a header-only pass over the whole watched folder (`UID SEARCH ALL`, then `BODY.PEEK[HEADER.FIELDS (...)]`). It moves each message whose source ID reached a final outcome **after archiving was turned on** (`sync_state.archive_enabled_at`) to its archive or needs-attention folder. A message is never processed twice.
  - The back catalogue, items finished before archiving was enabled, is left alone. Filing it is a deliberate step, `backfill --archive` ([§ 8](#8-backfill)), not a side effect of the first sweep.
  - The sweep is cheap, because an archiving folder holds only unfinished items, recent finished ones and the back catalogue.
  - On a server with neither `MOVE` nor `UIDPLUS`, archiving is unsupported (below), so the sweep doesn't run at all.
  - **Drive has the same sweep.** When a Drive source has `archive: true`, each poll also queues a Drive sweep for the worker, which lists the dropoff folder's direct children with its own client and moves any file whose source ID reached a final outcome after `archive_enabled_at` to its destination. That catches a failed needs-attention move or an interrupted adopt.

The tests cover each window explicitly (see [Testing Strategy](#testing-strategy)).

**How IMAP moves a message:** `UID MOVE` when the server advertises `MOVE`. Otherwise `UID COPY`, then `UID STORE +FLAGS (\Deleted)`, then `UID EXPUNGE`, the last of which needs `UIDPLUS`. With neither `MOVE` nor `UIDPLUS`, a plain `EXPUNGE` would also delete any other `\Deleted` messages in the folder, so the watcher refuses: it leaves the message in place, logs a warning once and shows "archive unsupported" in `status`. The archive and needs-attention folders are created on startup if they're missing.

**Drive skipped and failed moves** use `files.update(addParents=<needs-attention>, removeParents=<dropoff>)`. They're made with the store account's token, under the evidence-store design's client guard, which allows exactly these moves. Every Drive move, these and the adopt, needs that token. Until the evidence store's Drive backend and the dedicated account land, Drive sources run with `archive: false`, and config validation rejects `archive: true` on a Drive source while the configured store backend isn't Drive.

**Drive edge cases:**
- **A whole-original duplicate** (step 2 skips extraction) is still adopted with `archive: true`, so the dropoff empties. The store records it as a second provenance of the existing receipt, under its own file ID.
- **An item that fails terminally after it was adopted** is a commit that failed after the file moved into `originals/`. It stays there with no receipt and is never deleted. The store's `verify --orphans` reports it. `status` lists it among terminal failures with its location, and `retry` re-fetches it by file ID from wherever it is.
- **A failed needs-attention move** is caught by the Drive sweep above.

### 8. Backfill

```
receipt-index backfill --source NAME [--since DATE] [--until DATE]
                       [--originals-only] [--archive] [--dry-run] [--limit N]
```

- **Full mode** (the default) enumerates the source over the date range and calls `process_item` on everything it hasn't seen. For items it has seen, it fills in the original if one is missing. It searches the watched folder, the archive folder and `needs-attention` (IMAP: `UID SEARCH SINCE d BEFORE d'`, which uses the server's internal arrival date; Drive: `files.list` with a `createdTime` range over the dropoff and needs-attention folders). Without `--since`, it takes the whole source.
- **`--originals-only`** re-fetches the original of each receipt that has none, and does no extraction:
  1. Take the list from the evidence store's `provenance_missing_originals(source_name, since, until)`.
  2. Fetch each item by source ID through the shared fetch path ([§ 6](#6-retries)), which searches the same folders.
  3. Store it with `store_email_original` or `adopt_drive_original`, and link it. A Drive source is adopted only with `archive: true`; with `archive: false` its originals stay `pending`.
  4. If an item can't be found, call `mark_original_unavailable(provenance_id, reason)`.

  This mode needs the evidence store's schema ticket, which creates one provenance row for each existing receipt.
- **Draining the dropoffs** (needs ticket 6's archive moves). Today's Drive files, already indexed, are still in the dropoff folder. Once the source has `archive: true`, running `backfill --originals-only` on it adopts each one, which moves it into `originals/`, so the same run empties the dropoff. For IMAP, `backfill` with `--archive` moves already-processed messages to the archive folder. This is the only way the back catalogue gets filed, since the sweep handles only outcomes reached after archiving was enabled. Without `--archive` it never moves mail, so a dry run and a first real run change nothing in the mailbox.
- **Idempotency and locking.** It takes item locks, never the source lock, so it runs while `serve` is up. It never moves a sync-state cursor. Re-running it over the same range does nothing new.
- **Cost.** Full mode costs one LLM call per unseen item; `--dry-run` prints how many there are first. `--originals-only` makes no LLM calls.
- **Completeness check.** `backfill --dry-run --since <date>` reporting zero unseen items is how the operator checks after cutover, and from time to time, that the watchers miss nothing.

### 9. Health

**The heartbeat.** The health loop touches a heartbeat file (`/tmp/receipt-index-serve.heartbeat`) every 30 seconds while the worker thread is alive and is idle, paused on a transient error, or has run its current item for less than `item_timeout_seconds` (running time only, as for the watchdog). A slow LLM call therefore doesn't trip the check, but a wedged worker does. Each watcher updates `last_poll_at` and, on success, `last_success_at` in its sync-state row.

**`receipt-index status [--output json] [--check]`** reports:
- each source's cursor, `last_poll_at`, `last_success_at`, `last_error`, reset count and archive mode;
- the worker's heartbeat age and whether it's paused on a transient error, and why;
- retry counts: pending (`failed` with a due `next_retry_at`), terminal, and skipped in the last 7 days;
- any orphaned sync-state rows.

`status` works without `serve` running. It reads the database, and the heartbeat file if present.

**`status --check`** exits non-zero, printing one line per problem, when any of these holds:
- the heartbeat file is older than `heartbeat_stale_seconds` (default 120);
- a source's `last_success_at` is older than its stale threshold. That's `source_stale_seconds` when set, and otherwise three times the source's poll interval: 720 seconds for IMAP and 360 for Drive at the defaults. A threshold that follows the interval keeps hourly polling from failing the check between polls;
- a watcher has stopped after losing its source lock to another process ([§ 1](#1-process-model));
- the worker has been paused on transient errors for longer than `source_stale_seconds` when that's set, or otherwise the largest of the sources' stale thresholds (720 seconds at the defaults).

Terminal failures don't fail the check. They're content problems, not liveness problems, so they mustn't mark the container unhealthy. They still reach the operator through the dead-man's ping (below) as well as `status` and `failures`.

**The container healthcheck** runs `receipt-index status --check` every 60 seconds with `retries: 3`. Docker marks the container unhealthy. The `restart: unless-stopped` policy restarts only a process that exits, so the healthcheck is a signal, and the dead-man's switch is the alert.

**The dead-man's switch.** Every `ping_interval_seconds` (default 300), the health loop runs the same checks as `status --check`:

- If they pass and no item has turned terminal within the last `terminal_alert_seconds` (default 86400), it sends an HTTP GET to `ping_url`.
- Otherwise it sends an HTTP POST to `ping_url` + `/fail`. The body holds one line per problem: the check's name, the source name, and an age or a count (for example `source personal-email stale 1200s` or `terminal 2 in 24h`). It never holds `last_error`, exception text or receipt details, which stay on the host in `status`.

A new terminal failure therefore raises one alert, which clears itself after `terminal_alert_seconds` without an acknowledgement step. The operator handles it with `failures --status terminal` and `retry`.

The external monitor alerts when pings stop arriving or when a failure ping arrives. That covers what the healthcheck can't: the host down, Docker down, or the whole container gone. The URL comes from an env var (`${HEALTH_PING_URL}`), because a ping URL is a capability: anyone holding it can send pings. With no `ping_url` key, there's no ping. The key stays commented out in the example config, because the config loader refuses any `${VAR}` whose variable is unset ([ADR-0006](../adr/0006-config-based-source-definitions.md)). An uncommented key with `HEALTH_PING_URL` missing would stop every command, including the healthcheck and a rollback `ingest`. Rollout step 4 sets the variable first and uncomments the key after. The success GET and the `/fail` POST follow the conventions of Healthchecks.io-style monitors. A monitor without a failure endpoint still alerts on missed pings. **Which monitor to use, and its grace period, is the operator's choice** at deployment. The runbook suggests a 15-minute grace period, three missed pings.

### 10. Latency target

| Target | What it takes | Running cost | Operational cost |
|---|---|---|---|
| **Hours** (for example, hourly) | Sync state ([§ 5](#5-sync-state)) and retries ([§ 6](#6-retries)) in the one-shot `ingest`, on an hourly cron line. No `serve`, no IDLE. | Lowest: 24 runs a day, each reading only new items. | Low: health is still "did the cron line log something", unless `status --check` runs as its own cron line. |
| **5 minutes** (default) | `serve` with IMAP IDLE plus a 4-minute safety poll, and Drive polled every 2 minutes with 60 seconds of settle time. | Small: one idle IMAP connection per source, about 720 Drive calls a day. LLM cost is per receipt, not per poll, so it doesn't change. | A long-running process to keep healthy, covered by [§ 9](#9-health). |
| **Under a minute** | All of the above, plus Drive push (`changes.watch`) with a public HTTPS endpoint and channel renewal. | Small. | High: public exposure, certificates, channel renewal. Not justified by any current need. |

**The target is 5 minutes, configurable, as signed off.** All the intervals that set it are config values, with defaults chosen to meet it: `idle_reissue_seconds`, `imap_poll_seconds`, `drive_poll_seconds`, `drive_settle_seconds` and the health thresholds. The operator can trade latency for load without a code change. Raising `imap_poll_seconds` and `drive_poll_seconds` to 3600 gives roughly the "hours" behaviour from the same process, and the default stale thresholds follow ([§ 9](#9-health)).

**What the target means:** for an item that arrives while its source and the LLM API are reachable, a search can find it within 5 minutes.
- IMAP with IDLE working: seconds, plus processing time (about 10–60 seconds, mostly the LLM call and Chromium).
- IMAP if IDLE silently breaks: at most `imap_poll_seconds` (240 by default, which leaves a minute for processing) plus processing.
- Drive: at most `drive_poll_seconds` + `drive_settle_seconds` + processing, about 4 minutes.
- A bulk drop of many items queues behind the single worker, so the last one's latency grows with the batch. That's accepted: completeness comes before latency.
- Transient pauses ([§ 6](#6-retries)) also extend latency, and `status` reports them.

### 11. Configuration

The YAML config ([ADR-0006](../adr/0006-config-based-source-definitions.md)) gains a top-level `ingest:` block and a few new fields for each source. Every new field has a default, so existing configs load unchanged.

```yaml
ingest:
  idle_reissue_seconds: 600
  imap_poll_seconds: 240        # safety poll; also the poll interval without IDLE
  drive_poll_seconds: 120
  drive_settle_seconds: 60
  item_timeout_seconds: 600
  transient_item_max_seconds: 21600
  retry_max_attempts: 3
  retry_backoff_seconds: [300, 1800, 7200, 43200]
  health:
    heartbeat_stale_seconds: 120
    # source_stale_seconds: 900   # optional; default is 3x each source's poll interval
    terminal_alert_seconds: 86400
    # ping_url: ${HEALTH_PING_URL}  # add only once HEALTH_PING_URL is set (see below)
    ping_interval_seconds: 300

sources:
  - name: personal-email
    type: imap
    host: mail.example.com
    username: user@example.com
    password: ${IMAP_PASSWORD}
    folder: INBOX.Receipts
    archive: true                          # default false
    archive_folder: INBOX.Receipts.Filed
    needs_attention_folder: INBOX.Receipts.Needs-Attention

  - name: scanned-receipts
    type: gdrive
    folder_id: "1aBcDeFg..."
    token_json: ${GDRIVE_TOKEN_JSON}       # becomes ${GDRIVE_STORE_TOKEN_JSON} at the switch to the store account
    archive: false                         # true needs the evidence store's Drive backend and scope
    needs_attention_folder_id: "1hIjKlMn..."
```

Validation fails at load time, with the config error style used today, when:
- `archive: true` is set without the matching folder fields, or on a Drive source while the store backend isn't Drive;
- an explicit `source_stale_seconds` is below a poll interval it applies to;
- `retry_backoff_seconds` has fewer than `retry_max_attempts - 1` entries;
- an interval is below a floor: 30 seconds for polls, 60 seconds for IDLE re-issue.

Per-source interval overrides are left out until a source needs one.

### 12. CLI summary

| Command | Change |
|---|---|
| `serve` | **New.** Runs watchers, the worker, retries and health until SIGTERM. `--source NAME` (repeatable) limits it to some sources. |
| `ingest` | Now incremental (reads sync state), takes the source lock, honours `archive`. Exits 3 if a `serve` holds the source lock. Exit 1 on any failed item is unchanged. |
| `backfill` | **New.** See [§ 8](#8-backfill). |
| `retry` | **New.** See [§ 6](#6-retries). |
| `status` | **New.** See [§ 9](#9-health). |
| `failures` | Gains `--status`, `--since` and `--source`. The default output is unchanged. |

All new commands support `--output json` (RD-03).

### 13. Deployment and rollback

**The target compose layout** (`deploy/docker-compose.yml`):

- A new **`ingest`** service: the same image, `entrypoint: ["receipt-index", "serve"]`, `restart: unless-stopped`, the `healthcheck` from [§ 9](#9-health), `init: true`, `shm_size: 1gb` and `stop_grace_period: 11m` ([§ 1](#1-process-model)), with the env, config and data mounts the scheduler has today.
- The **`scheduler`** service stays for the rclone mirror and backup lines. The `*/15 … receipt-index ingest` line leaves `deploy/crontab`. Its 5-minute grace period can drop back to the default once no ingest runs there.
- `HEALTH_PING_URL` is added to `deploy/example.env`, and `deploy/example.receipt-index.yaml` gains the `ingest:` block.

**Rollout, in order:**

1. Ship tickets 1 and 2 (sync state and retries) into the one-shot `ingest`, still on cron. Each 15-minute run then reads only new items and runs due retries. Watch a few days of runs.
2. Run `receipt-index retry --status failed --all --yes` once by hand to work through the historical failures.
3. Deploy `serve` with every source on `archive: false`, and remove the cron ingest line in the same change. Even if the cron line is left by mistake, the source lock makes it exit 3 rather than race.
4. Set up the external monitor, add `HEALTH_PING_URL` to the server `.env`, then uncomment `ping_url` in the config. Confirm that one deliberate stop (`docker compose stop ingest`) raises an alert.
5. After a week with `status --check` clean and `backfill --dry-run --since <cutover - 7 days>` reporting zero unseen items, set `archive: true` on IMAP sources. From then on, the sweep files new outcomes. Filing the back catalogue is a separate, deliberate step: `backfill --archive --dry-run` first, then `backfill --archive`.
6. After the evidence store's Drive backend and scope change land, set `archive: true` on Drive sources and run `backfill --originals-only` to drain the dropoff into `originals/`.

**Rollback**, at any step after 3: revert the cutover change in `deploy/` (git revert of ticket 7's commit), which removes the `ingest` service and restores the cron line. Then run `docker compose -f deploy/docker-compose.yml up -d --remove-orphans`, which removes the `ingest` container and restarts the scheduler with the old crontab. A plain `docker compose stop ingest` isn't enough, because the next `up -d` would start it again. The schema changes are additive, so the one-shot `ingest` keeps working against them. Messages and files that were already moved stay where they are. They were processed, so nothing is lost, and `backfill` searches the archive folders if anything needs re-reading. Rolling back step 1 brings back today's full rescan. Before running the down migrations for `sync_state` and the `ingest_log` changes, drain pending retries (`failures --status retrying` and `--status queued` empty, after an `ingest`): the old code counts any `ingest_log` row as processed, so an item still waiting for its retry would never be tried again. The down migration deletes `started` rows, so the old status constraint can be restored.

---

## Implementation Plan

Proposed tickets. They are filed after the operator signs off this design, not before. Each lists the files it's expected to touch.

1. **Sync state and incremental reads in the one-shot `ingest`.** The `sync_state` migration (with grants and the `conftest.py` mirror); the adapter split into `list_new` and `fetch`; the pipeline split into `process_item`; header-first IMAP fetch; the UIDVALIDITY reset; the Drive changes feed with bootstrap, settle time (on the change's `time`), the folder and unsupported-type filtering and the invalid-token path; verified IMAP TLS; the Drive account ID in `sync_state` with re-bootstrap on change, and the check that a shared dropoff folder shows in the store account's feed (with the `files.list` fallback); the source lock and the item lock, with the lock-connection check and the re-check after locking; the `ingest_log` migration that adds the `started` status and `source_name` (with the `INSERT (source_name)` grant, and a down migration that deletes `started` rows first), and the `started`-row write, which the cursor rule needs; the first keys of the `ingest:` config block (`drive_settle_seconds`). It stands on its own and already pays off under cron.
   Files: `db/migrations/receipt/0000NN_create_sync_state.*.sql`, `db/migrations/receipt/0000NN_ingest_log_started.*.sql`, `tests/integration/conftest.py`, `src/receipt_index/config.py`, `src/receipt_index/adapters/{base,imap,gdrive}.py`, `src/receipt_index/pipeline.py`, `src/receipt_index/repository.py`, a new `src/receipt_index/locks.py`, `src/receipt_index/cli.py`, `pyproject.toml` (`imapclient`), and the unit and integration tests.
2. **Retries and failure surfacing.** The `attempt` and `next_retry_at` columns and grants; the `TransientError` boundaries, the per-item transient cap, the per-call timeouts and `counts_attempt`; the two pick-up checks; the retry-scheduler function, run once by the one-shot `ingest`; the shared re-fetch path (three folders, fallback-hash search); `retry`; `failures --status/--since/--source`; the retry and timeout keys of the `ingest:` block.
   Files: a migration, `conftest.py`, `config.py`, `pipeline.py`, `extraction.py`, `repository.py`, `cli.py`, `models.py`, `docs/user/cli-reference.md`, tests. Depends on 1.
3. **`serve` with IMAP IDLE and Drive polling.** Watcher threads, the worker queue, the retry scheduler on a timer (ticket 2's function), one Google client per thread, the health-loop skeleton with the watchdog, SIGTERM handling (abandoning a pause uncounted), the remaining `ingest:` keys (IDLE and poll intervals).
   Files: a new `src/receipt_index/service.py`, `adapters/imap.py`, `config.py`, `cli.py`, `example.env`/`deploy/example.receipt-index.yaml`, tests (a fake IMAP client for the IDLE loop; GreenMail for an end-to-end IDLE wake-up where it supports IDLE). Depends on 1 and 2.
4. **Health.** The heartbeat, `status` and `status --check`, the dead-man's ping. Ticket 3 creates the health loop with its watchdog; this ticket adds the rest.
   Files: `service.py`, a new `src/receipt_index/health.py`, `repository.py`, `cli.py`, `config.py`, tests. Depends on 3.
5. **`backfill` (full mode).** Date-range enumeration across the watched, archive and needs-attention folders, through ticket 2's shared fetch path.
   Files: `adapters/{imap,gdrive}.py`, `pipeline.py`, `cli.py`, `docs/user/cli-reference.md`, tests. Depends on 1 and 2. `--originals-only` follows the evidence store's schema and originals tickets; `--archive` lands with ticket 6.
6. **Dropoff archive.** The `archive` config and its validation; setting and reading `sync_state.archive_enabled_at` (the column comes with ticket 1's table); IMAP `MOVE` and the UIDPLUS fallback; the IMAP and Drive archive sweeps, run by the worker; needs-attention moves; the Drive adopt and needs-attention moves; the crash-window recovery paths; `backfill --archive`.
   Files: `adapters/{imap,gdrive}.py`, `pipeline.py`, `service.py`, `config.py`, tests. IMAP depends on 3 and on the evidence store's IMAP originals ticket, and Drive on the evidence store's Drive backend and scope ticket.
7. **Production cutover.** The `ingest` service in `deploy/docker-compose.yml`, `deploy/crontab`, `deploy/example.env`, `deploy/example.receipt-index.yaml`, the runbook steps in `docs/user/server-deployment.md` (rollout and rollback above, and monitor setup), `docs/user/troubleshooting.md`. The Drive sources' `token_json` switch to `${GDRIVE_STORE_TOKEN_JSON}` isn't part of it: that lands with the evidence store's Drive ticket. Depends on 3 and 4.

Suggested order: 1 → 2 → 3 → 4 → 7, with 5 any time after 1 and 6 after the matching evidence-store tickets.

---

## Testing Strategy

- **Unit, IMAP watcher.** A fake `imapclient` drives the IDLE loop through:
  - `EXISTS`;
  - a timeout with re-issue;
  - a disconnect during IDLE and a catch-up after reconnect;
  - a server without `IDLE` (poll fallback);
  - a server without `MOVE`, with and without `UIDPLUS`;
  - an authentication error, which mustn't loop.
- **Unit, UIDVALIDITY.** A changed value resets the cursor and triggers a full header scan that queues only unseen `Message-ID`s.
- **Unit, IMAP TLS.** The client is built with a verifying SSL context.
- **Unit, Drive feed.** Bootstrap order (token before listing); filtering by parent, with folders dropped; an unsupported file type recorded as `skipped` so the token passes it; settle time on the change's `time` holding the token back; the invalid-token rebootstrap; pagination.
- **Unit, retries.** Backoff schedule; terminal after max attempts; transient errors not using up an attempt; an item that always raises the same transient error giving way after `transient_item_max_seconds`, so later items still run; a source-wide refusal pausing without using attempts; an item over `item_timeout_seconds` recorded as an item error; a dead `started` row using up an attempt; the two pick-up checks over mixed `ingest_log` histories (a re-read never jumps a pending retry); the re-fetch path finding a message in needs-attention and archive, and a fallback-hash message by its headers; existing failed rows terminal after the migration; the one-shot `ingest` running due retries.
- **Unit, cursor rule and crash windows.** The cursor never passes an item without a committed `started` row. After a simulated crash in each window of [§ 7](#7-dropoff-archive), the item is recovered by source ID. The archive sweep moves a message committed but not moved.
- **Unit, locks and races.** The scheduler, after taking the lock, finds a committed outcome and does nothing (a skipped item never becomes an automatic retry). The worker drops an item that `backfill` finished between queueing and locking. A locked item holds the cursor back until it has a record.
- **Unit, shutdown and watchdog.** SIGTERM during a transient pause writes an uncounted row and exits promptly. The watchdog exits the process when an item outruns `item_timeout_seconds`, and the restart charges one attempt.
- **Unit, Drive account and sweeps.** A changed account ID re-bootstraps the source. Both sweeps move only outcomes after `archive_enabled_at`, leaving the back catalogue to `backfill --archive`.
- **Unit, health.** The stale threshold follows the poll interval; the heartbeat survives a slow item but not a wedged worker; the `/fail` POST body carries no error text; a new terminal failure turns the ping to `/fail` until `terminal_alert_seconds` passes.
- **Integration (Postgres).** The `sync_state` and `ingest_log` migrations up and down, with grants checked under `receipt_index_dev_write`. Source-lock contention between two connections (the second `ingest` exits 3), and the lock re-taken after its connection is killed. Item-lock skipping between `backfill` and a worker. The `ingest_log` down migration succeeding with `started` rows present.
- **Integration (GreenMail).** End to end: a message delivered by SMTP while `serve` is in IDLE is indexed without a poll, and `UID MOVE` lands it in the archive folder. GreenMail's IDLE and MOVE support is confirmed in ticket 3; any path it lacks stays covered by the fake client only, and the ticket says which.
- **Fail-first.** Each new test is seen to fail against the unchanged code, or with its fixture broken, before it's trusted, following the project's checks discipline.
- **Operational** (ticket 7): a deliberate `docker compose stop ingest` alerts within the monitor's grace period; a test receipt mailed to the watched folder is searchable within 5 minutes; `backfill --dry-run` after a week reports zero unseen items.

---

## Risks

| Risk | Mitigation |
|---|---|
| IDLE silently stops firing (a half-open TCP connection, provider quirks) | Safety poll every `imap_poll_seconds`; IDLE re-issued every 10 minutes; TCP keepalive. |
| A long-running process leaks memory or wedges | A fresh Chromium per item; the heartbeat and `status --check`; the dead-man's ping alerts. A manual restart is safe at any time. |
| A bug moves mail or files that weren't processed | Archiving is off by default and enabled per source only after the rollout's verification week. It moves only items with a committed final outcome. Nothing is ever deleted (needs-attention is cleared by hand). Drive moves only between the configured dropoff, needs-attention and the store's `originals/`, a restriction the evidence-store design enforces in its Drive client. |
| A UIDVALIDITY reset on a very large folder | One header-only `FETCH`. Idempotency keyed on `Message-ID` makes it duplicate-free. It's counted in `resets` and shown in `status`. |
| Messages without a `Message-ID` | Today's fallback hash (subject, date, sender) is kept, using the same headers fetched by the header-first read. |
| An LLM outage or a lapsed key burns retries | Transient errors and source-wide refusals pause the worker without using up attempts, and a long pause fails `status --check`. |
| One item blocks the single worker | Per-call timeouts, a watchdog that restarts the process, a cap on transient retries per item, and an attempt charged for each crash recovery all end in an ordinary item error. |

---

## Out of Scope

- Drive push notifications (`changes.watch`); see [§ 4](#4-drive-changes-feed) for when they'd pay off.
- An HTTP interface or remote search API. `serve` is where one would live later, but none is designed here.
- Parallel workers.
- Watching more than one folder per IMAP source. Configure one source per folder.
- Deciding the Drive OAuth scope, the storage interface or the provenance schema (all owned by the evidence-store design).
- Re-extraction with new prompts or fields (owned by the receipt data-model design). `retry` and re-extraction share the item-fetch path.

---

## Open Questions

| Question | Who decides | When |
|---|---|---|
| Which external monitor receives the dead-man's ping, and its grace period | The operator | At the production cutover (ticket 7) |
| Whether GreenMail supports IDLE and `MOVE` well enough for an end-to-end test | The implementer of ticket 3 | During ticket 3; the fallback is the fake client |
| Whether the store account can move an operator-owned file, or the copy fallback applies | The evidence store's Drive ticket checks it, and the operator confirms | That ticket's first step; Drive `archive` follows the result |
| Whether the store account's changes feed reports files added to a folder shared with it | The implementer of ticket 1, against a scratch folder | During ticket 1; the fallback is listing the dropoff folder each poll |
