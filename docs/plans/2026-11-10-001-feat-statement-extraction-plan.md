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

The ledger's session pre-reviewed the read contract and found it good to build, and the operator gave the go on 2026-11-10 (see Open Questions). An app review on 2026-11-11 found a status contradiction, contract gaps and build traps; this revision answers them, with the ledger's answers recorded as decision 5 (see Document Review). The build starts when the lead says so.

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
- **One counter for the feed and for revisions, never going backwards.** Every transaction that writes a revision, or republishes a statement, takes the feed lock (a row lock on the counter's one row, held until commit; built in U2 in place of the advisory lock first planned, so a read-only login can't take it and stall the writers), then sets the next value to the greater of the last value plus one and the current time in microseconds since the epoch, in one `UPDATE … RETURNING` on a one-row state table. A new revision takes one value as its `revision`; the statement then takes another as its `change_seq`, its place in the feed. The two are separate so a republish after a restore can move a statement in the feed without inventing a revision. Triggers draw both values, so no code path can skip the counter, and statement writes run at `READ COMMITTED`.
  - Writers commit in counter order, and Postgres makes a commit visible before it releases the lock, so a reader that has seen *n* has seen every change below it.
  - A timestamp alone can skip a row whose transaction commits late. A transaction-ID cursor doesn't survive a dump and restore. A bare sequence goes backwards when an older backup is restored, and so would a per-statement revision count. Then the ledger's stored cursor would skip new changes, or a revision number it already applied would come back with different content. The clock floor keeps every new value above anything handed out before a restore, as long as the server's clock doesn't run backwards.
  - A restore can still leave a statement's restored `change_seq` below a cursor the ledger already holds. `statements republish` (U8), the restore runbook's last step, gives every statement a fresh `change_seq`, so nothing restored stays hidden behind the cursor.
  - Writes are a handful a day from one worker, so the lock costs nothing. The receipts feed uses the same convention with its own state row; it doesn't share this one.
- **Statement items are their own source type in `ingest_log`.** Rows carry `source_type = 'gdrive_statement'`, so the retry scheduler's fallback to "any source of the same type" never hands a statement to a receipt source, and `run_due_retries` dispatches by type. `sync_state` keeps `'gdrive'` for a statement source: it is keyed by source name, and its type only says which cursor columns apply. `ingest_log.source_type` has no check constraint, so no migration is needed for the new value. U5 lists every place the retry and requeue code assumes a receipt.
- **Each phase is its own watchdog step.** The check, the read, the first pass and each account call re-mark the item clock through a new `RunControl.step(source_id)` method (U5), and a statement lists at most 8 accounts. Each step then stays under `item_timeout_seconds` by the same sum-of-deadlines rule as receipts (U5).
- **Pages kept.** The statement read returns per-page text. The prompt marks pages, so the first pass can give page ranges and each line its page.
- **Amount sign: the statement's own balance.** Decided in the origin document, agreed with the ledger with conditions (below).
- **Held, never partial, with a reason at every level.** A held statement always carries a `hold_reason`, and an account keeps its own failure class even when the statement holds it, marked `held_by_statement`. A loan account counts as usable (`figures_only`) for the statement's status.
- **No new dependency.** pdfplumber, pydantic-ai and weasyprint (for test fixtures) are already in the project.

---

## The Read Contract (pre-reviewed by the ledger; revised after the app review)

Published at build time as `docs/engineering/designs/statement-read-contract.md`, the single source for its shapes. Summary:

### Versioning

- Every response carries `contract_version` (integer, starts at 1).
- **Additive, not breaking:** a new field, a new document type for a rejected document, a new failure class or hold reason. The contract document notes each. The ledger ignores fields it doesn't know and treats an unknown failure class as held. A new field stays out of `content_digest` until a version change.
- **The ledger's safe defaults:** it imports lines only from an account whose `status` is `clean`, whose `account_type` is `deposit`, `card` or `loan`, and whose `currency` is `USD`. It treats any other status, type or currency, a null currency included, as held. (The ledger is USD-only today.)
- **Breaking:** removing or renaming a field, changing a field's meaning, sign or format, a new `account_type`, a new account `status` that carries lines, or a change to the digest's field list. A breaking change ships as new tools beside the old ones (for example `get_statement_v2`), and the old ones stay until the ledger has moved.

### Formats

- **Money:** a JSON string matching `^-?(0|[1-9][0-9]*)\.[0-9]{2}$`. Two places always, no `+`, no thousands separator, no currency sign. Zero is `"0.00"`, never `"-0.00"`.
- **Currency:** an ISO 4217 code per account (below). Every money field on an account, its lines included, is in that account's currency.
- **Dates:** `YYYY-MM-DD`, the calendar date as printed, no time zone.
- **Timestamps** (`extracted_at`): RFC 3339 in UTC with a `Z` suffix and microseconds, for example `2026-11-10T18:37:50.123456Z`.
- **Integers** (`revision`, `ordinal`, `page`, counts): JSON numbers. `revision` is a 64-bit integer. Its values are microseconds since the epoch, about 1.8 × 10^15 today: exact as a JSON number (below 2^53) until about the year 2255, but a consumer stores it as a 64-bit integer, never a 32-bit one or a float.
- **Text:** UTF-8, as extracted (no Unicode normalisation), except that the three masked fields below have their separators folded (see [Masking](#masking)).
- **Normalised text:** `institution` is trimmed, with inner whitespace collapsed to one space, and case left alone. `section`, on lines and in totals alike, goes through one function: trimmed, inner whitespace collapsed, a trailing colon dropped, lower case. The same printed heading always gives the same `section` on a line and in a total.
- **Masked text:** `description`, `section` (on lines and in totals) and `institution` all go through the one mask in [Masking](#masking), after normalisation and before the gates and the digest.

### Tools (read-only, on the existing MCP server)

| Tool | Returns |
|---|---|
| `list_statement_changes(cursor?, limit?)` | Statements created or changed after `cursor`, oldest change first, each as `{statement_id, revision, status, document_type, source_sha256}`, plus `next_cursor`. No cursor means from the start. `limit` defaults to 100, at most 200 (the server's existing page maximum). |
| `get_statement(statement_id, revision?)` | One statement revision, with its accounts but not their lines. The current revision unless one is named. |
| `get_statement_lines(statement_id, revision, account_ordinal, limit?, offset?)` | One account's lines, in `ordinal` order, in the server's existing page envelope (`total`, `limit`, `offset`, `next_offset`). `revision` is required, so every page comes from one revision. `limit` defaults to 500, at most 1,000: a new constant, `MAX_LINE_PAGE_SIZE`, separate from the server's `MAX_PAGE_SIZE` (200), since lines are small and an account can have hundreds. A held or loan account returns an empty page. |
| `get_statement_file(statement_id, include_content?)` | The original PDF, access-logged like `get_receipt_file`, with the same content limit (10 MiB by default): over it, the call returns the metadata with `content_included: false` and `content_omitted_reason: "too_large"` rather than the bytes, as `get_receipt_file` does. |

**Errors.** Every tool refuses bad input with a tool error whose message starts with a stable code, so a client can branch on it:
- `invalid_cursor`: a cursor that doesn't parse, or whose prefix isn't one this server issues (anything but `s1:` today).
- `invalid_id`: a statement ID that isn't a UUID.
- `not_found`: no such statement, no such revision of it, or no such account ordinal in that revision.
- `invalid_argument`: `limit` or `offset` out of range.

A well-formed cursor ahead of everything the server has issued (possible after a restore) isn't an error: it returns no rows, and `next_cursor` is the request's cursor.

### Change feed

- The cursor is opaque to the ledger (today `"s1:<change_seq>"`). It survives restarts and redeploys, and never goes backwards.
- `change_seq` is the statement's place in the feed. It takes a new value from the counter (below) whenever the statement gets a new current revision, and when the operator republishes it (below). It is separate from `revision`, which numbers content.
- A statement appears once per change. A later change moves it, so the feed always shows its latest. Skipped revisions can still be fetched with `get_statement(id, revision)`.
- Rejected documents are in the feed too, as revisions with no accounts, so a dropped file never vanishes silently. Model and schema errors are not: they make no revision (see Failure classes).
- Every run is a new revision and a feed entry, even when its content is identical to the last one. The account digests then show that nothing changed.
- `revision` numbers are increasing for a statement and never reused, even across a restore.
- At-least-once delivery: the ledger applies by `(statement_id, revision)`, so a repeat is harmless.
- Rows carry no cursor of their own. `next_cursor` is the only one: the position after the page's last row, or the request's cursor when no rows follow.
- **After a restore from an older backup:** changes made after the backup are lost from shoebox. The Drive files are still in the folder (Drive sources don't archive), and the restored sync state lists them again. Statements first read after the backup are re-read and get new `statement_id`s, and statements re-run after the backup go back to their earlier revision. That earlier revision keeps its old `change_seq`, which can be below the ledger's cursor, so on its own it would never reach the ledger. The restore runbook therefore ends with `receipt-index statements republish --all --yes`, which gives every statement a new `change_seq` without a new revision, so the whole set passes through the feed once more. The ledger's rules then apply: a new `statement_id` whose `source_sha256` matches a statement it knows is that statement; a revision lower than one it applied is never treated as newer; account digests are compared with what it imported, and a difference goes to review.

### Per statement (one revision)

| Field | Meaning |
|---|---|
| `statement_id` | UUID, stable for the original's bytes within one database history (see the restore rule) |
| `source_sha256` | Hex SHA-256 of the original PDF, lower case |
| `revision`, `extracted_at` | This revision's number and when it ran |
| `is_current`, `current_revision` | Whether this is the statement's current revision, and the current revision's number |
| `document_type` | `deposit`, `card`, `loan_statement`; or, for a rejected document, `loan_bill`, `tax_or_escrow_notice`, `not_statement`, `scan`, `unreadable` (not a PDF, one that needs a password to open, or one pdfplumber can't open), `over_bounds` |
| `rejected_bound` | For `over_bounds`: `pages`, `size`, `text`, `memory`, `crash` (the PDF reader died of a signal, such as a parser segfault on a malformed file) or `accounts` (more than 8 accounts, or a first pass whose account list stopped at its output ceiling); else null |
| `status` | `complete`, `incomplete`, `held` or `rejected` (below) |
| `hold_reason` | Why a `held` statement is held (below); null for every other status |
| `period_start`, `period_end` | The document's own printed period; null when not printed. The date gate uses each account's period (below) |
| `printed_account_count` | Accounts the document's own summary lists; null when it prints none |
| `pages` | Page count |
| `extractor` | `{model, prompt_version, first_pass_version}`; null fields for a rejection before any model call |
| `masked_runs` | How many cuts the mask made in the text this revision stores (cuts, not distinct numbers: a number grouped by mixed separators can take two or three cuts, so the count is an upper bound on the numbers found): every account's `institution` and section-total and column-total headings, and the `description` and `section` of each stored line. A held account's lines are never stored, so their text isn't counted. 0 for a rejection. Not in any digest |
| `accounts` | Below, in document order |

**Statement status.** Exactly one applies, tested in this order:
- `rejected`: not extracted; `document_type` says why. No accounts.
- `held`, with a `hold_reason`:
  - `no_accounts`: a deposit, card or loan statement whose first pass lists no account;
  - `dropped_account`: a listed account came back empty (its own class is `empty_account`, below), or its `last_page` lies past the document's pages, in which case it isn't called and its class is `empty_account` too. An empty loan counts too, as a completeness failure under the operator's answer 3: the first pass listed a loan, and a no-activity loan bill is already rejected by document type, so a listed loan with no printed figure means the extraction lost an account, not that the loan had nothing to show;
  - `account_count_mismatch`: the printed account count disagrees with the accounts listed;
  - `no_usable_account`: none of the above, but no account is `clean` or `figures_only`.

  For the first three the account list itself can't be trusted, so every account is held and marked `held_by_statement`, whatever its own gates said.
- `incomplete`: no hold reason, at least one account held by its own gates, and at least one `clean` or `figures_only`. Clean accounts pass their lines.
- `complete`: no hold reason, and every account is `clean` or `figures_only`. A loan-only statement whose loan figures came back is `complete`.

### Per account

| Field | Meaning |
|---|---|
| `account_id` | UUID, stable within a revision only |
| `ordinal` | 1-based position in the document |
| `match_key` | `{account_type, last4, occurrence}`, where `occurrence` counts accounts with the same type and `last4` in document order. Pairs this account with itself in another revision of the same statement. An account in one revision and not in the other is added or removed, for the ledger's review. `institution` is left out, since a re-extraction can give an institution's name in a short or a long form. Accounts of one type with a null `last4` pair by document order alone, so if a re-extraction drops one, the later ones pair with the wrong account; the digest comparison shows that as changes, for review |
| `institution` | As printed, normalised as above |
| `last4` | Last four digits only, never a full number; null when none is printed |
| `account_type` | `deposit`, `card`, `loan` |
| `currency` | ISO 4217 code. Read from the statement: a printed code, or a currency sign the statement uses. In this version a bare `$` with no other currency named reads as `USD`, since a null there would hold nearly every statement the ledger expects; a non-USD statement that prints only `$` is therefore labelled `USD` (see Risks). Null when the statement gives no currency at all |
| `period_start`, `period_end` | The account's period: as printed for that account, else the document's. Null when neither is printed |
| `status` | `clean`, `held`, or `figures_only` (loans) |
| `held_by_statement` | True when the statement's hold reason holds this account; then `status` is `held` even if its own gates passed |
| `beginning_balance`, `ending_balance` | As printed, on the statement-balance basis; null when not printed |
| `section_totals` | `[{section, amount}]`: each printed section total, as an unsigned magnitude; `section` is never null here |
| `column_totals` | `[{section, credits, debits}]`: a table's printed column totals, as unsigned magnitudes; `section` null for a table covering the whole account. Credits are the lines that raise a deposit balance or lower a card or loan balance |
| `line_order` | `oldest_first` or `newest_first`, as printed |
| `line_count` | Lines this account carries (0 for held and loan accounts) |
| `date_grace_days` | Days before `period_start` a posted date may fall and still pass: 7 for a card, 0 otherwise |
| `failure_class`, `difference`, `first_page`, `last_page` | For an account its own gates held: why, the ending balance less (beginning plus lines) where both are printed, and where to look. `failure_class` is null for a clean or figures-only account, and for one held only by the statement |
| `loan` | For a loan: `principal_before` and `principal_after` on the statement-balance basis; `principal_paid`, `interest`, `escrow`, `fees` and `advances` as unsigned printed amounts. Each null when not printed, and null when printed as a credit (a refund or reversal): version 1 has no signed breakdown, and a magnitude would read as a charge |
| `content_digest` | Hex SHA-256 of the account's canonical form (below), lower case |

**Held accounts carry no lines, by design.** The contract gives a held account's balances, totals, difference and pages, so the owner can act on it, but never its lines: a held extraction is not a fact to import, and it can't be re-footed from the response. The ledger shouldn't read the missing lines as a fault.

**Canonical form for `content_digest` (contract version 1).** A JSON object of exactly these fields: `account_type`, `institution`, `last4`, `currency`, `period_start`, `period_end`, `status`, `held_by_statement`, `failure_class`, `difference`, `beginning_balance`, `ending_balance`, `section_totals`, `column_totals`, `line_order`, `loan`, and `lines`, each line with exactly `posted_date`, `transaction_date`, `description`, `amount`, `running_balance`, `section` and `page`, in `ordinal` order. Keys sorted, no insignificant whitespace, UTF-8 with no `\u` escapes beyond JSON's required ones, text as the contract gives it, money and dates as the strings the contract gives, integers as JSON numbers, booleans as `true`/`false`, nulls kept. Ordinal and IDs are left out, so renumbering alone doesn't change a digest; the account's period is in, since the date gate depends on it. The contract document gives a worked example, so the ledger can recompute it from `get_statement` and `get_statement_lines`. Four properties to state in the contract document: the totals are digested in printed order, so the same totals printed in another order give another digest; a held account's lines aren't stored or digested, so two held accounts with the same figures share a digest; a capped (`too_many_lines`) account has null `last4`, `institution` and `currency`, so its `match_key` shifts and can pair with another account whose `last4` is null; and the digest covers masked text, so it follows the running Python's Unicode data, which decides which characters the mask joins.

**Failure classes**, for an account its own gates held, in the order they are tested (an account carries the first that applies):
- `empty_account`: a deposit or card account with no lines that isn't dormant (beginning and ending balances printed and equal), or a loan account with no printed figure at all;
- `no_beginning_balance`, `no_ending_balance`;
- `too_many_lines`: the account's extraction hit the output cap (see U3) and was cut off;
- `undated`: printed dates carry no year, and no period is printed to place them, or a yearless date fits more than one year of a period a year or longer;
- `does_not_foot`, with the difference;
- `date_out_of_period`;
- `balance_chain_break`;
- `totals_mismatch`.

A model or schema error makes no revision: the item is retried, and after `retry_max_attempts` it is a terminal failure in `ingest_log`, shown by `failures`, not in the feed.

### Per line (clean accounts only)

| Field | Meaning |
|---|---|
| `line_id` | UUID, stable within a revision only |
| `ordinal` | 1-based position in the printed order within the account, across pages. For a `newest_first` account, ordinal 1 is the newest line |
| `posted_date` | The date the line posted. When a line prints one date, it goes here, whatever the column is called |
| `transaction_date` | A second, transaction date where printed, else null. Not gated |
| `description` | Text as extracted, wrapped lines joined with one space, then masked (see [Masking](#masking)). The digest covers the masked text. The model is also told to cut account and card numbers, but the mask is the guarantee |
| `amount` | Signed by its effect on the statement's printed balance |
| `running_balance` | As printed on the line, else null |
| `section` | The printed heading it sits under, normalised as above; null when it sits under none |
| `page` | 1-based page |

**Years.** A statement often prints dates without a year. The model returns each date as printed parts (month, day, and the year only when printed), and shoebox assigns the year after extraction, from the account's period: the year that puts the date inside the period, or for a card inside the grace window before it. Every year from the earliest allowed date's to `period_end`'s is tried; when more than one fits (a period of a year or more), the year can't be told and the account is held as `undated`. A December to January period is the usual case where the years matter. A date that no year places that way takes `period_end`'s year, and then fails the date gate, holding the account as `date_out_of_period`. An account with a yearless date and no period is held as `undated`; it carries no lines, so no line needs a guessed year.

<a id="masking"></a>**Masking.** One deterministic function masks `description`, `section` and `institution`, after normalisation and before the gates and the digest. It first applies the statement text's own normalisation to the field (built in U3), since the model's output can carry characters the read already removed from the page text: control, bidirectional, zero-width and other invisible characters are dropped, so none can sit between two digit groups and hide a number. It then folds separators in the whole field: every Unicode whitespace character (a non-breaking space, a tab, a thin space, a line break, and the rest of Python's `str.isspace` set) and every run of them becomes one ASCII space, and every Unicode dash (category `Pd`, and the minus sign U+2212) becomes an ASCII `-`. Nothing else in the text changes. It then replaces every match of this pattern, over ASCII digits only (no Unicode digit classes), with the last four digits of the match:

```
[0-9](?:[ -]*[0-9]){7,}
```

That is, 8 or more digits in a row, where any number of spaces and dashes between two digits count as part of the run. So does any character that shows as nothing or as a mark on its neighbour: a format character (`Cf`), a combining mark (`Mn`, `Me`), a private-use character (`Co`) or an unassigned code point (`Cn`), which normalisation doesn't always drop (an Arabic number sign, a Mongolian variation selector, a combining accent). So do lone surrogates (`Cs`; pydantic refuses them in model output anyway) and the letters and symbols that render as blank space (the Hangul and halfwidth fillers, the blank Braille pattern, U+303F, U+1D159, U+1DA7F, U+1DA80, and the Egyptian hieroglyph full and half blanks U+13441 and U+13442, letters from Unicode 15.0). The categories come from the running Python's Unicode database, so the joined set follows it: Python 3.11, production's image, joins every code point unassigned in Unicode 14, including some that later versions assign as visible letters. That only makes the mask cut more; the blank-name test checks each version's names. A test walks every code point in those six categories, and another lists every letter or symbol whose name says it is blank as either joined or visible. A letter of any script breaks a run. A symbol or punctuation mark between digit groups is handled by the second cut below. The pattern above is written with that wider separator class in the code. Runs of several separators join, since the mask's point is to miss nothing. Because separators are folded first, a number grouped by non-breaking spaces, tabs or Unicode dashes is caught, and so is one split across a wrapped line. A second cut, run with the first until neither changes anything, takes every other separator: digit groups joined by any punctuation mark or symbol (after the first group, two digits or more, and a last group of one allowed, so a number in pairs, `41.11.11.11.11.11.11.12`, or with a short tail, `4111.1111.1111.12`, is cut whole) (`.`, `,`, `/`, `_`, `'`, `:`, `#`, `+`, `|`, `~`, `(`, `"`, a middle dot, a bullet, a fraction slash and the rest), with at most a space either side, that total 8 or more digits are cut to their last four. Only an amount that can occur is left alone: a run in thousands groups (`123,456,789`, `1.234.567`, `1'234'567`) of at most nine digits, or at most twelve when a decimal part follows with the other separator (`123,456,789,012.50`). Thirteen or more digits are cut whatever their grouping, so `4,111,111,111,111,234` is cut, and a bare twelve-digit `123,456,789,012` is cut too (a policy: an amount on a statement has its cents). Plain digits with a decimal part (`123456.78`) are an amount too. Times (`12:30`, `12:30:45`) are too short to cut. An earlier version of this section said a number split by a visible separator kept "a fragment of up to seven digits": that was wrong, since a number grouped by dots, commas or slashes passed whole, and the second cut closes it. A third cut joins digit groups separated only by spaces when none of them has a decimal part: `411,111 111,111 1234` is cut, while amounts side by side (`5,678.90 1,234.56`, `CHECK 1234 5,678.90`) stay. Plain digits with a decimal part (`123456.78`, `1234567.89`) are amounts, but eight digits in a row are cut by the first cut, as any are. What the mask is: a pattern guard for the ways institutions print account and card numbers (in a row, or grouped by spaces, dashes, punctuation or symbols, in any combination). It is not a proof that no number can pass. Known limits, recorded rather than fixed: a number written one digit per group (`1,2,3,4,…`); spaced digits mixed with marks (`4 1 1 1.1 1 1 1.1 2 3 4`); and a number padded to look like a twelve-digit amount with cents (`004,111,111,111.11`) pass. Accepted costs: a bare count over nine digits in thousands groups is cut; an eight-digit date with any separator is cut; and a phone number loses all but its last four digits, so `(555) 123-4567` becomes `(4567`.

The mask cuts any long digit run, not only account numbers. Known side effects, all accepted for version 1:
- a reference or confirmation number of 8 or more digits keeps only its last four;
- a phone number printed with dashes or spaces (ten digits) keeps its last four;
- a date printed with no separators (`20261109`) becomes `1109`;
- an eight-digit date is one run, whatever its separator: `2026-11-09` becomes `1109`, and `11-09-2026`, `11/09/2026` and `11.09.2026` become `2026` (the line's `posted_date` carries the date apart from its text);
- any adjacent digit groups separated only by spaces and dashes join into one run and are cut together when they total 8 or more digits: a store number then a terminal number, or a date beside a store number (`2026-11-09 4412` becomes `4412`).

Digits outside ASCII (full-width ones, for example) are not matched; see Risks.

Decisions, one line each:
- **Separators:** only Unicode whitespace (the full-width space included) and dashes join digit groups; any other character ends a run.
- **Full-width forms:** full-width digits are not masked (ASCII only, above), and a section heading's full-width or small colon is dropped like an ASCII one.
- **Loan breakdown signs:** a breakdown figure printed as a credit is null, and one printed as a charge (`DR` or `+`) is the amount. The cost: under negative amortisation, a `principal_paid` printed negative is nulled; `principal_before` and `principal_after`, which are signed, still show the growth.
- **Decimal places:** money is exactly two decimal places. A statement in a currency printed with none or three fails its calls' schema, an item error that becomes terminal after `retry_max_attempts`; version 1 reads two-place currencies only.

Why accepted: no arithmetic check reads these fields; the mask is deterministic, so the same input always gives the same text and the ledger's fingerprint stays stable across re-extractions; and the original PDF is one `get_statement_file` call away when a full reference is needed. A narrower rule (only runs next to words like "account" or "card") would miss numbers printed without such a word. `last4` and `match_key` are unaffected: `last4` is extracted as four digits on its own.

The mask covers what the contract's JSON carries. It doesn't touch the original: `get_statement_file` returns the PDF as stored, with every number in it, which is why that tool is access-logged. Each revision stores how many runs were masked (`masked_runs`), shown by `statements show`; the masked text is never logged.

### Sign basis

Balances, lines and running balances on an account, and a loan's principal before and after, are on one basis: the statement's own printed balance. A line is positive when it raises that balance and negative when it lowers it. For a deposit account the balance is money held. For a card or loan it is money owed, so a purchase is positive and a payment negative. A card or loan balance printed as a credit (a `CR` suffix or a minus sign) comes through negative on the same basis and is never normalised. Section and column totals, and a loan's payment breakdown, are unsigned printed magnitudes. A total printed as a credit (a leading minus, parentheses, or a trailing minus or `CR`; a card's payments total, often) or as a charge (a leading `+` or a trailing `DR`) is taken as its magnitude. A breakdown figure printed as a credit is null (see `loan`); one printed as a charge is the amount itself. Every Unicode dash (category `Pd`, and the minus sign) reads as a minus and full-width or small parentheses as parentheses, folded as the mask folds them. Any other form, such as a thousands separator, still fails the call's schema.

To reach the ledger's debit-positive sign (like OFX `TRNAMT`), the ledger multiplies the signed figures of an account by +1 for `deposit` and −1 for `card` and `loan`. It doesn't flip unsigned magnitudes.

### What the ledger can re-check from one revision

- Beginning plus the sum of the lines equals ending.
- The running-balance chain, walked in date order: in ascending `ordinal` for `oldest_first`, in descending `ordinal` for `newest_first`. Starting from the beginning balance, each printed running balance equals the previous printed balance (or the beginning balance) plus the amounts of the lines since it, lines with no printed balance included.
- Each section total equals the magnitude of the sum of the lines with that `section`. Each column total equals the sum of the magnitudes of the lines on that side, within its section or the whole account. Totals are magnitudes, so they don't check direction; the footing check above does.
- Every posted date lies between the account's `period_start − date_grace_days` and `period_end`. When the account has no period, there is nothing to check dates against, and shoebox skips the check.
- `content_digest`, recomputed from the fields.

Shoebox runs the same checks, so the ledger doesn't have to trust its status.

### Contract decisions agreed with the ledger

Asked of the ledger's session, one question at a time, on 2026-11-10:

1. **Sign.** No blocker with the statement-balance sign, account type and section, the ledger converting. Conditions, all adopted above: balances on the same basis as the lines, so the whole account flips together; the flip stated in the contract by account type (deposit keeps its sign, card and loan negate); a card or loan balance printed as a credit comes through negative and is never normalised. The ledger's own balance check would catch a statement whose printed polarity breaks that assumption. (After the document review, totals and the loan breakdown are stated as unsigned magnitudes; see the review dispositions.)
2. **Revisions and duplicates.** Accepted as proposed: a statement ID per original's bytes, a new revision per re-extraction, account and line IDs stable only within a revision, and a second statement (no merge, no flag) for the same account and period in different bytes. Additions, adopted above: a monotonic `revision` number, and a `content_digest` per account covering its status and lines. The ledger's facts are immutable, so it never replaces imported lines: an unchanged digest is a no-op, held to clean imports, and changed lines or clean to held after an import go to the ledger's review. Shoebox only has to report revisions honestly. The ledger dedupes lines with its own fingerprint (posted date, amount, normalised description, position among identical lines that day, per account), which also catches overlap with a structured file of the same account.
3. **Change feed semantics.** Fits the ledger's poller as proposed: latest revision only (the ledger compares digests, so skipped revisions don't matter), at-least-once, an opaque cursor that the ledger advances only after applying a page. Rejected documents stay in the feed, so the owner sees them. Conditions, adopted above: the cursor survives restarts, redeploys and backup restores and never goes backwards; the contract states whether an identical re-extraction makes a revision (it does).

4. **Pre-review of this contract (on this plan's pull request, 2026-11-10).** Good to build, with no blocking changes. The document review's changes are accepted: revisions from the clock-floored counter, `match_key` with a digest over a fixed field list, unsigned totals and loan breakdown (the ledger flips only signed fields), and the restore rule. The ledger keys statements on `source_sha256` throughout and treats `statement_id` as shoebox's handle for fetching. After a restore it never treats a lower revision as newer: it compares digests. A statement that was clean and is now rejected, or an account present before and missing now, becomes a review item in the ledger if it imported anything, and never an automatic change. The ledger checks a loan's `principal_after`, negated, against its own balance.
5. **Answers to the app review (2026-11-11).** Asked of the ledger's session through the lead:
   - **Rejections in the feed:** they stay in the feed; model errors stay out. The pre-review's note saying otherwise was loose wording. Decision 3 and the two review cases in decision 4 stand as written.
   - **Currency:** an ISO 4217 `currency` per account, and every money field on that account is in it. The ledger is USD-only today and holds any account that isn't `USD`; that is in its safe defaults.
   - **`match_key`:** `institution` leaves the key, which becomes `{account_type, last4, occurrence}` within one statement, because a re-extraction can render an institution's name in a short or a long form. The printed `institution` field stays, and stays in `content_digest`, since the ledger maps accounts by it. It is normalised by trimming and collapsing inner whitespace, with case left alone.
6. **Review of the revised contract (2026-11-11).** Good to build, with one change: mask account numbers deterministically. After extraction and before the digest, any run of 8 or more digits, ignoring spaces and dashes inside the run, becomes its last four, tested against a stubbed model output that leaves a full number in. Adopted (contract, `description` and Masking). Following the app reviews, the same mask also covers `section` and `institution`, with separators folded first. The ledger stores `revision` and the cursor's sequence as 64-bit integers, and a republish costs it only the reads.

---

## Open Questions

### Answered by the operator (2026-11-10)

The operator gave the go on this plan, after the ledger's pre-review of the contract, and answered the four open questions:

1. **Totals as a gate:** kept. A printed section or column total that disagrees with the lines holds the account (`totals_mismatch`).
2. **Card grace:** 7 days before the period start, as a statement-level constant, not configuration.
3. **Clean accounts of an incomplete statement** pass their lines, and a completeness failure holds every account.
4. **Automatic retry of a held statement:** none in this version; `statements rerun` by hand.

### Answered by the operator (2026-11-11)

5. **Encrypted statements:** a PDF with only an owner password is read, whatever its permission flags say, and its `encrypted` flag is kept. A PDF that needs a user password to open stays rejected as `unreadable`. This confirms the reading of "encrypted" that U1 was built on.

### Resolved during planning

- Schema: `receipt`. Migration numbers: 000023 and 000024 (main's last is 000022).
- Store: the local backend, a copy of the Drive file under `originals/`.
- Feed and revision numbers: the serialized, clock-floored counter above.
- No printed period: the date gate is skipped, and `period_start` null tells the ledger. Yearless dates with no printed period hold the account as `undated`.
- Statement status after the app review: the four statuses are mutually exclusive, a held statement always has a `hold_reason`, and a loan's `figures_only` counts as usable (see Statement status).

### Deferred to implementation

- The exact prompt wording, versioned from 1. Prompts describe layouts in general and contain no statement text.
- Bound values, tuned on the synthetic fixtures within the ceilings below.

---

## Implementation Units

Each unit is a pull request, merged in order. Every new check or fixture is seen to fail before it is trusted, with the command and output in its pull request.

### U1. Statement bounds and per-page reading

**Goal:** Read a statement's text, page by page, under its own limits, and say why when it can't.

**Files:** `src/receipt_index/pdf_validate.py`, `src/receipt_index/pdf_reader.py`, `tests/unit/test_statement_read.py`, `tests/pdfs.py`

**Approach:**
- `STATEMENT_BOUNDS = PdfBounds(check=PdfLimits(cpu_seconds=20, wall_seconds=60, max_bytes=25 MiB, memory_bytes=512 MiB), read=PdfLimits(cpu_seconds=90, wall_seconds=270, max_bytes=25 MiB, memory_bytes=1 GiB, max_output_bytes=4 MiB), max_read_pages=60)`. The read's own output cap holds the most text the token cap lets through, even escaped, so the parent never parses more: a small PDF whose font expands each glyph to many characters stops in the child as `text`. Every field is set explicitly, so a later change to `PdfLimits`' defaults doesn't move the statement bounds. `max_bytes` is what gives the `size` reason. Ceilings, not targets: a long statement is tens of pages and a few megabytes. `RECEIPT_BOUNDS` doesn't change.
- `read_statement(data: bytes, *, bounds=STATEMENT_BOUNDS) -> StatementRead` in `pdf_reader.py` (built in U1; planned here as `read_pdf_pages(path, bounds)`) takes its limits and page cap from the `PdfBounds` it is given, and the PDF's bytes rather than a path. `read_pdf` keeps its `RECEIPT_BOUNDS` defaults (`pdf_reader.py`, `limits` and `max_pages`), and the statement path never calls it.
- `read_statement` normalises each page's text once, before any check: a control character that breaks a line (carriage return, vertical tab, form feed, the file to unit separators, next line, the line and paragraph separators) becomes a newline, so the words on either side stay apart; NUL and every other C0 and C1 control except a tab, bidirectional overrides, isolates and marks, zero-width and other invisible characters, and Unicode tag characters are dropped. Then the token cap (a length check, measured without building the marked text) runs before the scan test, and nothing in the parent splits the text into words.
- A PDF with no pages, including one whose damaged cross-reference table recovers to no pages, is `unreadable`, not `scan`. An empty page keeps its place, so page numbers stay true.
- A statement read returns per-page text and whether the PDF is encrypted (an owner password only), or a reason: `pages` (from the page count `text_up_to_pages` already returns), `size`, `memory`, `crash` (any other signal), `unreadable`, `timeout` (CPU or wall clock, or a kill), `failed` (the reader's own error exit), `scan` or `text`. The receipt path keeps returning None. A PDF with a huge page tree surfaces as `memory` or `timeout`, not `pages`: pdfplumber builds every page object before the count is known, and the limits catch it.
- `timeout` and `failed` are retryable item errors, not rejections, since either can be the host's doing. The others are permanent rejections.
- **Text cap, in tokens.** `PdfBounds` stays about the PDF; the text cap is enforced once, in `read_statement`, after the child's read and before any model call. U5 takes the `text` reason from the read and never applies a cap of its own. It counts estimated tokens, not characters: `ceil(characters / 2.5)` over the page-marked text, a conservative ratio for numeric, columnar text, which tokenises more densely than prose. Over 85,000 estimated tokens is rejected as `over_bounds`, `text`. With the prompt (about 1,500 tokens) on top, each call's input then stays under the 100,000-token point where Haiku's price rises. Every call's real `input_tokens` is recorded (U5); the `statement-eval` run reports the real characters-per-token ratio on the fixtures, and the build fails the unit if any fixture comes in under 2.5.
- A document whose text, across all pages, has fewer than 20 non-whitespace characters, not counting `(cid:N)` glyphs (a font with no text map), or with no more readable characters than glyphs (each glyph counting as one), is a scan: rejected as `scan`. `read_statement` decides it, and U5 doesn't check again. The test is over the whole document, not per page: a readable character is a non-whitespace character outside a glyph. So a mixed document (a text cover page over scanned pages, or readable headers over blank or glyph-only transaction pages) passes this test and then fails the gates, so it is held rather than rejected. That is accepted for this version and stated in the user docs.

**Tests:** a fixture over 60 pages is refused with `pages`; a file over `max_bytes` with `size`; a CPU-bomb PDF stops with `timeout`, at the CPU limit; a read past the wall clock stops with `timeout`, at the wall clock; a crash signal gives `crash` and the reader's own error exit `failed`; a non-PDF gives `unreadable`, and so does a PDF that needs a password; an owner-password PDF is read, with its text and `encrypted` set; the `(cid:N)` boundary (as many glyphs as readable characters is `scan`, one more readable character passes, a glyph-only disclosures page beside readable pages passes); receipts' bounds and return values are unchanged; per-page text keeps page order; the token estimate rejects text just over the cap and passes text just under it.

### U2. Schema

**Goal:** Tables for statements, their sources, revisions, accounts and lines, and the counter.

**Files:** `db/migrations/receipt/000023_statements.{up,down}.sql`, `db/migrations/receipt/000024_statements_grants.{up,down}.sql`, `tests/integration/conftest.py`, `src/receipt_index/statement_repository.py`

**Approach (shape; names final at build):**

```sql
CREATE TABLE receipt.statement_feed_state (   -- one row: the last value handed out
    id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id), last_value BIGINT NOT NULL
);
INSERT INTO receipt.statement_feed_state (id, last_value) VALUES (TRUE, 0);  -- seeded here

CREATE TABLE receipt.statements (
    id                     UUID        NOT NULL DEFAULT uuidv7(),
    original_file_id       UUID        NOT NULL,   -- receipt.stored_files
    source_sha256          BYTEA       NOT NULL,
    current_revision_id    UUID        NOT NULL,   -- FK, DEFERRABLE INITIALLY DEFERRED
    document_type          TEXT        NOT NULL,   -- copy of the current revision's
    status                 TEXT        NOT NULL,   -- copy of the current revision's
    change_seq             BIGINT      NOT NULL,   -- place in the feed; set by trigger
    publish_count          INTEGER     NOT NULL DEFAULT 0,  -- incremented by republish
    created_at, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_statements_original UNIQUE (original_file_id),
    CONSTRAINT uq_statements_change_seq UNIQUE (change_seq),
    ...
);

CREATE TABLE receipt.statement_sources (    -- one row per source item, as provenance is for receipts
    id, statement_id, source_name, source_id UNIQUE, file_name, received_at, created_at
);

CREATE TABLE receipt.statement_revisions (  -- append-only, guarded like receipt.extractions
    id, statement_id /* FK, DEFERRABLE INITIALLY DEFERRED */, revision BIGINT UNIQUE,
    document_type, status, hold_reason, rejected_bound, masked_runs INTEGER NOT NULL DEFAULT 0,
    period_start, period_end, printed_account_count, pages,
    llm_model, prompt_version, first_pass_version, input_tokens, output_tokens, origin, created_at
);

CREATE TABLE receipt.statement_accounts (   -- append-only
    id, revision_id, ordinal, institution, last4 CHECK (last4 ~ '^[0-9]{4}$'), account_type,
    currency CHECK (currency ~ '^[A-Z]{3}$'), period_start, period_end, status, held_by_statement,
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

- **The counter.** One function, `receipt.next_feed_value()`, runs `UPDATE statement_feed_state SET last_value = greatest(last_value + 1, <microseconds since the epoch>) RETURNING last_value`; the row lock that UPDATE takes is the feed lock, held to commit. `receipt.lock_statement_feed()` takes the same lock without drawing a value: a write that also updates a statement row (a re-run's `set_current_revision`, a republish) takes it first, so every writer takes the feed lock before a statement row lock and a republish beside a re-run of the same statement can't deadlock. Both functions are `SECURITY DEFINER`. A `BEFORE INSERT` trigger on `statement_revisions` sets `revision` from it. A `BEFORE INSERT OR UPDATE` trigger on `statements` sets `change_seq` from it on insert, when `current_revision_id` changes, and when `publish_count` (an integer, starting at 0) changes; on any other update it keeps the old `change_seq`. `statement_repository.republish` increments `publish_count`, so a republish is an explicit marker, never inferred from which columns an update touched. Neither `revision` nor `change_seq` can be written by hand: the triggers overwrite any value given. `statements republish --all` commits one transaction per statement, so the feed lock is held for one statement at a time and the worker isn't blocked for the whole run; an interrupted run is safe to repeat.
- **Insert order, in one transaction.** The two tables point at each other, so both foreign keys are `DEFERRABLE INITIALLY DEFERRED`, and the check that a statement's `status` and `document_type` match its current revision is a `CONSTRAINT TRIGGER … DEFERRABLE INITIALLY DEFERRED` on `statements`, so it runs at commit. A new statement: take a statement ID (`SELECT uuidv7()`), insert the revision with it, then the accounts and lines, then the statement row pointing at the revision, then its `statement_sources` row and the `ingest_log` row. A re-run: insert the revision, its accounts and lines, then update the statement's `current_revision_id`, `status` and `document_type`. The foreign keys and the match check all run at commit, after both rows exist.
- A trigger refuses a `statement_lines` row on an account that isn't `clean`.
- No full account number anywhere: `last4` is four digits or null.
- 000024 grants (built in U2, stricter than first planned): `_write` gets `SELECT` on the statement tables, `INSERT` on their business columns only (no ids, timestamps, revision numbers or `change_seq`, except a statement's `id`, which the writer draws first), `UPDATE (current_revision_id, status, document_type, publish_count)` on `statements`, `EXECUTE` on `lock_statement_feed()` and `next_feed_value()`, and nothing on `statement_feed_state`, so no login can set the counter by hand. `_read` gets `SELECT` on all of them, and no `EXECUTE` on the counter's functions.
- A revision is complete when its transaction commits: a trigger refuses an account on a revision written by an earlier transaction, and a line on an earlier account, so nothing the feed has published changes in place. `publish_count` only steps by one.
- Database backstops, from the ledger's pre-review of U2 (2026-11-11):
  - a statement's current revision only moves forward: at commit it is the statement's newest, and every revision written becomes current in its transaction (a restore brings earlier revisions back with the rows, not through an update);
  - no run of eight digits in a line's `description` or `section`, an account's `institution`, a total's section name, or a source's `file_name` (`!~ '[0-9]{8}'`), behind the application's mask, which stays the main guard. Both the mask and this backstop are ASCII-digit-only by decision: fullwidth or Arabic-Indic digits are neither masked nor refused;
  - no `NaN` in any money column;
  - `match_key` is unique within a revision (`revision_id, account_type, last4, occurrence`, nulls not distinct);
  - at commit a `held` or `rejected` revision has no `clean` or `figures_only` account, and `held_by_statement` appears only in a `held` revision, since the ledger imports a clean account's lines whatever the statement's status. Whether a usable revision is `complete` or `incomplete` is U4's gates' decision, not checked by the database.
- Down migrations drop the tables and the function. See Rollback for the stored originals.
- `_run_migrations()` mirrors 000023, the seed row included; `apply_migration_grants` applies 000024.

**Tests:** up, down and up again on a scratch database, with the seed row present after each up; a new statement and a re-run commit with the insert order above, and the same writes with a revision whose status differs from the statement's fail at commit; **the cursor-skip guarantee**: writer A inserts a revision and stops before committing (held at a barrier); writer B starts its own revision and blocks on the advisory lock; a reader lists the feed and sees neither; A commits, B proceeds and commits; the reader then sees A before B, with B's `change_seq` above A's; a counter value set below the last stays monotonic (the restore case); a statement whose `change_seq` is set below a reader's cursor (a restored row) is invisible to that cursor until `republish`, and visible after it with its `revision` unchanged (the fixture, connected as the `main` superuser, runs `ALTER TABLE receipt.statements DISABLE TRIGGER` for the `change_seq` trigger, sets the value, and enables it again, which is what a restore does in effect); an update that changes neither `current_revision_id` nor `publish_count` keeps `change_seq`; `republish --all` over three statements commits three transactions; a line can't attach to a held account; `last4` rejects five digits; `currency` rejects `usd`.

### U3. Extraction models and prompts

**Goal:** The first pass, the deposit and card extraction, and the loan figures, as pydantic-ai agents with versioned prompts.

**Files:** `src/receipt_index/statement_extraction.py`, `tests/unit/test_statement_extraction.py`

**Approach:**
- Output models with `extra="forbid"`. Money is a two-place decimal string and never a float, as in the spike, and is reformatted to the contract's money format (no `+`, no separators, `0.00` for zero) before it is stored.
- First pass: document type, accounts (label, type, page range, and its own period where printed), printed account count, the document's period. The label is used only to tell the extraction call which account to read. It is not stored, since it can hold a name.
- Every prompt's document text is built with `page_marked_text` from the `StatementRead`'s page texts, the same function the token cap measured, so the cap and the prompt agree. Text inside a page shaped like a page marker (`[Page 3]`, any case or spacing, ASCII or full-width brackets) is shown in parentheses, so a statement can't forge a page boundary; the change keeps the length, so the cap still measures the prompt exactly.
- Dates: `DateParts` refuses a date that doesn't exist (30 February; 29 February in a printed common year). `period_dates` and `place_date` return a reason instead of `None`: a period is `not_printed`, `yearless` or `invalid`; a date is `undated`, `ambiguous` or `unplaceable`. U4 holds an `undated` or `ambiguous` posted date as `undated` and an `unplaceable` one as `date_out_of_period`; a transaction date that can't be placed is null.
- A signed amount's leading Unicode dash or minus is read as `-`; a page number refuses a boolean.
- Calls: every agent ends a refusal as a content-filter error (`kind` `refused`), as the receipt agent does. A capped call is the only one with `output` None; U4 checks `capped` first.
- One test per call runs it through the locked pydantic-ai and anthropic packages against the stub Messages API (`tests/unit/test_statement_stack.py`): a reply, a `max_tokens` stop, a context-window stop and a refusal. `statement-eval` reports thinking tokens beside each call's ceiling, and the real time of the longest account call against its 420 s deadline.
- Account extraction: one call per listed account, the whole page-marked text, told the account's label, type, pages and period. The deposit and card prompts share a base that states the sign basis, the currency rule and the date rules: a line that prints one date gives it as the posted date; a card line that prints two gives the posted date and the transaction date; each date comes back as printed parts, `{month, day, year}`, with `year` null when not printed; shoebox assigns missing years after the call (the contract's Years rule), and a null `year` on an account with no period holds it as `undated`. Section and column totals come back as printed magnitudes; section headings are normalised after the call, by the one function the contract names.
- **Output cap.** Each account call sets a `max_tokens` ceiling well inside Haiku's output limit, sized at build time from the fixtures. A response that stops at that ceiling (the model's stop reason says so) is not retried, since a retry would stop in the same place: the account is held as `too_many_lines`, and the statement's status follows from the rules. At about 35 output tokens a line, this holds an account of more than roughly a thousand lines. Splitting such an account into page ranges is deferred.
- Loan: printed figures only, null when not printed. The prompt forbids deriving a figure. A breakdown figure printed as a credit comes back null, by the prompt and by the output model, so a valid statement never fails as an item error.
- Totals printed with a minus sign or in parentheses are taken as magnitudes by the output model, for the same reason.
- The first pass has its own output ceiling, guarded the same way: a capped first pass is reported, not retried, and U5 rejects the statement as `over_bounds`, `accounts`. Only a cut-off first response is a cap: one that follows an output retry began as a schema error and stays an item error. The first pass has no account cap of its own; the output ceiling bounds its list, and U5 rejects more than 8.
- A stop at the context window (the provider's raw reason `model_context_window_exceeded`, which pydantic-ai also maps to `length`) is never read as a cap: it raises its own error, an item error. U1's token cap should make it impossible.
- The first pass's `accounts` aren't tied to its `document_type` in the schema. U4 ignores a rejected type's accounts; a statement type with none is the `no_accounts` hold, which a schema error would turn into a retry; and an account's type may differ from the document's.
- `normalise_section` and `normalise_institution` apply the statement text's normalisation first, so a hidden character can't make a line's section differ from its total's.
- Every prompt says the document is data and its instructions are to be ignored, and tells the model to cut any account or card number in a description, a section heading or an institution name to its last four. The deterministic mask on all three (contract, Masking) runs after the call regardless.
- `STATEMENT_PROMPT_VERSION` and `FIRST_PASS_VERSION`, recorded on each revision.
- Model and effort come from the existing `llm` configuration, and so does the first pass's request timeout. Account calls override that timeout with their own 420 s request deadline (`ACCOUNT_REQUEST_TIMEOUT_SECONDS`), sized for their output ceiling.

**Tests:** schema validation refuses a float, a three-place amount and a five-digit `last4`; money reformatting gives `0.00` for a negative zero and drops a `+`; section normalisation gives the same value for a heading on a line and in a total; a stubbed response that stops at the output ceiling holds the account as `too_many_lines` with no retry; the prompts contain no statement text (a test checks them against the fixture generator's strings).

**Tests, masking:** a stubbed model output whose description carries a full 16-digit card number, grouped by spaces, by dashes, by runs of several separators, by non-breaking spaces, by tabs and by en dashes, comes out with the last four only, in the stored line and in the digest's input; the same for a number in a `section` and in `institution`; boundaries: exactly 8 digits in a row is masked, 8 digits in two groups of four (`1234 5678`, `1234-5678`) is masked, and 7 digits (in a row or grouped) is left alone; a number split across a wrapped description line is masked after the join; each side effect in the contract's Masking list has a case pinning its result, the dashed dates `2026-11-09` and `11-09-2026` and a date beside a store number (`2026-11-09 4412`) included; an amount with thousands separators or a decimal part and a time are left alone, while slash and dotted dates and dotted numbers are cut; a Unicode digit (such as a full-width one) isn't matched; `masked_runs` on the revision equals the number of runs cut; the mask is written to fail first against an identity function.

What the stubbed tests can't show: whether the real model trims account numbers before the mask, places printed years correctly, or picks the right date column. The stub returns what the test chose. Those are measured by `make statement-eval` against the real model on the synthetic fixtures (Testing Strategy), which counts masked runs and dates outside each fixture's period.

### U4. Gates

**Goal:** Pure functions from the first pass and the account extractions to account and statement statuses.

**Files:** `src/receipt_index/statement_gates.py`, `tests/unit/test_statement_gates.py`

**Approach:** from the spike's grader, re-written here, in the contract's order:
- per account, the first failure class that applies: `empty_account` (a deposit or card account with no lines that isn't dormant; a loan with no printed figure at all), `no_beginning_balance`, `no_ending_balance`, `too_many_lines`, `undated`, `does_not_foot` (to the cent, with the difference), `date_out_of_period` (the account's period, with the card grace; skipped when the account has no period), `balance_chain_break` (walked in date order, carrying amounts across lines with no printed balance), `totals_mismatch` (magnitudes);
- dormant account: no lines and equal printed balances is clean;
- statement: `rejected`, else the hold reasons in order (`no_accounts`, `dropped_account`, `account_count_mismatch`, `no_usable_account`), else `incomplete` or `complete`. The first three set `held_by_statement` on every account and keep each account's own class;
- the canonical form and `content_digest`. The worked example is pinned in U4's tests (its canonical JSON and digest); U7's contract document copies it;
- dates: a date that doesn't exist (30 February) is refused by `DateParts` (U3). A posted date `place_date` calls `undated` (no year, no period) or `ambiguous` (it fits more than one year of a long period) holds the account as `undated`; one it calls `unplaceable` (a yearless 29 February with no leap year to put it in) fails `date_out_of_period`, so its line can't drop out of a clean account. An account or document period that is `invalid` or `yearless` gives way to the next (the account's own, else the document's). A transaction date that can't be placed is null, since it isn't gated;
- the mask on `description`, `section` and `institution` runs before the gates and the digest (see the contract's Masking); masked runs are counted into the revision's `masked_runs`, never logged as text. Masking is not a gate. It covers every free-text field the contract carries: a line's `description` and `section`, the `section` of section totals and column totals, and `institution`. (`ListedAccount.label` is never stored; U3 masks it before it goes into the account request.)
- a rejected `document_type` gives a rejected statement with no accounts, whatever the first pass listed.
- a capped account (`too_many_lines`) carries no figures, but keeps its period (its own printed one, else the document's), since the period is in its digest.
- a total whose printed heading normalises and masks to nothing can't be matched to lines: the account fails `totals_mismatch`, and that total's section is published as an empty string: a section total's is never null, and a null column-total section means a whole-account table.
- every gate compares exact decimals: one cent decides footing, the chain and both kinds of total.
- input the output models should have refused (a non-finite or non-decimal amount, an output of the wrong kind, a run count that doesn't match) raises `ValueError`; a `decimal.InvalidOperation` is re-raised as one. U5 treats it as an item error, with no revision.
- only a capped call has no output; `gate_statement` refuses any other call with none. U5 passes an account it doesn't call as an empty extraction, which gates as `empty_account`.
- section keys: a line's `section` and a total's `section` go through the same normalisation and mask, so the same printed heading gives the same key. `None` is the same key on both sides and means no heading: a line with none counts toward no section total, since a section total's section is never `None`, and a column total with none covers the whole account, as the contract says.
- The money strings inside `section_totals` and `column_totals` (JSONB) have no database check, unlike the NUMERIC columns. The writer refuses any total that isn't a finite two-place decimal (NaN and infinities included) before it is stored, with a test.

- **Shapes the schema allows but the gates never write** (a decision, recorded): a complete revision with no accounts, a clean account with `difference` or `first_page` set, a clean account with a null `line_order`, and a date grace on a deposit. U2's checks accept them; a later tightening migration could refuse them once the gates have run in production for a while.

**Tests:** one test per gate, failure class, hold reason and statement status, each written to fail first against a stub that passes everything. Named cases: a loan-only statement is `complete`; a loan beside a deposit account held by its own gates is `incomplete`; a statement with no accounts is `held` with `no_accounts`; an empty loan holds the statement as `dropped_account`, and every account, the clean ones included, is held and marked `held_by_statement` while a held one keeps its own class; a newest-first chain passes when walked in date order and fails when walked in printed order; a December to January card period with a line dated in late December passes, and one dated in the next December fails; a digest test against the contract document's worked example; the same account with its institution's whitespace varied keeps its `match_key` and its digest; a test walks every `str` field of `AccountExtraction` and `LoanExtraction` (nested models included), puts a 16-digit number in each, and finds it in no gated account, so a new text field can't skip the mask.

### U5. Statement pipeline and retries

**Goal:** One statement item from fetch to commit, and its retries, kept apart from receipts.

**Files:** `src/receipt_index/statements.py`, `src/receipt_index/pipeline.py` (`RunControl`, `run_due_retries`), `src/receipt_index/service.py` (the watchdog), `src/receipt_index/repository.py`, `src/receipt_index/fetching.py`, `src/receipt_index/requeue.py`, `tests/integration/test_statement_pipeline.py`

**Approach:**
1. `started` row in `ingest_log`, with `source_type = 'gdrive_statement'`.
2. Store the original with `put(purpose="original", folder=StoreFolder.for_original(...))` through `_put_original`, so it is write-once under `originals/` like every other original. If a statement already has that file, add a `statement_sources` row and finish with no model call.
3. Check and read the PDF with `read_statement` under `STATEMENT_BOUNDS`. It has already applied the scan test and the token cap; U5 maps its reason and applies neither again:
   - `scan`, `unreadable`: a `rejected` revision with that `document_type`;
   - `pages`, `size`, `memory`, `text`, `crash`: a `rejected` revision, `over_bounds`, with that `rejected_bound`. A crash is recorded as `crash`, never as `memory`, so a reader isn't misled about which limit applied;
   - `timeout` (a CPU or wall-clock limit, or a `-9` kill, which can be the host's load or a host out-of-memory kill caused by the file) and `failed` (the reader's own error exit): an item error, retried with the existing backoff and terminal after `retry_max_attempts`. The bound matters: a file that really does exhaust the host is retried a fixed number of times, never forever.
   - Log `StatementRead.encrypted` for each statement read.
   - One revision per statement per transaction: the database refuses a revision that isn't made current in its own transaction, so runs can't be batched. Write and commit each run on its own.
   - Keep the write transaction short. The feed lock is held from the first revision insert to commit, and every other statement writer waits on it: read the PDF and make every model call before the transaction starts, then write the revision, accounts, lines and statement and commit at once.
   - Mask `statement_sources.file_name` with the same digit-run mask as the extracted text before storing it: a dropped file's name can carry an account number, and the read role (the MCP server, backups) can read the column.
   - A parent-side `OSError` from starting the reader propagates out of `read_statement` rather than reading as `failed`. It can come from `Popen` (no processes or file descriptors left) or from the `TemporaryFile` the output goes to (a full or unwritable temporary directory). U5 catches it around the read and treats it as the same bounded, retried item error.
   - A defensive check: `len(page_texts) == pages` for every read that returns text; a mismatch is an item error, not a rejection, since it would be a bug in the reader.
   - To consider: a separate `cpu` reason for a read stopped at its CPU limit, rejected rather than retried, since a file that burns 90 s of CPU will burn it again. Today the CPU limit and the wall clock both read as `timeout` and are retried a bounded number of times.
4. First pass. A non-extractable type, more than 8 accounts, or a capped first pass (`over_bounds`, `accounts`): a `rejected` revision. An account whose `last_page` is past the document's page count isn't called: U5 passes it to the gates as an empty extraction, gated as `empty_account`, which holds the statement as `dropped_account`.
5. One call per account, then the gates.
6. One transaction, in the U2 insert order: the revision, accounts, clean accounts' lines, the statement (new or updated), its source row, and an `ingest_log` `success` row with the token totals.

- **Watchdog steps.** `RunControl` gains `step(source_id)`, which re-marks the running item's clock: `serve`'s watchdog then times from the last step, not from the item's start. Steps 3, 4 and each account call call it. The receipt path doesn't, and the test doubles that implement `RunControl` get a no-op. The longest step is one account call: at most `MAX_REQUESTS` (2) model requests of up to 420 s each, 840 s, under `item_timeout_seconds` (1200).
- **Startup.** `serve` builds the mask's pattern once at start (`statement_extraction._masked_run()` and `_dashes()`, about a second), so the first statement doesn't pay for it inside its watchdog step.
- **Tracing.** The statement agent constructors take a model object, never a name, and accept only an Anthropic model built by `llm.anthropic_model` (its client resolves traces and propagation off) or pydantic-ai's test models; a name or a default-client Anthropic model is refused (U5's `StatementAgents.create` does the same). Every statement agent also sets `instrument = False` after construction (there is no constructor argument), which outranks a global `Agent.instrument_all()` or `logfire.instrument_pydantic_ai()`: a span would carry the statement text and the model's output. A test turns tracing on globally with an in-memory exporter and finds no span from the first pass or an account call, while an ordinary agent is traced. The pydantic-ai console banner is off for the process (`llm.py`). The Anthropic SDK traces every request on its own, through the global tracer provider and the `ANTHROPIC_OPEN_TELEMETRY_*` variables (with the content mode, a span holds the page text): `llm.anthropic_model` builds its client with traces and propagation off, whatever the environment says, and a stack test sets a global provider and the content mode, with a traced control, and finds no span. Never in this process: `logfire.instrument_anthropic`, `logfire.instrument_httpx` (with or without `capture_all`), `logfire.instrument_pydantic_ai`, `Agent.instrument_all`, any `opentelemetry.instrumentation` package, or the `opentelemetry-instrument` launcher, since they patch the client or its transport and export what it sends. A unit test checks the package and the deployment files for them. A startup check could refuse statement work when one is active (an `opentelemetry.instrumentation.*` or `logfire` module loaded, or the httpx send methods wrapped), but each library patches differently, so it would be a heuristic that misses new ones: the tests and the deployment rule are the guard, and U5 may add the module check as a cheap extra.
- **Frame locals.** `run_first_pass` and `run_account` hold the page texts, the request and the run's messages in their locals. No error reporter that captures frame locals may run in this process (Sentry's default `include_local_variables`, rich's `show_locals`, a debugger's post-mortem dump) unless it is configured not to. A rule for U5 and for the deployment notes.
- **Payload loggers.** `llm.quiet_payload_loggers()` holds every top-level logger whose name starts with `anthropic`, `httpx` or `httpcore` (the installed stack has the 2.x forks `httpx2` and `httpcore2` too) at INFO or above whatever the app's level, since at DEBUG the SDK logs each request, statement text and earlier model output included. The CLI's logging setup and every agent, receipt or statement, call it. A stack test plants a distinctive number in a failing call's output, captures every record at DEBUG, and finds none of its eight-digit windows and no DEBUG record from those loggers. A child logger given its own level (say `anthropic._base_client` set to DEBUG by hand) isn't covered, since the clamp sets only the top-level loggers: U5 decides whether to clamp every name under the prefixes in the logger registry or to leave that as the documented way to debug the SDK.
- **Logging.** A statement call raises only `StatementCallError(kind, error_class, usage)` (U3), raised with no cause or context, so even a traceback holds no model output: a pydantic `ValidationError` quotes it, and the ceiling errors' `.response` holds it, and either can carry an unmasked number. Its `kind` is `schema`, `context_window`, `timeout`, `refused` or `provider`; its `error_class` is `errors.classify` of the original error, which U5 uses to pause, retry or fail, since the chain that `classify` would read is gone. Log it by kind and class.
- **Errors.** A transient model or Drive error pauses the worker as today. An account call's request timeout counts as an attempt, so a slow account is retried a bounded number of times, never forever. A model or schema error on any call is an item error, retried with the existing backoff, and terminal after `retry_max_attempts`. It writes no revision. A held account, a `too_many_lines` cut-off included, is a successful outcome, not an error. Any exception from `gate_statement` is an item error, logged by its class only: the gates raise one `ValueError` with a fixed message and no cause, whatever went wrong.
- **Retries stay statements.** Every place the retry and requeue code assumes a receipt changes:
  - `select_due` is called with `source_type='gdrive_statement'` for a statement source. Its name match only ever finds that source's own rows (U6's validation refuses two sources with one name), and its fallback for a removed or unnamed source matches the type, so a statement row is never offered to a receipt source, and the reverse.
  - `_NO_PROVENANCE` becomes `_NOT_DONE`: no `provenance` row and no `statement_sources` row for the item. It is used by `select_due`, `select_retry_candidates` and the other queries that use it today. `has_receipt` in the retry loop's "latest row still due" check becomes `item_done`, true for either. `get_processed_source_ids` also unions `statement_sources`.
  - `fetching.SourcePool.candidates` compares an item's `source_type` with the config's `type` today. It compares with the source's ingest type instead (`gdrive_statement` for a Drive source with `documents: statements`, the config's `type` otherwise), so a statement item whose source is gone is re-fetched only through statement sources.
  - `run_due_retries` sends `gdrive_statement` items to this pipeline, never to `process_item`.
  - The `retry` command (`RetryFilter`, `apply_requeue`) writes its `queued` row with the item's own `source_type` already, so a requeued statement item stays a statement item and the next retry pass sends it here. `orphan_report` looks for renditions, which a statement item never has, so it reports none for one. `failures` lists statement items by their source name, and `failures --source` filters them.
- `statements rerun` (U8) runs steps 3 to 6 from the stored original as a new revision.

**Tests:** each fixture end to end with a stubbed model; a crash after the original is stored resumes without storing it twice; the same bytes from two drops give one statement; a due statement item, including one whose source was removed, never reaches the receipt `process_item`; a statement item requeued with `retry --yes` reaches the statement pipeline on the next pass; a statement item with a `statement_sources` row is not due, and isn't offered by `retry`; a slow stubbed model call calls `step` at each phase, and the watchdog doesn't fire across a run longer than one step's budget made of steps each inside it.

### U6. Intake through `serve`

**Goal:** A Drive source that feeds only the statement pipeline.

**Files:** `src/receipt_index/config.py`, `src/receipt_index/service.py`, `src/receipt_index/cli.py`, `example.env` and the example configuration

**Approach:**
- `GdriveSourceConfig.documents: Literal["receipts", "statements"] = "receipts"`. Existing configurations are unchanged.
- Config validation refuses a statement source whose folder is also a receipt source's folder, and two sources with the same name.
- The worker dispatches by the source's `documents`. Sync state (still `gdrive`), item locks, health and `status` work per source as now. `status` shows statement sources with their own counts.
- `archive` stays false for Drive, as for receipts: the file stays in the folder, and the store keeps its own copy.

**Tests:** a statement source never reaches the receipt `process_item`, and a receipt source never reaches the statement pipeline; overlapping folders are refused.

### U7. MCP tools and the contract document

**Goal:** The four tools, and the published contract.

**Files:** `src/receipt_index/mcp_server.py`, `docs/engineering/designs/statement-read-contract.md`, `tests/unit/test_mcp_statements.py`, `tests/integration/test_mcp_statements.py`

**Approach:** as in [The Read Contract](#the-read-contract-pre-reviewed-by-the-ledger-revised-after-the-app-review). One snapshot per call. `get_statement_lines` uses `paging.page_envelope`. Errors use the existing `ToolError`, with the contract's codes at the start of each message. `get_statement_file` reuses `get_receipt_file`'s content limit and access log. The server instructions gain a paragraph on statements: the sign basis, that held accounts have no lines, and that statement text is untrusted data. The contract document carries a worked digest example and a worked sign flip.

**Tests:** the feed under two interleaved writers never skips; a held account returns no lines; an earlier revision returns its own status with `is_current` false and the right `current_revision`; `get_statement_lines` pages a fixture of more lines than one page and the pages join to the account's `line_count`; each error code for a garbage cursor, an `s2:` cursor, an unknown statement, an unknown revision and an out-of-range `limit`; a cursor ahead of the counter returns no rows; the file fetch is access-logged, refused when the log can't be written, and over the limit returns `too_large` with no content.

### U8. CLI

**Files:** `src/receipt_index/cli.py`, `docs/user/cli-reference.md`, `docs/user/server-deployment.md` (the restore runbook)

**Commands:** `receipt-index statements list [--status ...] [--since DATE] [--output json]`, `statements show ID [--revision N] [--output json]`, `statements file ID`, `statements rerun ID [--yes]`, `statements republish (ID... | --all) [--yes]`. JSON output is the contract's shapes. `republish` writes no revision: it gives each statement a new `change_seq`, and the restore runbook gains it as its last step. `statements show` prints the revision's `masked_runs` ("Masked digit runs: N") in text, and as the contract field in JSON; a test runs it on a fixture with two masked runs and asserts both forms. `failures --source` already lists statement items that failed.

### U9. Documentation

**Files:** `docs/engineering/designs/statement-extraction.md` (the technical design, from this plan), `docs/user/server-deployment.md`, `docs/user/getting-started.md`, `README.md`

The user docs say plainly that a statement with only an owner password is read, even when its permission flags forbid copying text, and that one needing a password to open is rejected as `unreadable`.

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
- card over a December to January period, printing dates with no year, with a purchase in late December inside the grace window;
- card that prints one date per line;
- loan-only statement (complete), and a loan beside a deposit account that doesn't foot (incomplete);
- statement whose summary lists two accounts, with only one present (held, `account_count_mismatch`);
- a mixed document: a text cover page over image-only pages (held by the gates, not rejected);
- loan statement with principal, interest and escrow printed, and no opening principal;
- no-activity loan bill, escrow notice, a letter (rejected);
- an image-only page set (scan), a 61-page PDF (over bounds), a file over the size bound, text over the token cap, and a non-PDF file (unreadable).

### With a stubbed model

pydantic-ai's `FunctionModel` returns a chosen extraction per call, so each gate is driven to pass and to fail on purpose: a dropped line, a flipped section, a duplicated page-break line, a date outside the grace, a date in the wrong year, a broken chain, an empty account, an empty loan, a wrong account count, no accounts at all, a response cut off at the output ceiling. The stub proves the gates and the plumbing, not the model: what the model does with a real layout is measured only by the real-model run below.

### Against the real model, by hand

`make statement-eval` runs the fixtures against the configured model and prints a counts-only table: per fixture, the status and class against the expected ones, masked runs, dates outside the period, and characters per input token. It isn't run in CI and costs a few cents. Its output on the synthetic fixtures goes in U3's and U4's pull requests.

### Integration

Pipeline, retry, feed and MCP tests run against the devtest Postgres under the shared lock, as now.

---

## Cost

Haiku 5.5 is $0.10 per million input tokens and $0.50 per million output tokens, for prompts under 100,000 tokens. The token cap (U1: 85,000 estimated tokens of text, at 2.5 characters a token, plus about 1,500 of prompt) keeps each prompt under that point.

Per statement: input ≈ (1 + accounts) × (document tokens + about 1,500 of prompt); output ≈ 35 tokens a line, plus thinking at medium effort. A text page of prose is roughly 700 to 1,000 tokens; a dense numeric page can run higher, which is why the cap counts estimated tokens rather than pages.

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
- Roll back the code: redeploy the previous image, **only after** the statement source is out of the config. The order matters: the previous image's `GdriveSourceConfig` ignores keys it doesn't know, so it would read a source with `documents: statements` as a receipt source and send statements down the receipt pipeline. With the source removed, the previous image's `select_due` can't match a `gdrive_statement` row by name (no source has that name) or by type (its fallback matches `gdrive` only), and its `retry` command would only write another `gdrive_statement` row that nothing selects. The new tables are unused by it.
- Remove the data (only if wanted): the 000024 and 000023 down migrations. The stored originals stay in the store and in `stored_files` and `file_locations`. No command lists them today; they are listed from those tables by hand if they are ever to be removed.

---

## Risks & Mitigation

| Risk | Mitigation |
|---|---|
| A statement reconciles with a misdated or mis-described line | The date gate catches dates outside the period; the operator spot-checks a few after rollout. Descriptions aren't verifiable by arithmetic: stated in the contract. |
| The first pass misses an account and nothing prints a count | An empty or missing account list holds the statement; a missed account with no printed count is undetectable in shoebox. The ledger's own balance check on the account is the backstop. Stated in the contract. |
| The first pass flips between runs | Holds are safe; `statements rerun` by hand. Revisit an automatic retry after a month. |
| A description, section heading or institution name carries a full account number | A deterministic mask on all three cuts every run of 8 or more digits to its last four, with Unicode spaces and dashes folded first, before the gates and the digest (the ledger's change, decision 6). It also cuts long reference numbers and dates, accepted (contract, Masking). |
| A number printed in non-ASCII digits | Not matched by the mask, which is ASCII-only. Statements print ASCII digits, and the model's output is reviewed by `statement-eval`'s masked-run count; accepted for version 1. |
| Two section headings differ only in a long digit run | Both mask to the same `section`, so their lines and totals collapse into one section. The section-totals check then compares one total with both sections' lines and holds the account as `totals_mismatch`, which is safe but a false hold; accepted, since headings with long numbers are rare. |
| A non-USD statement prints only `$` | It is labelled `USD` and passes the ledger's safe default, undetected here. Accepted for version 1, as the ledger's statements are US accounts; a statement that names another currency anywhere gets that code. |
| An account too long for one call's output | The output ceiling holds it as `too_many_lines` with no retry; splitting by page range is deferred. |
| A restore hides a statement behind the ledger's cursor | `statements republish --all --yes` is the restore runbook's last step (U8). |
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
- A deposit, card or loan statement with no accounts read as complete: held (since the app review, with the hold reason `no_accounts`).
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

**App review on the pull request (2026-11-11)**

Every point is answered on the pull request. Changes made:
- Statuses contradicted each other for a loan-only statement and for a loan beside a held account: the four are now mutually exclusive and tested in order, and `figures_only` counts as usable. The requirements document's F1 and R13 are aligned.
- A held statement could carry no reason, and a completeness failure overwrote an account's own class: a statement-level `hold_reason`, a `held_by_statement` mark on each account, and the account keeps its own class. An empty loan is defined (no printed figure at all) and has the class `empty_account`.
- Contract gaps: `currency` per account; exact money, zero, timestamp and integer formats; a period per account, in the digest; `ordinal` follows printed order and the chain is walked in date order; one normalisation for `section` on lines and totals, null for a line under no heading; one printed date goes in `posted_date`, and `transaction_date` isn't gated; year inference and the `undated` class; held accounts carry no lines by design; `is_current` and `current_revision`; feed limits, error codes, a cursor ahead of the counter, and paged lines through a new `get_statement_lines`; `revision` as a 64-bit integer; `change_seq` separated from `revision`, and `statements republish` so a restored revision reaches the ledger.
- `match_key` drops `institution`, and `institution` is normalised (the ledger's answer, decision 5).
- Feasibility: the insert order with deferred foreign keys and a deferred match check, and the seeded counter row; every receipt assumption in the retry and requeue code (`select_due`'s type, `_NO_PROVENANCE`, `has_receipt`, `get_processed_source_ids`, `SourcePool.candidates`, `retry`, `failures`); a token-based text cap; an output ceiling and `too_many_lines`; every `STATEMENT_BOUNDS` field explicit and a `read_pdf_pages` that takes its bounds; `RunControl.step`; the rollback order; the file-content limit.
- Tests and fixtures: a year boundary, a card printing one date, loan-only and loan-beside-held, an empty loan, a count mismatch, a mixed document, the barrier test for the cursor guarantee, a restored `change_seq` below the cursor, `match_key` stability, `is_current`, the error codes, and a statement of what the stubbed model can't show.

Not changed:
- Section totals stay unsigned magnitudes; the footing check pins direction. Stated in the contract.

**Second app review, and the ledger's change (2026-11-11)**
- `get_statement_lines` has its own page maximum, `MAX_LINE_PAGE_SIZE` (1,000), separate from `MAX_PAGE_SIZE` (200).
- Republish is an explicit `publish_count` increment, and the trigger redraws `change_seq` only on insert, a new current revision or that increment. `republish --all` commits per statement.
- The restore test disables the `change_seq` trigger in its fixture to set a low value.
- A bare `$` stays `USD` (the lead's call: a null would hold nearly every US statement); the undetectable non-USD case is in Risks.
- An empty loan is a completeness failure under the operator's answer 3, stated.
- Dates come back as `{month, day, year}` with `year` nullable; shoebox assigns years; an unplaceable date takes `period_end`'s year and fails the date gate.
- Null-`last4` pairing by document order, and the feed's single `next_cursor`, are stated.
- The ledger's change: a deterministic mask (on descriptions, later widened to sections and institution names), replacing the earlier "no scrub" decision and the 9-digit flag, with its side effects stated and accepted.

**Third app review (2026-11-11), all on the mask**
- The rule is an exact pattern over ASCII digits, `[0-9](?:[ -]*[0-9]){7,}`; runs of several separators join.
- Eight-digit dates with any separator are listed as side effects and tested; amounts that can occur and times are stated as not masked, and every other grouping of 8 or more digits is cut.
- R18 overclaimed: the mask now covers `section` and `institution` too, and the contract says `get_statement_file` returns the original with every number.
- `masked_runs` is stored on each revision and shown by `statements show`.
- Boundary tests: exactly 8 digits, 8 digits in two groups, and 7 digits.

**Fourth app review (2026-11-11)**
- Non-breaking spaces, tabs and other Unicode spaces and dashes escaped the mask: separators are folded to ASCII first, with tests.
- The adjacency side effect is stated for any digit groups separated only by spaces and dashes, with a date-beside-store-number test.
- `masked_runs`: what it counts is stated, and `statements show` prints it, with a test.
- Risks gains section headings that collapse under the mask, and non-ASCII digits.
- The description-only wording in decision 6, the dispositions, the prompt bullet and the Risks row is aligned.

**Not changed (document review)**
- Canonical JSON details (escaping, normalisation) are now stated; the worked example in the contract document settles the rest at build time.

---

## Sources & References

- Origin: `docs/brainstorms/2026-11-10-statement-extraction-requirements.md`
- `docs/engineering/designs/always-on-ingest.md` §§1, 5, 6, 7
- `docs/engineering/designs/evidence-store.md` §§2, 3, 6, 9
- `docs/engineering/designs/receipt-data-model.md` (extraction history, cost per receipt)
- `src/receipt_index/pdf_validate.py`, `pdf_reader.py`, `mcp_server.py`, `pipeline.py`, `repository.py`, `fetching.py`, `store.py`
