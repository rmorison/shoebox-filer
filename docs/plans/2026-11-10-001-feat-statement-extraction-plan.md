---
title: "feat: Statement extraction and the ledger's read contract"
type: feat
status: approved
date: 2026-11-10
origin: docs/brainstorms/2026-11-10-statement-extraction-requirements.md
---

# feat: Statement extraction and the ledger's read contract

## Overview

Read PDF bank and card statements from their own Drive folder, keep each original in the evidence store, and extract every account and line with Haiku: a first pass that classifies the document and lists its accounts, then one call per account. Each account is checked against the statement's own printed figures. A clean account's lines are served to the ledger through new read-only MCP tools under a published, versioned contract, with a change feed. An account that fails a check is held, with its failure class, difference and page range, and contributes no line. The same pull request as this plan renames the roadmap's "Reconciliation" lane to document extraction.

The ledger's session pre-reviewed the read contract and found it good to build, and the operator gave the go on 2026-11-10 (see Open Questions). The build starts when the lead says so.

---

## Problem Frame

See the origin document. In short: the ledger imports bank lines as facts and classifies them. Periods with no structured download exist only as PDF statements. A feasibility spike, run outside this repository on real statements that never came here, chose the method (text layer to Haiku, first pass plus one call per account) and the checks. The operator signed off the go and the design decisions on 2026-11-09. Those decisions are not reopened here.

---

## Requirements Trace

From the origin document:

- R1–R4 intake: own Drive source, inside `serve`, original kept, scans rejected → U1, U2, U5, U6
- R5 statement bounds → U1, U5
- R6–R7 document-type gate → U3, U5
- R8–R10 extraction, loans as printed figures → U3
- R11–R13 gates, held never partial, completeness → U4
- R14 extraction history → U2, U5
- R15–R22 read contract, change feed, original over MCP, CLI → U7, U8
- R23 lane rename → this pull request (`docs/product/roadmap.md`)

---

## Scope Boundaries

Carried from the origin document: no scans, no structured-file import, no classification or matching, no computed loan figure, no override, no IMAP statement source, no chaining of a missing beginning balance, no receipts change feed (only its cursor convention is agreed here).

### Deferred to follow-up work

- The receipts change feed, using this plan's cursor convention.
- A `search_statements` MCP tool. The ledger reads through the feed, and the CLI's `statements list` covers the operator. Adding a tool later isn't a breaking change.
- Diagnosing why an account doesn't foot (sign error, missed section, page break). This version holds it as `does_not_foot` with the difference and pages. New failure classes are additive.
- Prompt caching of the document text across a statement's calls (see Cost).
- An automatic retry of a held statement, if production shows the first pass is unstable.
- An IMAP statement source.
- The Drive storage backend: statements use the local store like everything else today.
- A command that lists store files no longer referenced by any record (none exists today; see Rollback).

---

## Context & Research

### Relevant code and patterns

- `src/receipt_index/pdf_validate.py`: `PdfBounds` and `PdfLimits` already expect a second kind of document ("a statement gets its own bounds rather than loosening these"). `run_bounded` runs a job in a limited child and returns None on any stop, with the reason only in the log.
- `src/receipt_index/pdf_reader.py`: `read_pdf` reads text in that child. It joins pages with blank lines, so page numbers are lost. Statements need per-page text.
- `src/receipt_index/llm.py`, `extraction.py`: pydantic-ai on Anthropic, a per-request timeout below `item_timeout_seconds`, `EXTRACTION_PROMPT_VERSION`, token usage recorded per run.
- `src/receipt_index/config.py`: `item_timeout_seconds` (1200, never lower) is set as the sum of every deadline one receipt item can meet. The `serve` watchdog exits when one item step runs longer.
- `src/receipt_index/adapters/gdrive.py`, `service.py`, `pipeline.py`: the Drive watcher, the worker, `process_item`, item locks, `started` rows and the retry scheduler (always-on ingest design §§1, 5, 6).
- `src/receipt_index/repository.py` `select_due`, `fetching.py` `SourcePool`, `pipeline.py` `run_due_retries`: retries find an item's source by `source_name`, fall back to every configured source of the same `source_type`, and always run the receipt `process_item`.
- `src/receipt_index/store.py`, `originals.py`: `put(purpose="original", folder=StoreFolder.for_original(...))` under `originals/`, write-once, with `_put_original` reusing bytes already stored. Under the local backend a Drive item's original is left `pending` today. Statements store a copy instead (U5).
- `src/receipt_index/mcp_server.py`, `paging.py`: read-only tools, one `REPEATABLE READ, READ ONLY` snapshot per call, the access-logged `get_receipt_file`. `check_read_only_role` already checks every table in the `receipt` schema for write privileges, so new tables there are covered.
- `tests/integration/conftest.py` `_run_migrations()`: every migration is mirrored there.
- Receipts already have a kind `statement`, for statements that arrive as email receipts. They stay as they are; this feature doesn't read or move them.

### External input

- The feasibility spike's findings, summarised on the statement-extraction issue. Only its conclusions come here: no statement, count, layout or model output from it.
- The ledger's session, asked one question at a time. Answers are recorded under [Contract decisions agreed with the ledger](#contract-decisions-agreed-with-the-ledger).

---

## Key Technical Decisions

- **One schema.** Statement tables go in the `receipt` schema, next to the evidence-store tables they reference. A new schema would need its own migration directory, migration table, grants, and Makefile and deploy changes, for no isolation the read role needs. The MCP role check already covers every table in the schema.
- **Statement ID per original.** One statement per distinct PDF (SHA-256 of its bytes). The same bytes dropped twice are one statement with two source rows. Different bytes for the same account and period are two statements.
- **Every run is a revision.** Each processing run of a statement, a rejection included, writes one immutable revision that holds its document type, status, period, model, prompt versions, tokens, accounts and lines. The statement row only points at its current revision and copies its status and type for the feed. Status and type change only with a new revision.
- **One counter for the feed and for revisions, never going backwards.** Every transaction that writes a revision takes one transaction-level advisory lock, then sets the next value to the greater of the last value plus one and the current time in microseconds since the epoch, in one `UPDATE … RETURNING` on a one-row state table. The value is both the new revision's number and the statement's `change_seq`. A trigger enforces it, so no code path can skip it, and statement writes run at `READ COMMITTED`.
  - Writers commit in counter order, and Postgres makes a commit visible before it releases the lock, so a reader that has seen *n* has seen every change below it.
  - A timestamp alone can skip a row whose transaction commits late. A transaction-ID cursor doesn't survive a dump and restore. A bare sequence goes backwards when an older backup is restored, and so would a per-statement revision count. Then the ledger's stored cursor would skip new changes, or a revision number it already applied would come back with different content. The clock floor keeps every new value above anything handed out before a restore, as long as the server's clock doesn't run backwards.
  - Writes are a handful a day from one worker, so the lock costs nothing. The receipts feed uses the same convention.
- **Statement items are their own source type in `ingest_log`.** Rows carry `source_type = 'gdrive_statement'`, so the retry scheduler's fallback to "any source of the same type" never hands a statement to a receipt source, and `run_due_retries` dispatches by type.
- **Each phase is its own watchdog step.** The check, the read, the first pass and each account call re-mark the item clock, and a statement lists at most 8 accounts. Each step then stays under `item_timeout_seconds` by the same sum-of-deadlines rule as receipts (U5).
- **Pages kept.** The statement read returns per-page text. The prompt marks pages, so the first pass can give page ranges and each line its page.
- **Amount sign: the statement's own balance.** Decided in the origin document, agreed with the ledger with conditions (below).
- **No new dependency.** pdfplumber, pydantic-ai and weasyprint (for test fixtures) are already in the project.

---

## The Read Contract (proposed, for the ledger's pre-review)

Published at build time as `docs/engineering/designs/statement-read-contract.md`, the single source for its shapes. Summary:

### Versioning

- Every response carries `contract_version` (integer, starts at 1).
- **Additive, not breaking:** a new field, a new document type for a rejected document, a new failure class. The contract document notes each. The ledger ignores fields it doesn't know and treats an unknown failure class as held. A new field stays out of `content_digest` until a version change.
- **The ledger's safe defaults:** it imports lines only from an account whose `status` is `clean` and whose `account_type` is `deposit`, `card` or `loan`. It treats any other status or type as held.
- **Breaking:** removing or renaming a field, changing a field's meaning or sign, a new `account_type`, a new account `status` that carries lines, or a change to the digest's field list. A breaking change ships as new tools beside the old ones (for example `get_statement_v2`), and the old ones stay until the ledger has moved.

### Tools (read-only, on the existing MCP server)

| Tool | Returns |
|---|---|
| `list_statement_changes(cursor?, limit)` | Statements created or changed after `cursor`, oldest change first, each as `{statement_id, revision, status, document_type, source_sha256}`, plus `next_cursor`. No cursor means from the start. |
| `get_statement(statement_id, revision?)` | One statement revision, with its accounts and the lines of clean accounts. The current revision unless one is named. |
| `get_statement_file(statement_id, include_content?)` | The original PDF, access-logged like `get_receipt_file`. |

### Change feed

- The cursor is opaque to the ledger (today `"s1:<change_seq>"`). It survives restarts, redeploys and database restores, and never goes backwards.
- A statement appears once per revision. A later revision moves it, so the feed always shows its latest. Skipped revisions can still be fetched with `get_statement(id, revision)`.
- Rejected documents are in the feed too, as revisions with no accounts, so a dropped file never vanishes silently.
- Every run is a new revision and a feed entry, even when its content is identical to the last one. The account digests then show that nothing changed.
- `revision` numbers are increasing for a statement and never reused, even across a restore.
- At-least-once delivery: the ledger applies by `(statement_id, revision)`, so a repeat is harmless.
- `next_cursor` is the last row's cursor, or the request's cursor when no rows follow.
- **After a restore from an older backup:** changes made after the backup are lost from shoebox. The Drive files are still in the folder (Drive sources don't archive), and the restored sync state lists them again. Statements first read after the backup are re-read and get new `statement_id`s, and statements re-run after the backup go back to their earlier revision. The ledger's rule: a new `statement_id` whose `source_sha256` matches a statement it knows is that statement, and its account digests are compared with what it imported.

### Per statement (one revision)

| Field | Meaning |
|---|---|
| `statement_id` | UUID, stable for the original's bytes within one database history (see the restore rule) |
| `source_sha256` | Hex SHA-256 of the original PDF |
| `revision`, `extracted_at` | This revision's number and when it ran |
| `document_type` | `deposit`, `card`, `loan_statement`; or, for a rejected document, `loan_bill`, `tax_or_escrow_notice`, `not_statement`, `scan`, `unreadable` (not a PDF, encrypted, or one pdfplumber can't open), `over_bounds` |
| `rejected_bound` | For `over_bounds`: `pages`, `size`, `text`, `memory` or `accounts`; else null |
| `status` | `complete`, `incomplete`, `held` or `rejected` (below) |
| `period_start`, `period_end` | As printed; null when not printed |
| `printed_account_count` | Accounts the document's own summary lists; null when it prints none |
| `pages` | Page count |
| `extractor` | `{model, prompt_version, first_pass_version}`; null fields for a rejection before any model call |
| `accounts` | Below, in document order |

Statement status:
- `rejected`: not extracted; `document_type` says why.
- `held`: a completeness failure (a listed account came back empty and isn't dormant, the printed account count disagrees, or a deposit, card or loan statement lists no accounts). Every account is held, with `dropped_account` or `account_count_mismatch`, since the account list itself can't be trusted. Also `held` when no account is clean.
- `incomplete`: the list is complete, at least one account is held by its own gates and at least one is clean or figures-only. Clean accounts pass their lines.
- `complete`: every account is clean or figures-only.

### Per account

| Field | Meaning |
|---|---|
| `account_id` | UUID, stable within a revision only |
| `ordinal` | 1-based position in the document |
| `match_key` | `{account_type, institution, last4, occurrence}`, where `occurrence` counts accounts with the same type, institution and `last4` in document order. The key that pairs this account with itself in another revision. An account in one revision and not in the other is added or removed, for the ledger's review |
| `institution` | As printed |
| `last4` | Last four digits only, never a full number; null when none is printed |
| `account_type` | `deposit`, `card`, `loan` |
| `status` | `clean`, `held`, or `figures_only` (loans) |
| `beginning_balance`, `ending_balance` | As printed, on the statement-balance basis; null when not printed |
| `section_totals` | `[{section, amount}]`: each printed section total, as an unsigned magnitude |
| `column_totals` | `[{section, credits, debits}]`: a table's printed column totals, as unsigned magnitudes; `section` null for a table covering the whole account. Credits are the lines that raise a deposit balance or lower a card or loan balance |
| `line_order` | `oldest_first` or `newest_first`, as printed |
| `date_grace_days` | Days before `period_start` a posted date may fall and still pass: 7 for a card, 0 otherwise |
| `failure_class`, `difference`, `first_page`, `last_page` | For a held account: why, the ending balance less (beginning plus lines) where both are printed, and where to look |
| `loan` | For a loan: `principal_before` and `principal_after` on the statement-balance basis; `principal_paid`, `interest`, `escrow`, `fees` and `advances` as unsigned printed amounts. Each null when not printed |
| `content_digest` | Hex SHA-256 of the account's canonical form (below) |
| `lines` | Clean accounts only; empty for held and loan accounts |

**Canonical form for `content_digest` (contract version 1).** A JSON object of exactly these fields: `account_type`, `institution`, `last4`, `status`, `failure_class`, `difference`, `beginning_balance`, `ending_balance`, `section_totals`, `column_totals`, `line_order`, `loan`, and `lines`, each line with exactly `posted_date`, `transaction_date`, `description`, `amount`, `running_balance`, `section` and `page`. Keys sorted, no insignificant whitespace, UTF-8 with no `\u` escapes beyond JSON's required ones, text as extracted (no Unicode normalisation), money and dates as the strings the contract gives, integers as JSON numbers, nulls kept. Ordinal and IDs are left out, so renumbering alone doesn't change a digest. The contract document gives a worked example, so the ledger can recompute it.

Failure classes, for a held account: `no_beginning_balance`, `no_ending_balance`, `does_not_foot`, `date_out_of_period`, `balance_chain_break`, `totals_mismatch`, `dropped_account`, `account_count_mismatch`. A held account carries one: a completeness failure first, otherwise the first that applies in that order. A model or schema error makes no revision: the item is retried, and after `retry_max_attempts` it is a terminal failure in `ingest_log`, shown by `failures`, not in the feed.

### Per line (clean accounts only)

| Field | Meaning |
|---|---|
| `line_id` | UUID, stable within a revision only |
| `ordinal` | 1-based printed order within the account |
| `posted_date` | Date posted |
| `transaction_date` | Where printed, else null |
| `description` | As printed, wrapped lines joined; any account or card number cut to its last four |
| `amount` | Decimal string, signed by its effect on the statement's printed balance |
| `running_balance` | As printed on the line, else null |
| `section` | The printed heading it sits under, lower case |
| `page` | 1-based page |

Money is a decimal string with two places (`"-1234.56"`), dates are `YYYY-MM-DD`.

### Sign basis

Balances, lines and running balances on an account, and a loan's principal before and after, are on one basis: the statement's own printed balance. A line is positive when it raises that balance and negative when it lowers it. For a deposit account the balance is money held. For a card or loan it is money owed, so a purchase is positive and a payment negative. A card or loan balance printed as a credit (a `CR` suffix or a minus sign) comes through negative on the same basis and is never normalised. Section and column totals, and a loan's payment breakdown, are unsigned printed magnitudes.

To reach the ledger's debit-positive sign (like OFX `TRNAMT`), the ledger multiplies the signed figures of an account by +1 for `deposit` and −1 for `card` and `loan`. It doesn't flip unsigned magnitudes.

### What the ledger can re-check from one response

- Beginning plus the sum of the lines equals ending.
- In `line_order`, each printed running balance equals the previous printed balance (or the beginning balance) plus the amounts of the lines since it, lines with no printed balance included.
- Each section total equals the magnitude of the sum of that section's lines. Each column total equals the sum of the magnitudes of the lines on that side, within its section or the whole account.
- Every posted date lies between `period_start − date_grace_days` and `period_end`. When the period isn't printed, there is nothing to check dates against, and shoebox skips the check.

Shoebox runs the same checks, so the ledger doesn't have to trust its status.

### Contract decisions agreed with the ledger

Asked of the ledger's session, one question at a time, on 2026-11-10:

1. **Sign.** No blocker with the statement-balance sign, account type and section, the ledger converting. Conditions, all adopted above: balances on the same basis as the lines, so the whole account flips together; the flip stated in the contract by account type (deposit keeps its sign, card and loan negate); a card or loan balance printed as a credit comes through negative and is never normalised. The ledger's own balance check would catch a statement whose printed polarity breaks that assumption. (After the document review, totals and the loan breakdown are stated as unsigned magnitudes; see the review dispositions.)
2. **Revisions and duplicates.** Accepted as proposed: a statement ID per original's bytes, a new revision per re-extraction, account and line IDs stable only within a revision, and a second statement (no merge, no flag) for the same account and period in different bytes. Additions, adopted above: a monotonic `revision` number, and a `content_digest` per account covering its status and lines. The ledger's facts are immutable, so it never replaces imported lines: an unchanged digest is a no-op, held to clean imports, and changed lines or clean to held after an import go to the ledger's review. Shoebox only has to report revisions honestly. The ledger dedupes lines with its own fingerprint (posted date, amount, normalised description, position among identical lines that day, per account), which also catches overlap with a structured file of the same account.
3. **Change feed semantics.** Fits the ledger's poller as proposed: latest revision only (the ledger compares digests, so skipped revisions don't matter), at-least-once, an opaque cursor that the ledger advances only after applying a page. Rejected documents stay in the feed, so the owner sees them. Conditions, adopted above: the cursor survives restarts, redeploys and backup restores and never goes backwards; the contract states whether an identical re-extraction makes a revision (it does).

4. **Pre-review of this contract (on this plan's pull request, 2026-11-10).** Good to build, with no blocking changes. The document review's changes are accepted: revisions from the clock-floored counter, `match_key` with a digest over a fixed field list, unsigned totals and loan breakdown (the ledger flips only signed fields), and the restore rule. The ledger keys statements on `source_sha256` throughout and treats `statement_id` as shoebox's handle for fetching. After a restore it never treats a lower revision as newer: it compares digests. A statement that was clean and is now rejected, or an account present before and missing now, becomes a review item in the ledger if it imported anything, and never an automatic change. The ledger checks a loan's `principal_after`, negated, against its own balance.

---

## Open Questions

### Answered by the operator (2026-11-10)

The operator gave the go on this plan, after the ledger's pre-review of the contract, and answered the four open questions:

1. **Totals as a gate:** kept. A printed section or column total that disagrees with the lines holds the account (`totals_mismatch`).
2. **Card grace:** 7 days before the period start, as a statement-level constant, not configuration.
3. **Clean accounts of an incomplete statement** pass their lines, and a completeness failure holds every account.
4. **Automatic retry of a held statement:** none in this version; `statements rerun` by hand.

### Resolved during planning

- Schema: `receipt`. Migration numbers: 000023 and 000024 (main's last is 000022).
- Store: the local backend, a copy of the Drive file under `originals/`.
- Feed and revision numbers: the serialized, clock-floored counter above.
- No printed period: the date gate is skipped, and `period_start` null tells the ledger.

### Deferred to implementation

- The exact prompt wording, versioned from 1. Prompts describe layouts in general and contain no statement text.
- Bound values, tuned on the synthetic fixtures within the ceilings below.

---

## Implementation Units

Each unit is a pull request, merged in order. Every new check or fixture is seen to fail before it is trusted, with the command and output in its pull request.

### U1. Statement bounds and per-page reading

**Goal:** Read a statement's text, page by page, under its own limits, and say why when it can't.

**Files:** `src/receipt_index/pdf_validate.py`, `src/receipt_index/pdf_reader.py`, `tests/unit/test_pdf_reader.py`

**Approach:**
- `STATEMENT_BOUNDS = PdfBounds(check=PdfLimits(cpu_seconds=20, wall_seconds=60), read=PdfLimits(cpu_seconds=90, wall_seconds=270, memory_bytes=1 GiB), max_read_pages=60)`. Ceilings, not targets: a long statement is tens of pages. `RECEIPT_BOUNDS` doesn't change.
- A statement read returns per-page text, or a reason: `pages` (from the page count `text_up_to_pages` already returns), `size`, `memory`, `unreadable`, or `timeout` (CPU or wall clock). The receipt path keeps returning None.
- `timeout` is a retryable item error, not a rejection, since a timeout can be the host's load. The others are permanent rejections.
- A text cap: a statement whose text is over 300,000 characters is rejected (`over_bounds`, `text`) before any model call. That keeps each prompt well under the 100,000-token point where Haiku's price rises.
- A document whose text, across all pages, has fewer than 20 non-whitespace characters is a scan: rejected as `scan`.

**Tests:** a fixture over 60 pages is refused with `pages`; a CPU-bomb PDF stops with `timeout`; a non-PDF gives `unreadable`; receipts' bounds and return values are unchanged; per-page text keeps page order.

### U2. Schema

**Goal:** Tables for statements, their sources, revisions, accounts and lines, and the counter.

**Files:** `db/migrations/receipt/000023_statements.{up,down}.sql`, `db/migrations/receipt/000024_statements_grants.{up,down}.sql`, `tests/integration/conftest.py`, `src/receipt_index/statement_repository.py`

**Approach (shape; names final at build):**

```sql
CREATE TABLE receipt.statement_feed_state (   -- one row: the last value handed out
    id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id), last_value BIGINT NOT NULL
);

CREATE TABLE receipt.statements (
    id                     UUID        NOT NULL DEFAULT uuidv7(),
    original_file_id       UUID        NOT NULL,   -- receipt.stored_files
    source_sha256          BYTEA       NOT NULL,
    current_revision_id    UUID        NOT NULL,
    document_type          TEXT        NOT NULL,   -- copy of the current revision's
    status                 TEXT        NOT NULL,   -- copy of the current revision's
    change_seq             BIGINT      NOT NULL,   -- the current revision's number
    created_at, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_statements_original UNIQUE (original_file_id),
    CONSTRAINT uq_statements_change_seq UNIQUE (change_seq),
    ...
);

CREATE TABLE receipt.statement_sources (    -- one row per source item, as provenance is for receipts
    id, statement_id, source_name, source_id UNIQUE, file_name, received_at, created_at
);

CREATE TABLE receipt.statement_revisions (  -- append-only, guarded like receipt.extractions
    id, statement_id, revision BIGINT UNIQUE, document_type, status, rejected_bound,
    period_start, period_end, printed_account_count, pages,
    llm_model, prompt_version, first_pass_version, input_tokens, output_tokens, origin, created_at
);

CREATE TABLE receipt.statement_accounts (   -- append-only
    id, revision_id, ordinal, institution, last4 CHECK (last4 ~ '^[0-9]{4}$'), account_type, status,
    occurrence, line_order, date_grace_days,
    beginning_balance, ending_balance NUMERIC(14,2), section_totals JSONB, column_totals JSONB,
    failure_class, difference NUMERIC(14,2), first_page, last_page, content_digest BYTEA,
    principal_before, principal_after, principal_paid, interest, escrow, fees, advances NUMERIC(14,2)
);

CREATE TABLE receipt.statement_lines (      -- append-only; written only for clean accounts
    id, account_id, ordinal, posted_date, transaction_date, description, amount NUMERIC(14,2),
    running_balance NUMERIC(14,2), section, page
);
```

- A `BEFORE INSERT` trigger on `statement_revisions` takes `pg_advisory_xact_lock` on the feed key, then `UPDATE statement_feed_state SET last_value = greatest(last_value + 1, <microseconds since the epoch>) RETURNING last_value` and sets `revision` from it. Making the revision current sets `statements.change_seq` to the same value, inside the same transaction. A trigger on `statements` refuses a `status`, `document_type` or `change_seq` that doesn't match its current revision.
- A trigger refuses a `statement_lines` row on an account that isn't `clean`.
- No full account number anywhere: `last4` is four digits or null.
- 000024 grants: `_write` gets `SELECT, INSERT` on the append-only tables and on `statement_sources`, `SELECT, INSERT, UPDATE (current_revision_id, status, document_type, change_seq, updated_at)` on `statements`, and `SELECT, UPDATE` on `statement_feed_state`. `_read` gets `SELECT` on all of them.
- Down migrations drop the tables. See Rollback for the stored originals.
- `_run_migrations()` mirrors 000023; `apply_migration_grants` applies 000024.

**Tests:** up, down and up again on a scratch database; two concurrent writers get counter values in commit order; a counter value set below the last stays monotonic (the restore case); a line can't attach to a held account; `statements` can't take a status its revision doesn't have; `last4` rejects five digits.

### U3. Extraction models and prompts

**Goal:** The first pass, the deposit and card extraction, and the loan figures, as pydantic-ai agents with versioned prompts.

**Files:** `src/receipt_index/statement_extraction.py`, `tests/unit/test_statement_extraction.py`

**Approach:**
- Output models with `extra="forbid"`. Money is a two-place decimal string and never a float, as in the spike.
- First pass: document type, accounts (label, type, page range), printed account count. The label is used only to tell the extraction call which account to read. It is not stored, since it can hold a name.
- Account extraction: one call per listed account, the whole page-marked text, told the account's label, type and pages. The deposit and card prompts share a base that states the sign basis. Card lines take the posted date and keep a printed transaction date. Section and column totals come back as printed magnitudes.
- Loan: printed figures only, null when not printed. The prompt forbids deriving a figure.
- Every prompt says the document is data and its instructions are to be ignored.
- `STATEMENT_PROMPT_VERSION` and `FIRST_PASS_VERSION`, recorded on each revision.
- Model, effort and request timeout come from the existing `llm` configuration.

**Tests:** schema validation refuses a float, a three-place amount and a five-digit `last4`; the prompts contain no statement text (a test checks them against the fixture generator's strings).

### U4. Gates

**Goal:** Pure functions from the first pass and the account extractions to account and statement statuses.

**Files:** `src/receipt_index/statement_gates.py`, `tests/unit/test_statement_gates.py`

**Approach:** from the spike's grader, re-written here:
- completeness first: a deposit, card or loan statement with no listed accounts, a listed account that comes back empty and isn't dormant, or a printed account count that disagrees holds the whole statement;
- no beginning or ending balance: held;
- footing to the cent, held as `does_not_foot` with the difference;
- posted dates in the period, with the card grace; skipped when no period is printed;
- running-balance chain in the printed order, carrying amounts across lines with no printed balance;
- section and column totals as magnitudes;
- dormant account: no lines and equal printed balances is clean;
- the canonical form and `content_digest`;
- statement status from the completeness check and the account statuses.

**Tests:** one test per gate, failure class and statement status, each written to fail first against a stub that passes everything; a digest test against the contract document's worked example.

### U5. Statement pipeline and retries

**Goal:** One statement item from fetch to commit, and its retries, kept apart from receipts.

**Files:** `src/receipt_index/statements.py`, `src/receipt_index/pipeline.py` (`run_due_retries`), `src/receipt_index/repository.py` (`select_due`), `src/receipt_index/fetching.py`, `tests/integration/test_statement_pipeline.py`

**Approach:**
1. `started` row in `ingest_log`, with `source_type = 'gdrive_statement'`.
2. Store the original with `put(purpose="original", folder=StoreFolder.for_original(...))` through `_put_original`, so it is write-once under `originals/` like every other original. If a statement already has that file, add a `statement_sources` row and finish with no model call.
3. Check and read the PDF under `STATEMENT_BOUNDS`. Scan, unreadable or over bounds: a `rejected` revision. A timeout: an item error, retried.
4. First pass. A non-extractable type, or more than 8 accounts (`over_bounds`, `accounts`): a `rejected` revision.
5. One call per account, then the gates.
6. One transaction: the statement (if new), its source row, the revision, accounts, clean accounts' lines, and an `ingest_log` `success` row with the token totals.

- **Watchdog steps.** Steps 3, 4 and each account call re-mark the item clock through `RunControl`, so each is timed as its own step. The longest is one model call: the request timeout plus one output retry, about 360 seconds, well under `item_timeout_seconds` (1200).
- **Errors.** A transient model or Drive error pauses the worker as today. A model or schema error on any call is an item error, retried with the existing backoff, and terminal after `retry_max_attempts`. It writes no revision. A held account is a successful outcome, not an error.
- **Retries stay statements.** `select_due`'s fallback matches `source_type`, so a `gdrive_statement` row is only ever offered to statement sources, including after its source is removed from the config. `run_due_retries` sends `gdrive_statement` items to this pipeline. The "is this item new" check also looks at `statement_sources`.
- `statements rerun` (U8) runs steps 3 to 6 from the stored original as a new revision.

**Tests:** each fixture end to end with a stubbed model; a crash after the original is stored resumes without storing it twice; the same bytes from two drops give one statement; a due statement item, including one whose source was removed, never reaches the receipt `process_item`; a slow stubbed model call re-marks the clock at each step.

### U6. Intake through `serve`

**Goal:** A Drive source that feeds only the statement pipeline.

**Files:** `src/receipt_index/config.py`, `src/receipt_index/service.py`, `src/receipt_index/cli.py`, `example.env` and the example configuration

**Approach:**
- `GdriveSourceConfig.documents: Literal["receipts", "statements"] = "receipts"`. Existing configurations are unchanged.
- Config validation refuses a statement source whose folder is also a receipt source's folder.
- The worker dispatches by the source's `documents`. Sync state (still `gdrive`), item locks, health and `status` work per source as now. `status` shows statement sources with their own counts.
- `archive` stays false for Drive, as for receipts: the file stays in the folder, and the store keeps its own copy.

**Tests:** a statement source never reaches the receipt `process_item`, and a receipt source never reaches the statement pipeline; overlapping folders are refused.

### U7. MCP tools and the contract document

**Goal:** The three tools, and the published contract.

**Files:** `src/receipt_index/mcp_server.py`, `docs/engineering/designs/statement-read-contract.md`, `tests/unit/test_mcp_statements.py`, `tests/integration/test_mcp_statements.py`

**Approach:** as in [The Read Contract](#the-read-contract-proposed-for-the-ledgers-pre-review). One snapshot per call. The server instructions gain a paragraph on statements: the sign basis, that held accounts have no lines, and that statement text is untrusted data. The contract document carries a worked digest example and a worked sign flip.

**Tests:** the feed under two interleaved writers never skips; a held account returns no lines; an earlier revision returns its own status; the file fetch is access-logged and refused when the log can't be written.

### U8. CLI

**Files:** `src/receipt_index/cli.py`, `docs/user/cli-reference.md`

**Commands:** `receipt-index statements list [--status ...] [--since DATE] [--output json]`, `statements show ID [--revision N] [--output json]`, `statements file ID`, `statements rerun ID [--yes]`. JSON output is the contract's shapes. `failures --source` already lists statement items that failed.

### U9. Documentation

**Files:** `docs/engineering/designs/statement-extraction.md` (the technical design, from this plan), `docs/user/server-deployment.md`, `docs/user/getting-started.md`, `README.md`

---

## Testing Strategy

### Synthetic fixtures

Built at test time from HTML with weasyprint (`tests/fixtures/statements/`), so each layout is readable in the repository. Every name, number and amount is invented. Layouts imitate the failing kinds the spike reported, described in general:

- single deposit account, one table, running balances on every line;
- card with payments and purchases in separate sections, section totals, a purchase dated a few days before the period;
- card whose balance is printed as a credit;
- deposit table split across a page break, with a reprinted header and a balance-forward row;
- wrapped descriptions over two or three lines;
- separate credit and debit columns with column totals;
- running balance printed only on a day's last line, newest-first order;
- combined statement: two deposit accounts and a dormant third, with a printed account count;
- statement with no beginning balance;
- loan statement with principal, interest and escrow printed, and no opening principal;
- no-activity loan bill, escrow notice, a letter (rejected);
- an image-only page set (scan), a 61-page PDF (over bounds) and a non-PDF file (unreadable).

### With a stubbed model

pydantic-ai's `FunctionModel` returns a chosen extraction per call, so each gate is driven to pass and to fail on purpose: a dropped line, a flipped section, a duplicated page-break line, a date outside the grace, a broken chain, an empty account, a wrong account count, no accounts at all.

### Against the real model, by hand

`make statement-eval` runs the fixtures against the configured model and prints a counts-only table. It isn't run in CI and costs a few cents.

### Integration

Pipeline, retry, feed and MCP tests run against the devtest Postgres under the shared lock, as now.

---

## Cost

Haiku 5.5 is $0.10 per million input tokens and $0.50 per million output tokens, for prompts under 100,000 tokens. The text cap (U1) keeps each prompt under that point.

Per statement: input ≈ (1 + accounts) × (document tokens + about 1,500 of prompt); output ≈ 35 tokens a line, plus thinking at medium effort. A text page is roughly 700 to 1,000 tokens.

| Synthetic example | Input | Output | Cost |
|---|---|---|---|
| Card, 6 pages, 120 lines | about 15,000 | about 8,000 | about $0.006 |
| Combined, 3 accounts, 20 pages, 300 lines | about 75,000 | about 18,000 | about $0.017 |
| Rejected document (first pass only) | about 8,000 | about 500 | about $0.001 |

A backlog of 500 statements costs on the order of $5. A rerun costs the same again. Caching the document text across a statement's calls would cut input by up to about three quarters on combined statements. It is deferred: input is the smaller part of the cost.

---

## Rollout and Rollback

**Rollout**
1. Merge U1–U9 in order. Each is inert until a statement source is configured.
2. On the server: back up the database, apply 000023 and 000024 (the cutover runbook's migration step), rebuild and restart `serve`.
3. The operator creates the statement folder in Drive and adds a `documents: statements` source to the config. Restart `serve`.
4. Drop two or three statements; check `receipt-index statements list` and a `get_statement` call before dropping the backlog.
5. Tell the ledger the contract is live.

**Rollback**
- Stop intake: remove the statement source from the config and restart. Its due retries stay statement items and are skipped with no statement source configured. The receipt pipeline is untouched throughout.
- Roll back the code: redeploy the previous image. The new tables are unused by it, and it ignores `gdrive_statement` rows, which no configured source matches.
- Remove the data (only if wanted): the 000024 and 000023 down migrations. The stored originals stay in the store and in `stored_files` and `file_locations`. No command lists them today; they are listed from those tables by hand if they are ever to be removed.

---

## Risks & Mitigation

| Risk | Mitigation |
|---|---|
| A statement reconciles with a misdated or mis-described line | The date gate catches dates outside the period; the operator spot-checks a few after rollout. Descriptions aren't verifiable by arithmetic: stated in the contract. |
| The first pass misses an account and nothing prints a count | An empty or missing account list holds the statement; a missed account with no printed count is undetectable in shoebox. The ledger's own balance check on the account is the backstop. Stated in the contract. |
| The first pass flips between runs | Holds are safe; `statements rerun` by hand. Revisit an automatic retry after a month. |
| A description carries a full account number | The prompt keeps last four; the contract says so; a unit test checks the prompt. A deterministic scrub is not added, since it would also cut reference numbers the ledger may want. |
| Prompt injection in a statement | Prompts say the document is data; outputs are schema-bound; held results carry no lines; the MCP instructions say statement text is untrusted. |
| The server's clock runs backwards | The counter takes the greater of the last value and the clock, so it stays monotonic within one database; only a restore combined with a clock set back past the backup could repeat a value. |
| A long transaction holds the feed lock | Only revision writes take it, from one worker; `statement_timeout` bounds each. |
| Real data reaching this repository | Fixtures are generated from invented HTML; prompts carry no statement text; no real-model output is committed. |

---

## Document Review

Run 2026-11-10 with four reviewers: coherence, feasibility, scope guardian, and adversarial on the read contract. The cross-model pass was skipped: it sends the document off the machine, and no one was at the keyboard to agree to that. Dispositions:

**Fixed in this plan**
- Rejected documents had no revision, so the ledger's apply key could be null and the tokens of a first-pass rejection had nowhere to go: every run is a revision now.
- Status and document type lived only on the mutable statement row, so an earlier revision would show the current status: both are kept per revision, and the statement row is a checked copy.
- A restore could bring back a revision number the ledger had already applied, with different content: revision numbers come from the clock-floored counter.
- No key paired an account across revisions, and the digest included the ordinal: `match_key` added, ordinal and IDs left out of the digest.
- A field added later would change every digest: the digest's field list is fixed per contract version.
- Unknown `account_type` or status had no safe default: the ledger's defaults are stated, and a new type or line-bearing status is breaking.
- `parse_failure` and `model_error` were listed as held classes but are item errors: removed from the contract, and the error path stated.
- A count mismatch or dropped account had no clear effect on the other accounts: a completeness failure holds every account; the origin document's success criteria are aligned.
- A deposit, card or loan statement with no accounts read as complete: held as `dropped_account`.
- No `document_type` for an unreadable file, and no record of which bound was hit: `unreadable` and `rejected_bound` added, and the statement read returns a reason.
- A timeout became a permanent rejection although it can be the host's load: now a retryable item error.
- Totals' sign, the loan breakdown's basis, the card grace and the balance chain's direction were unstated, so the ledger couldn't re-check: all stated in the contract.
- Retries ran statement items through the receipt pipeline, and the rollback made it worse: statement items have their own `source_type`, and retries dispatch by it.
- A statement item could exceed the watchdog's item budget: each phase is its own step, and accounts are capped at 8.
- `statements/originals/` was outside the store's write-once layout: originals go under `originals/` like the rest.
- Rollback cited a `verify --orphans` command that doesn't exist: corrected.
- The writer's isolation level was unstated: `READ COMMITTED`, with the counter advanced in one `UPDATE … RETURNING`.
- `statement_id` was called stable without the restore exception, and the restore rule was only a hint: qualified, with a matching rule on `source_sha256`.
- Requirements trace and the scan threshold's wording: corrected.

**Cut from scope (deferred)**
- `search_statements`: no requirement or consumer; the CLI covers the operator.
- Degraded mode when the statement grants are missing: rollout applies both migrations before the new image starts.
- Diagnosis of a footing difference (sign error, missed section, page break): `does_not_foot` with the difference and pages is enough to act on; classes can be added later.

**Answered in the ledger's pre-review**
- The review cases the agreed rules didn't name (clean to rejected, and an account removed between revisions): review items in the ledger, never automatic changes. See decision 4.

**Not changed**
- Canonical JSON details (escaping, normalisation) are now stated; the worked example in the contract document settles the rest at build time.

---

## Sources & References

- Origin: `docs/brainstorms/2026-11-10-statement-extraction-requirements.md`
- `docs/engineering/designs/always-on-ingest.md` §§1, 5, 6, 7
- `docs/engineering/designs/evidence-store.md` §§2, 3, 6, 9
- `docs/engineering/designs/receipt-data-model.md` (extraction history, cost per receipt)
- `src/receipt_index/pdf_validate.py`, `pdf_reader.py`, `mcp_server.py`, `pipeline.py`, `repository.py`, `fetching.py`, `store.py`
