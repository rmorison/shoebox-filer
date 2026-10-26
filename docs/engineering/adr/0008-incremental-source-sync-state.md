# ADR-0008: Incremental Source Reads with Stored Sync State

## Status

Accepted

Replaces the always-on service plan's acceptance of the full-mailbox scan (`docs/plans/2026-08-01-001-feat-always-on-docker-service-plan.md`, scope boundary "The O(N) IMAP enumeration issue is not addressed here"). It also replaces the same boundary in the always-on service brainstorm ("The O(N) IMAP enumeration issue is not solved here; it bounds how cheap frequent polling is, not whether this works").

Design: [Always-On Ingest](../designs/always-on-ingest.md).

## Context

Each ingest run reads every source from the beginning:

- The IMAP adapter runs `SEARCH ALL` and downloads every full message before comparing its `Message-ID` with the processed set.
- The Drive adapter lists the whole folder.

Idempotency (RD-07) makes that correct, but the cost grows with the size of the mailbox and the folder, not with what is new. At a 15-minute cron interval that cost was tolerable. With a 5-minute target and a long-running service ([ADR-0007](0007-always-on-ingest-service.md)), a source has to be read from where the last read stopped.

## Decision

1. **Each source's read position is stored in a `receipt.sync_state` table**, one row per (source name, folder).
   - IMAP rows hold `UIDVALIDITY` and the last UID read.
   - Drive rows hold the changes-feed page token and the ID of the Drive account whose change log it belongs to. A different account means a re-bootstrap.
   - Every row records the last poll, the last success, the last error and a reset count, which the `status` command and the health check read.
2. **IMAP reads only UIDs above the stored one** (`UID SEARCH UID <last+1>:*`), and fetches only the headers needed for the source ID before deciding whether to download a body.
3. **When `UIDVALIDITY` changes**, or a source has no row yet, the cursor is reset and the folder is read once with a header-only scan. Idempotency stays keyed on `Message-ID`, never on UID, so a reset can't create duplicates. Resets are counted and shown.
4. **Drive reads the changes feed** from the stored page token, filtered to the dropoff folder. Bootstrap takes the start token *before* listing the folder, so no file is missed in between. An invalid token falls back to the bootstrap.
5. **The cursor never passes an item that has no committed record.** An item is owned by the cursor until it has a committed `started` row in `ingest_log` or an existing record (a receipt or an earlier `ingest_log` row), and by the retry machinery afterwards. Losing in-memory work therefore means re-reading it, never skipping it.
6. **Sync state is a cache of where to start reading, not the record of what was processed.** Deleting a row is always safe: the source re-bootstraps, and the processed-ID check (receipts, `ingest_log`, and later the evidence store's provenance) decides what is new.

## Consequences

### Positive

- A poll costs one search plus the new items, whatever the size of the source. That's what makes a 4-minute safety poll and a 2-minute Drive poll cheap.
- The one-shot `ingest` gets the same saving, so it pays off even before `serve` exists.
- The sync-state row gives health reporting a per-source "last read" without a separate heartbeat table.

### Negative

- A new table and migration, mirrored in the integration-test migration helper.
- A UIDVALIDITY change or a source rename costs one full header scan. It's cheap, but it's a code path that has to be tested.
- The Drive changes feed covers the whole Drive, so each poll pages through changes made anywhere and filters them in memory. That's negligible for a personal Drive.

### Alternatives Considered

- **Keep the full scan and rely on idempotency.** Correct, but the cost grows without bound and the 5-minute target would mean downloading the whole mailbox every few minutes.
- **IMAP `SEARCH SINCE <date>`.** It works at day granularity only, uses the server's internal date (which RFC 3501 and RFC 6851 say a server SHOULD, not must, keep when a message is copied or moved) and still re-reads the whole day.
- **IMAP CONDSTORE/QRESYNC (`MODSEQ`).** More precise, but not every server supports them, and the extra precision (flag changes, expunges) isn't needed: only new messages matter.
- **Drive `files.list` filtered by `modifiedTime`.** It misses files whose timestamps come from the scanner app's clock, and it still re-lists the folder. The changes feed is the API's own incremental mechanism.
- **The position kept in a local file.** It would be lost with the container and invisible to `status`. The database already holds the processed set, and the position belongs next to it.
