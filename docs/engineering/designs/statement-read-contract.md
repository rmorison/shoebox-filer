---
title: "Statement read contract"
type: design
status: active
date: 2026-11-19
contract_version: 1
---

# Statement read contract

*How a client (the ledger) reads extracted bank, card and loan statements from the receipt index's read-only MCP server.*

This document is the single source for the contract's shapes. It is built from the statement extraction plan (`docs/plans/2026-11-10-001-feat-statement-extraction-plan.md`, "The Read Contract"), which records why each decision was made. The server's code (`src/receipt_index/mcp_server.py`) follows this document, and a test recomputes the worked digest example below with the code that writes the digests.

---

## Versioning

- Every response carries `contract_version`, an integer. This document describes version **1**.
- **Additive, not breaking:** a new field; a new document type for a rejected document; a new failure class, hold reason or error code; a new tool. Each is noted here when it ships. A client ignores fields it doesn't know and treats an unknown failure class as held. A new field stays out of `content_digest` until a version change.
- **Breaking:** removing or renaming a field; changing a field's meaning, sign or format; a new `account_type`; a new account `status` that carries lines; a change to the digest's field list. A breaking change ships as new tools beside the old ones (for example `get_statement_v2`), and the old ones stay until the client has moved.
- **The ledger's safe defaults.** It imports lines only from an account whose `status` is `clean`, whose `account_type` is `deposit`, `card` or `loan`, and whose `currency` is `USD`. It treats any other status, type or currency, a null currency included, as held. A call refused with `withheld` is held for review, never read as empty.

## Formats

- **Money:** a JSON string matching `^-?(0|[1-9][0-9]*)\.[0-9]{2}$`: two places always, no `+`, no thousands separator, no currency sign. Zero is `"0.00"`, never `"-0.00"`.
- **Currency:** an ISO 4217 code per account. Every money field on an account, its lines included, is in that account's currency.
- **Dates:** `YYYY-MM-DD`, the calendar date as printed, no time zone.
- **Timestamps** (`extracted_at`): RFC 3339 in UTC with a `Z` suffix and microseconds, for example `2027-02-03T04:05:06.000789Z`.
- **Integers** (`revision`, `ordinal`, `page`, counts): JSON numbers. `revision` is a 64-bit integer whose values are microseconds since the epoch, about 1.8 × 10^15 today: exact as a JSON number (below 2^53) until about the year 2255, but a client stores it as a 64-bit integer, never a 32-bit one or a float.
- **Hex:** `source_sha256` and `content_digest` are lower-case hex SHA-256.
- **Text:** UTF-8, as extracted (no Unicode normalisation), except that the masked fields below have their separators and digits folded (see [Masking](#masking)).
- **Normalised text:** `institution` is trimmed, with inner whitespace collapsed to one space and case left alone. `section`, on lines and in totals alike, is trimmed, inner whitespace collapsed, a trailing colon dropped, and lower-cased, so the same printed heading gives the same `section` on a line and in a total.
- **Masked text:** `description`, `section` (on lines and in totals) and `institution` all go through the mask, after normalisation and before the gates and the digest.

## Tools

Three read-only tools on the existing MCP server. Each call reads one consistent snapshot (`REPEATABLE READ, READ ONLY`).

| Tool | Arguments | Returns |
|---|---|---|
| `list_statement_changes` | `cursor` (optional), `limit` (1-200, default 100) | `{contract_version, changes: [{statement_id, revision, status, document_type, source_sha256}], next_cursor}`: statements created or changed after `cursor`, oldest change first. No cursor means from the start. |
| `get_statement` | `statement_id`, `revision` (optional) | One revision, with its accounts but not their lines ([Per statement](#per-statement-one-revision)). The current revision unless one is named. |
| `get_statement_lines` | `statement_id`, `revision` (required), `account_ordinal`, `limit` (1-1,000, default 500), `offset` (default 0) | `{contract_version, lines, total, limit, offset, next_offset}`: one account's lines in `ordinal` order. `revision` is required, so every page comes from one revision. `next_offset` is null on the last page. A held or loan account returns an empty page with `total` 0. |

**Planned, not in version 1.**
- `get_statement_file`: the original PDF. It would return the document unmasked, so it is deferred to a unit of its own, with its own review of who may call it. Until then the operator reaches an original on the server's host, and no MCP tool returns a statement original's path or bytes.
- `search_statements`: the ledger reads through the feed. Adding either tool later is additive.

### Errors

A refused call is an error result whose message starts with a stable code, then `: ` and a message, so a client can branch on the code:

| Code | When |
|---|---|
| `invalid_cursor` | A cursor that doesn't parse, or whose prefix isn't one this server issues (anything but `s1:` today). The message never repeats the cursor. |
| `invalid_id` | A `statement_id` that isn't a UUID. The message never repeats it. |
| `not_found` | No such statement, no such revision of it, or no such account ordinal in that revision. |
| `invalid_argument` | `limit` or `offset` out of range. |
| `withheld` | A stored field of the revision failed the read check (see [Withheld](#withheld)). |

A well-formed cursor ahead of everything the server has issued (possible after a restore) isn't an error: it returns no changes, and `next_cursor` is the request's cursor.

### Withheld

Text is masked before it is stored. Before a tool returns a revision's data, it checks what it is about to return once more. `get_statement` checks each account's `institution`, every totals `section` (text; never null in a section total) and every totals amount, credits and debits (a decimal string that fits the contract's money: finite, at most two places, under 10^12). `get_statement_lines` checks the page's `description` and `section` values. Each text field is held to the mask's own rule (see [the read check](#masking)). If any field fails, the call is refused with `withheld`, and nothing of that revision is returned by that call. Nothing is re-masked or repaired at read, since the digest covers the stored values.

- **The client's safe default:** treat the whole revision as held for review. A refusal on any page of `get_statement_lines` holds the revision, so discard the pages already read from it. Never read the refusal as an empty statement or account, and never as a fault to retry in a tight loop.
- A refusal is recorded in the server's access log by statement ID, revision, account ordinal and field name, never the text. `receipt-index status` shows how many reads were withheld and when the latest was.
- A withheld row is a stored-data problem, for the operator to fix (re-extract the statement, or correct the stored rows): a fix makes a new revision, or a republish, which appears in the feed.

## Change feed

- The cursor is opaque to the client (today `"s1:<change_seq>"`). It survives restarts, redeploys and backup restores, and never goes backwards.
- `change_seq` is a statement's place in the feed. It takes a new value whenever the statement gets a new current revision, and when the operator republishes it. It is separate from `revision`, which numbers content.
- A statement appears once per change. A later change moves it, so the feed always shows its latest. Skipped revisions can still be fetched with `get_statement(statement_id, revision)`.
- Rejected documents are in the feed too, as revisions with no accounts, so a dropped file never vanishes silently. A model or schema error makes no revision, and so no feed entry.
- Every run is a new revision and a feed entry, even when its content is identical to the last one. The account digests then show that nothing changed.
- `revision` numbers increase for a statement and are never reused, even across a restore.
- Delivery is at least once: a client applies by `(statement_id, revision)`, so a repeat is harmless.
- Rows carry no cursor of their own. `next_cursor` is the only one: the position after the page's last row, or the request's cursor when no rows follow (`s1:0` with no cursor).
- **Restore.** A restore from backup can leave a statement's `change_seq` below a cursor a client already holds. The restore runbook's last step republishes every statement, giving each a fresh `change_seq` with its `revision` unchanged, so nothing restored stays hidden behind a cursor. A client never treats a lower revision as newer: it compares digests.

## Per statement (one revision)

| Field | Meaning |
|---|---|
| `contract_version` | `1` |
| `statement_id` | UUID, stable for the original's bytes within one database history (see Restore). The ledger keys statements on `source_sha256` and uses `statement_id` to fetch |
| `source_sha256` | Hex SHA-256 of the original PDF |
| `revision`, `extracted_at` | This revision's number and when it ran |
| `is_current`, `current_revision` | Whether this is the statement's current revision, and the current revision's number |
| `document_type` | `deposit`, `card`, `loan_statement`; or, for a rejected document, `loan_bill`, `tax_or_escrow_notice`, `not_statement`, `scan`, `unreadable` (not a PDF, one that needs a password to open, or one the reader can't open), `over_bounds` |
| `rejected_bound` | For `over_bounds`: `pages`, `size`, `text`, `memory`, `crash` or `accounts`; else null |
| `status` | `complete`, `incomplete`, `held` or `rejected` (below) |
| `hold_reason` | Why a `held` statement is held (below); null for every other status |
| `period_start`, `period_end` | The document's own printed period; null when not printed |
| `printed_account_count` | Accounts the document's own summary lists; null when it prints none |
| `pages` | Page count; null when the document couldn't be read |
| `extractor` | `{model, prompt_version, first_pass_version}`; null fields for a rejection before any model call |
| `masked_runs` | How many cuts the mask made in the text this revision stores (an upper bound on the numbers found). 0 for a rejection. Not in any digest |
| `accounts` | Below, in document order |

**Statement status.** Exactly one applies, tested in this order:

- `rejected`: not extracted; `document_type` says why. No accounts.
- `held`, with a `hold_reason`:
  - `no_accounts`: a deposit, card or loan statement whose first pass lists no account;
  - `dropped_account`: a listed account came back empty, or its pages lie past the document's;
  - `account_count_mismatch`: the printed account count disagrees with the accounts listed;
  - `no_usable_account`: none of the above, but no account is `clean` or `figures_only`.

  For the first three the account list itself can't be trusted, so every account is held and marked `held_by_statement`, whatever its own gates said.
- `incomplete`: no hold reason, at least one account held by its own gates, and at least one `clean` or `figures_only`. Clean accounts pass their lines.
- `complete`: no hold reason, and every account is `clean` or `figures_only`.

## Per account

| Field | Meaning |
|---|---|
| `account_id` | UUID, stable within a revision only |
| `ordinal` | 1-based position in the document |
| `match_key` | `{account_type, last4, occurrence}`, where `occurrence` counts accounts with the same type and `last4` in document order. Pairs this account with itself in another revision of the same statement. Accounts of one type with a null `last4` pair by document order alone |
| `institution` | As printed, normalised and masked |
| `last4` | Last four digits only, never a full number; null when none is printed |
| `account_type` | `deposit`, `card`, `loan` |
| `currency` | ISO 4217 code. A bare `$` with no other currency named reads as `USD`. Null when the statement gives no currency at all |
| `period_start`, `period_end` | The account's period: as printed for that account, else the document's. Null when neither is printed |
| `status` | `clean`, `held`, or `figures_only` (loans) |
| `held_by_statement` | True when the statement's hold reason holds this account; then `status` is `held` even if its own gates passed |
| `beginning_balance`, `ending_balance` | As printed, on the statement-balance basis; null when not printed |
| `section_totals` | `[{section, amount}]`: each printed section total as an unsigned magnitude, in printed order; `section` is never null here |
| `column_totals` | `[{section, credits, debits}]`: a table's printed column totals as unsigned magnitudes; `section` null for a table covering the whole account. Credits are the lines that raise a deposit balance or lower a card or loan balance |
| `line_order` | `oldest_first` or `newest_first`, as printed |
| `line_count` | Lines this account carries (0 for held and loan accounts) |
| `date_grace_days` | Days before `period_start` a posted date may fall and still pass: 7 for a card, 0 otherwise |
| `failure_class`, `difference`, `first_page`, `last_page` | For an account its own gates held: why, the ending balance less (beginning plus lines) where both are printed, and where to look. `failure_class` is null for a clean or figures-only account, and for one held only by the statement |
| `loan` | For a loan: `{principal_before, principal_after, principal_paid, interest, escrow, fees, advances}`, the first two on the statement-balance basis, the rest unsigned printed amounts; each null when not printed, and null when printed as a credit. `loan` itself is null for any other account type, and for a loan account whose extraction was cut off (`too_many_lines`), which read no figures |
| `content_digest` | Hex SHA-256 of the account's canonical form (below) |

**Held accounts carry no lines, by design.** A held account's balances, totals, difference and pages are given, so the owner can act on it, but never its lines: a held extraction is not a fact to import. The ledger doesn't read the missing lines as a fault.

**Failure classes**, for an account its own gates held, in the order they are tested (an account carries the first that applies):

- `empty_account`: a deposit or card account with no lines that isn't dormant (beginning and ending balances printed and equal), or a loan account with no printed figure at all;
- `no_beginning_balance`, `no_ending_balance`;
- `too_many_lines`: the extraction hit its output cap and was cut off;
- `undated`: printed dates carry no year and no period places them, or a yearless date fits more than one year of a period a year or longer;
- `does_not_foot`, with the difference;
- `date_out_of_period`;
- `balance_chain_break`;
- `totals_mismatch`.

## Per line (clean accounts only)

| Field | Meaning |
|---|---|
| `line_id` | UUID, stable within a revision only |
| `ordinal` | 1-based position in the printed order within the account, across pages. For a `newest_first` account, ordinal 1 is the newest line |
| `posted_date` | The date the line posted. When a line prints one date, it goes here |
| `transaction_date` | A second, transaction date where printed, else null. Not gated |
| `description` | Text as extracted, wrapped lines joined with one space, then masked |
| `amount` | Signed by its effect on the statement's printed balance |
| `running_balance` | As printed on the line, else null |
| `section` | The printed heading it sits under, normalised and masked; null when it sits under none |
| `page` | 1-based page |

## The canonical form and `content_digest`

`content_digest` is the lower-case hex SHA-256 of a JSON object of exactly these fields: `account_type`, `institution`, `last4`, `currency`, `period_start`, `period_end`, `status`, `held_by_statement`, `failure_class`, `difference`, `beginning_balance`, `ending_balance`, `section_totals`, `column_totals`, `line_order`, `loan`, and `lines`, each line with exactly `posted_date`, `transaction_date`, `description`, `amount`, `running_balance`, `section` and `page`, in `ordinal` order.

Serialise it with keys sorted, no insignificant whitespace, UTF-8 with no `\u` escapes beyond JSON's required ones, text as the contract gives it, money and dates as the contract's strings, integers as JSON numbers, booleans as `true`/`false`, and nulls kept. Ordinals and IDs are left out, so renumbering alone doesn't change a digest. In Python: `json.dumps(form, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")`.

Four properties to know:

- Totals are digested in printed order, so the same totals printed in another order give another digest.
- A held account's lines aren't stored or digested, so two held accounts with the same figures share a digest.
- A capped (`too_many_lines`) account has null `last4`, `institution` and `currency`, so its `match_key` shifts and can pair with another account whose `last4` is null.
- The digest covers masked text, so it follows the running Python's Unicode data, which decides which characters the mask joins.

### Worked example

A synthetic card account, as `get_statement` and `get_statement_lines` return it, reduced to the canonical form:

<!-- digest-example:form -->
```json
{
  "account_type": "card",
  "beginning_balance": "200.00",
  "column_totals": [],
  "currency": "USD",
  "difference": null,
  "ending_balance": "145.99",
  "failure_class": null,
  "held_by_statement": false,
  "institution": "Synthetic Card Co",
  "last4": "1234",
  "line_order": "oldest_first",
  "lines": [
    {
      "amount": "45.99",
      "description": "Synthetic Market #0412",
      "page": 1,
      "posted_date": "2027-01-05",
      "running_balance": null,
      "section": "purchases",
      "transaction_date": "2027-01-04"
    },
    {
      "amount": "-100.00",
      "description": "Payment, thank you",
      "page": 2,
      "posted_date": "2027-01-20",
      "running_balance": null,
      "section": "payments",
      "transaction_date": null
    }
  ],
  "loan": null,
  "period_end": "2027-01-31",
  "period_start": "2027-01-01",
  "section_totals": [
    {"amount": "100.00", "section": "payments"},
    {"amount": "45.99", "section": "purchases"}
  ],
  "status": "clean"
}
```

Serialised as above (one line, keys sorted, no spaces), its SHA-256 is:

<!-- digest-example:digest -->
```text
41726ebf66a985ad2b94cc7c6d1f1d8386d873049fb2474e9dacf32ce259f234
```

## Sign basis

Balances, lines and running balances on an account, and a loan's principal before and after, are on one basis: the statement's own printed balance. A line is positive when it raises that balance and negative when it lowers it. For a deposit account the balance is money held. For a card or loan it is money owed, so a purchase is positive and a payment negative. A card or loan balance printed as a credit comes through negative on the same basis and is never normalised. Section and column totals, and a loan's payment breakdown, are unsigned printed magnitudes.

To reach a debit-positive sign (like OFX `TRNAMT`), a client multiplies the signed figures of an account by +1 for `deposit` and −1 for `card` and `loan`. It doesn't flip unsigned magnitudes.

### Worked sign flip

The card account above, flipped by −1:

| Field | As served (statement basis) | Debit-positive |
|---|---|---|
| `beginning_balance` | `200.00` (owed) | `-200.00` |
| line 1, purchase | `45.99` | `-45.99` |
| line 2, payment | `-100.00` | `100.00` |
| `ending_balance` | `145.99` (owed) | `-145.99` |
| `section_totals` | `100.00`, `45.99` | unchanged (magnitudes) |

A deposit account's figures keep their sign: a deposit of `25.00` stays `25.00`.

## What a client can re-check from one revision

- Beginning plus the sum of the lines equals ending.
- The running-balance chain, walked in date order: ascending `ordinal` for `oldest_first`, descending for `newest_first`. Starting from the beginning balance, each printed running balance equals the previous printed balance (or the beginning balance) plus the amounts of the lines since it.
- Each section total equals the magnitude of the sum of the lines with that `section`; each column total equals the sum of the magnitudes of the lines on that side, within its section or the whole account.
- Every posted date lies between the account's `period_start − date_grace_days` and `period_end`, when the account has a period.
- `content_digest`, recomputed from the fields.

Shoebox runs the same checks, so a client doesn't have to trust its status.

## Masking

One deterministic function masks `description`, `section` and `institution` after normalisation and before the gates and the digest. In short:

- Digits of any script (fullwidth, circled, another script's, a number character worth a whole number) are folded to ASCII digits and counted. Unicode whitespace becomes one space and Unicode dashes become `-`. Invisible characters inside a number are dropped.
- Digit groups joined by spaces, punctuation, symbols and the letter forms of x are read as one span. A run of eight digits or more becomes its last four (`CARD 4111-1111-1111-1234 $12.50` becomes `CARD 1234 $12.50`). Amounts with cents and short dates beside a number are kept readable (`STORE #0412 11/09 $45.99` stays as printed).
- **The bound.** A span of 13 digits or more (the shortest card number) never shows more than 12 digits once masked: its last four and at most one kept amount marked by a currency. A backstop cuts any span that still shows more.

The mask is a pattern guard for the ways institutions print account and card numbers, not a proof that no number can pass. It also cuts some numbers that aren't account numbers: a reference number of 8 or more digits, a phone number or an undelimited date keeps only its last four. The plan lists every known limit and side effect. A client should expect at most the last four digits of a card or account number in any text field.

**The read check.** The server checks every free-text field again at read, by the mask's own run rule: no run of digit groups that the mask would cut (eight digits or more, however the groups are split by spaces, dashes or other joiners, once amounts with cents and short dates are set aside), no span showing more than 12 digits, and no eight digits in a row. Digits of any script count, so the check is stricter than the database's own check, which reads ASCII digits only and only eight in a row. A field that fails is [withheld](#withheld). Text the mask wrote always passes. Known limit, accepted: a kept amount of eight or more digits in a row, or one of eight digits written in another script, would be withheld too. This fails closed: an over-cautious refusal, never a leak.

`masked_runs` counts the cuts. The masked text is never logged.
