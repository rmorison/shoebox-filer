# CLI Reference

## Commands

- [`uv run receipt-index ingest`](#ingest) — Fetch and process receipts from email
- [`uv run receipt-index search`](#search) — Find receipts by metadata
- [`uv run receipt-index show`](#show) — View full details for a receipt
- [`uv run receipt-index edit`](#edit) — Correct a receipt's fields by hand
- [`uv run receipt-index history`](#history) — List the changes made to a receipt after extraction
- [`uv run receipt-index failures`](#failures) — List items that failed or were skipped
- [`uv run receipt-index retry`](#retry) — Requeue failed or skipped items for the next ingest
- [`uv run receipt-index reextract`](#reextract) — Re-extract existing receipts with the current prompt and model
- [`uv run receipt-index refund`](#refund) — Link refunds to their purchases, unlink them, and list them
- [`uv run receipt-index store`](#store) — Record the hash and location of each stored rendition
- [`uv run receipt-index mcp`](#mcp) — Serve the index read-only to MCP clients (agents)

All commands require a configured `.env` file (see [Getting Started](getting-started.md)).

The CLI is installed into a uv-managed virtual environment by `make setup`. Run commands with `uv run receipt-index`, or activate the venv first (`source .venv/bin/activate`) and use `receipt-index` directly. Examples below use the `uv run` form.

---

## Amounts

Each receipt has three amounts:

| Field | Meaning |
|---|---|
| `amount` | The order, invoice or statement total, as the document states it |
| `paid_amount` | What the transaction charged to the payer's card, bank account, wallet or cash; for a refund, what it returned to them. `null` when the document doesn't say; `0` when nothing was charged, such as on an unpaid invoice |
| `effective_amount` | `paid_amount` when it is known, otherwise `amount` |

`paid_amount` includes tax, shipping and tip, and is after discounts. It leaves out anything paid with gift cards, store credit, points or vouchers, since those never reach a bank or card statement; the description says when they were used. It is never assumed from `amount`. When the document shows a payment, such as a card line or an amount charged, that is the paid amount even when it equals the total; for cash, it is the amount charged, not the cash handed over. A payment method named without an amount, such as a card on file or a guarantee, doesn't show a payment. When the document shows no payment, `paid_amount` is `null`, not the total. A refund's `paid_amount` is what it returned to the payer's card or account, never negative, so a sum of effective amounts counts refunds as spending, not as money back: sum `signed_amount` instead (see [Kinds and totals](#kinds-and-totals)).

The search amount filters and the `Amount` column in text output use the effective amount. JSON output keeps `amount` as the total and adds `paid_amount` and `effective_amount`, so a script that reads `amount` keeps working; read `effective_amount` for what was paid.

Receipts ingested before the paid amount was added have `paid_amount: null`, so their effective amount is their `amount`. Their `amount` may be the payment rather than the total, because the earlier extraction prompt asked for "the total amount charged". When these receipts are re-extracted ([`reextract`](#reextract)), `paid_amount` is filled in and `amount` can be corrected to the total, so an `amount` already read from an old receipt can change.

**Field confidence.** `field_confidence` holds the extractor's own confidence, from 0 to 1, for each of `vendor`, `amount`, `paid_amount` and `date`; it is below 0.5 when a figure was inferred rather than printed. It is the model's estimate, not a measured probability: use it to choose which receipts to check first, not as a pass mark. It is empty (`{}`) for receipts ingested before it was recorded. Receipts extracted since the order number and payment were added also carry `order_number` and `payment` entries when those fields are known. An entry is removed when a person edits its field by hand, since it described the extracted value.

### Kinds and totals

Each receipt has a `kind`, which says which way its amount counts in a total:

| `kind` | What it is | `signed_amount` |
|---|---|---|
| `purchase` | A charge or payment for goods or services: a receipt, an order confirmation, an invoice or bill, a payment confirmation. A statement that bills a new charge for its own period (a utility, phone or subscription statement) is a purchase | `effective_amount` |
| `refund` | Money returned for an earlier purchase: a return, a cancellation, a reversal or a chargeback | minus `effective_amount` |
| `credit` | A credit that isn't money returned for one purchase: a credit memo, an account or statement credit, a goodwill credit, a rebate | minus `effective_amount` |
| `statement` | A card or bank statement, or a vendor statement that summarises orders already receipted. Its transactions are charged or receipted elsewhere | `0` |

**A sum of `signed_amount` is the net spend.** `amount`, `paid_amount` and `effective_amount` are never negative, whatever the kind, and keep their meanings above, so a script that sums `effective_amount` sees the same figures it always did (and still counts refunds as charges). A refund given wholly as store credit has a `paid_amount` of `0`, so it counts `0`.

Receipts ingested before kinds were extracted are all `purchase` until they are [re-extracted](#reextract): `reextract --refund-candidates` finds the likely refunds, credits and statements among them.

A refund can be linked to the purchase it returns money for. Its `refund_of` is then that purchase's ID, and the purchase shows `refunded` (the sum of its linked refunds' effective amounts) and `net_paid` (its effective amount less `refunded`). `refunded` and `net_paid` are `null` on any other kind. Ingest, re-extraction and `edit` link a refund by rule; [`refund`](#refund) links and unlinks by hand.

### Order number and payment

| Field | Meaning |
|---|---|
| `order_number` | The order, confirmation or invoice number as printed. Not unique: a confirmation, a shipment, a refund and an invoice can share one |
| `payment_method` | `card`, `bank`, `wallet`, `cash`, `transfer` or `other` |
| `payment_last4` | The last four digits of the card or account. No more is ever stored: a longer number, in this field or in the description, vendor or payer, is cut to its last four digits (or replaced by `[card number removed]` when the digits around it make the last four uncertain) |
| `payer` | The cardholder or bill-to name as printed on the receipt, not who forwarded the email |

Each is `null` when the receipt doesn't show it, and on receipts extracted before these fields were added, until they are [re-extracted](#reextract).

---

## ingest

Fetch new receipts from the configured sources (IMAP folders and Google Drive folders), extract metadata using an LLM, render PDFs, and store everything in the database and file store.

```bash
uv run receipt-index ingest [OPTIONS]
```

### Options

| Option | Description |
|---|---|
| `--dry-run` | Preview what would be processed without making any changes |
| `--source NAME` | Ingest from one named source only |
| `--limit N` | Process at most N new items per source |

### Examples

```bash
# Preview what will be ingested
uv run receipt-index ingest --dry-run

# Ingest the next 10 unprocessed messages
uv run receipt-index ingest --limit 10

# Ingest all unprocessed messages
uv run receipt-index ingest
```

### How It Works

1. Takes a lock on each source, so two runs never read the same source at once. A source that another process holds is skipped (see the exit codes below).
2. Reads each source from where the last run stopped, using the read position stored in `receipt.sync_state`:
   - **IMAP**: only messages with a UID above the stored one, and only their headers at first. A message whose ID was already processed is skipped before its body is downloaded. If the folder's `UIDVALIDITY` changes (the server rebuilt the folder), the run rescans the folder's headers once; message IDs, not UIDs, decide what is new, so nothing is processed twice.
   - **Google Drive**: the Drive changes feed since the stored page token, filtered to the configured folder. The first run lists the folder instead. A file changed less than `ingest.drive_settle_seconds` ago (default 60) is left for the next run, because scanner apps can upload a file's content after creating it. A file of a type that can't be ingested is recorded as skipped.
3. For each new item:
   - Records that work on it has started, so the run's position can move past it safely
   - Sends the email or file content to Claude Haiku to extract vendor, total, paid amount, date, description and per-field confidence (see [Amounts](#amounts))
   - If the LLM confidence is too low, the item is skipped (not a receipt). When the email's receipt is only a link to a hosted receipt ("View your receipt"), the skip is recorded as a link-only receipt, with the link, so that [`failures --status link-only`](#failures) lists it for the operator to save by hand. Ingest never follows the link
   - Renders the email as a PDF (using Playwright headless Chromium, with weasyprint fallback)
   - Stores the metadata in PostgreSQL and the PDF in the file store
4. Saves each source's new read position.
5. Runs the source's items that are due for a [retry](#retries), then reports processed/skipped/failed counts, retries included.

Re-running `ingest` is safe — previously processed items are automatically skipped. Deleting a source's row from `receipt.sync_state` is safe too: the next run rescans that source from the start and skips everything already processed.

### Retries

A failed item is retried by a later run. Each failure is recorded with a class:

| Class | Examples | Uses up an attempt | Retried after |
|---|---|---|---|
| `transient` | The LLM is overloaded or rate-limited, a server error, a dropped connection or timeout, a source that can't be reached | No | 30 seconds, doubling with each such failure in a row, up to 15 minutes |
| `config` | The LLM API key is rejected, the account's credits ran out, or the configured model doesn't exist (a retired or mistyped `llm.model`) | No | The same as `transient` |
| `content` | The message can't be parsed or rendered, the extraction fails validation, the model declines the request | Yes | `ingest.retry_backoff_seconds` for that attempt (5 minutes, then 30 minutes) |

The failure of attempt `ingest.retry_max_attempts` (default 3) is **terminal**: the item isn't retried automatically again, and only [`retry`](#retry) puts it back. Once `ingest.transient_item_max_seconds` (default six hours) have passed since an item's first transient failure (since it was last requeued), each further transient failure counts as an attempt, so the item can't hold up retries for ever. The clock is wall time since that first failure, not time spent failing: in an outage longer than six hours, the oldest items start using up attempts. Nothing is lost; `retry --transient` puts them back. A `config` failure never counts, since it isn't the item's fault.

A `transient` or `config` failure also stops the source for the rest of the run, since the next item would most likely fail the same way. An LLM outage during a large backfill therefore costs one failed item per run, not one per item.

Each run's retry pass picks up, for the sources it holds:

- failed items whose retry time has come;
- items requeued with [`retry`](#retry);
- items whose processing started but has no outcome, because the run handling them stopped mid-item. Each is recorded as a failure that uses up an attempt, then retried after the normal backoff. This includes such items left by runs before retries existed: the first `ingest` after the upgrade picks them up.

A retry finds the item again by its source ID: an IMAP message by its `Message-ID` in the watched folder, a Drive file by its file ID. An item recorded before records named their source, or under a source since renamed or removed from the configuration, is looked for in every configured source of its type. An item that can't be found fails as `content`. Messages without a `Message-ID` can't be found yet. Skipped items (not receipts) are never retried automatically.

Failures recorded before retries existed have no class, and start out terminal. Use [`retry`](#retry) to run them again.

```yaml
ingest:
  retry_max_attempts: 3                         # 1 turns automatic retries off
  retry_backoff_seconds: [300, 1800, 7200, 43200]  # needs at least retry_max_attempts - 1 entries
  transient_item_max_seconds: 21600
```

**Exit codes**:

| Code | Meaning |
|---|---|
| 0 | Success, even if some items were skipped |
| 1 | An item failed to process, a source couldn't be read, or a source stopped after a transient or config failure |
| 3 | A source was skipped because another process holds it (a running `ingest`). The other sources still run |

Useful for cron job monitoring.

**Note on `--dry-run`**: In dry-run mode, all items that would be processed are counted as "Skipped" in the output summary. A dry run reads only the new range and writes nothing: no records and no change to the stored read position. It still takes each source's lock while it reads, so a scheduled `ingest` that starts during a dry run exits with code 3 for that source.

---

## search

Query indexed receipts by vendor, amount, date, or any combination.

```bash
uv run receipt-index search [OPTIONS]
```

### Options

| Option | Description |
|---|---|
| `--vendor TEXT` | Filter by vendor name (case-insensitive substring match) |
| `--amount DECIMAL` | Filter by exact effective amount (the paid amount when known, otherwise the total; see [Amounts](#amounts)) |
| `--amount-min DECIMAL` | Filter by minimum effective amount (inclusive) |
| `--amount-max DECIMAL` | Filter by maximum effective amount (inclusive) |
| `--date-from YYYY-MM-DD` | Filter by start date (inclusive) |
| `--date-to YYYY-MM-DD` | Filter by end date (inclusive) |
| `--needs-review` | Only receipts still needing a look: `paid_amount` is `null`, or the latest extraction failed or fell below the confidence gate (see [reextract](#reextract)). A receipt whose paid amount was set by hand ([`edit`](#edit)) is left out |
| `--order-number TEXT` | Filter by order number (exact match) |
| `--last4 NNNN` | Filter by the payment card's or account's last four digits (exactly four digits) |
| `--payer TEXT` | Filter by payer name (case-insensitive substring match) |
| `--kind KIND` | Only receipts of this [kind](#kinds-and-totals): `purchase`, `refund`, `credit` or `statement`. Repeatable. Default: every kind |
| `--limit N` | Print at most N receipts (1 or more). Default: every match |
| `--offset N` | Skip the first N matches (0 or more). Default: 0 |
| `--output text\|json` | Output format (default: `text`) |

Note: `--amount` cannot be combined with `--amount-min`/`--amount-max`.

Results are ordered by date (newest first), then vendor name. With `--needs-review` they are ordered by paid-amount confidence, lowest first, with receipts that have none first of all. Ties are broken by ID, so the order is the same on every run.

**Paging.** `--limit` and `--offset` take a page of that order, as the MCP `search_receipts` tool does: `--limit 5` gives the first five, and `--limit 5 --offset 5` the next five. Without either option, `search` prints every match; unlike the MCP tool, which defaults to 50 and allows at most 200, `--limit` has no default and no upper bound. With either, the text output's footer places the page among all matches, for example `5 receipt(s) shown (6-10 of 37).`, and an offset past the last match prints `No receipts on this page (37 match).` The JSON output is the same envelope in either case (see [JSON Output](#json-output)). If an ingest commits between two runs, receipts can shift between pages; see the MCP [paging note](#mcp) for that. The same goes for `--needs-review` while you work through it: a receipt you fix with [`edit`](#edit) or [`reextract`](#reextract) can leave, join or move in the list, so run it again from offset 0 (or with `--limit` alone) rather than moving on to the next offset.

A link-only receipt isn't indexed, so `search` can't list it. With `--needs-review`, the text output ends with how many there are (only `--source` narrows that count; vendor, amount and date filters don't apply to them) and points to `failures --status link-only`. The JSON output doesn't count them.

### Examples

```bash
# Find all Amazon receipts
uv run receipt-index search --vendor amazon

# Find receipts between $50 and $200
uv run receipt-index search --amount-min 50 --amount-max 200

# Find receipts from January 2026
uv run receipt-index search --date-from 2026-01-01 --date-to 2026-01-31

# Combine filters: vendor + date range
uv run receipt-index search --vendor "whole foods" --date-from 2026-01-01

# Find an exact amount paid (useful for reconciliation)
uv run receipt-index search --amount 47.99

# Match a card statement line: the card's last four and the amount
uv run receipt-index search --last4 1234 --amount 47.99

# Every receipt for one order (confirmation, shipments, refund)
uv run receipt-index search --order-number 123-4567890

# Purchases only: leave out refunds, credits and statements
uv run receipt-index search --kind purchase --date-from 2026-01-01

# The five newest receipts, then the next five
uv run receipt-index search --limit 5
uv run receipt-index search --limit 5 --offset 5

# Output as JSON for scripting
uv run receipt-index search --vendor amazon --output json
```

### Text Output

The default text output shows a table. `Order` is the order number and `Last4` the payment card's last four digits, blank when not known. `Kind` is the [kind](#kinds-and-totals). `Amount` is the effective amount, unsigned, and `Total` is `amount`; they differ when, for example, part of an order was paid with a gift card:

```
ID                                     Date         Vendor               Order            Last4 Kind          Amount      Total
------------------------------------------------------------------------------------------------------------------------------
01942a3b-...                           2026-01-15   Amazon               123-4567890      1234  purchase       47.99      47.99
01942a3c-...                           2026-01-22   Acme Parking (U.S.)…                        purchase       30.00      50.00
01942a3d-...                           2026-01-29   Amazon               123-4567890      1234  refund         12.00      12.00

2 receipt(s) found.
```

### JSON Output

Use `--output json` for machine-readable output. It is the same page envelope as the MCP [`search_receipts`](#mcp) tool's, with the same field names and meanings:

- `receipts`: the receipts on the page, in the order above.
- `total`: how many receipts match the filters, on every page.
- `limit`: the `--limit` given, or `null` without one.
- `offset`: the `--offset` given, or `0`.
- `next_offset`: the `--offset` for the next page, or `null` when this page is the last. It is always `null` without `--limit`, since the page then runs to the end.

The total and the page are read from one snapshot, so they agree. Each receipt carries the same fields as an MCP receipt, except `file` (the PDF's path on the server and whether it exists), which only the MCP tool gives.

To read every page, start at `--offset 0` and pass `next_offset` as the next `--offset` until it is `null`.

```json
{
  "receipts": [
    {
      "id": "01942a3b-...",
      "vendor": "Amazon",
      "amount": "47.99",
      "paid_amount": "47.99",
      "effective_amount": "47.99",
      "kind": "purchase",
      "signed_amount": "47.99",
      "refund_of": null,
      "refunded": "12.00",
      "net_paid": "35.99",
      "currency": "USD",
      "receipt_date": "2026-01-15",
      "field_confidence": {"vendor": 0.98, "amount": 0.95, "paid_amount": 0.9, "date": 0.97,
                           "order_number": 0.95, "payment": 0.9},
      "manual_fields": [],
      "order_number": "123-4567890",
      "payment_method": "card",
      "payment_last4": "1234",
      "payer": "Jane Doe",
      "pdf_path": "2026/01/2026-01-15__amazon__47.99.pdf",
      ...
    }
  ],
  "total": 3,
  "limit": null,
  "offset": 0,
  "next_offset": null
}
```

---

## show

Display full details for a specific receipt by its ID.

```bash
uv run receipt-index show RECEIPT_ID [OPTIONS]
```

### Options

| Option | Description |
|---|---|
| `--output text\|json` | Output format (default: `text`) |

### Examples

```bash
# Show receipt details
uv run receipt-index show 01942a3b-1234-7def-8abc-567890abcdef

# Show as JSON
uv run receipt-index show 01942a3b-1234-7def-8abc-567890abcdef --output json
```

### Text Output

```
ID:           01942a3b-1234-7def-8abc-567890abcdef
Vendor:       Amazon
Amount:       47.99 USD (paid)
Paid:         47.99
Total:        47.99
Date:         2026-01-15
Description:  Wireless keyboard
Order:        123-4567890
Payment:      card ending 1234 (Jane Doe)
Kind:         purchase
Net paid:     35.99 (refunded 12.00)
Confidence:   0.95
Field Conf:   vendor 0.98, amount 0.95, paid_amount 0.90, date 0.97
PDF:          renditions/2026/01/2026-01-15__amazon__47.99.pdf
Rendition:    local:renditions/2026/01/2026-01-15__amazon__47.99.pdf
Source:       imap (msg-id-12345@mail.example.com)
Subject:      Your Amazon.com order has shipped
Sender:       ship-confirm@amazon.com
Email Date:   2026-01-15T10:30:00
```

`Amount` is the effective amount. When the paid amount isn't known, it shows the total and says so (`50.00 USD (total; paid amount not known)`), and `Paid` shows `— (not known)`. `Field Conf` is left out for receipts ingested before field confidence was recorded. A `Hand-edited:` line lists the fields a person has edited (`manual_fields` in JSON); re-extraction never writes them. `Order` and `Payment` are left out when the receipt doesn't show them; `Payment` shows whichever of the method, the last four digits and the payer are known. `Kind` shows a refund, credit or statement with what it counts in a total (`refund (counts -12.00)`); a linked refund adds `Refund of:` and its purchase's ID, and a purchase with linked refunds adds `Net paid:`.

The `PDF` path is relative to the store root, `store.path` in the config. `Rendition` is the same file's evidence-store reference: `local:` followed by that path.

New PDFs are stored as `renditions/{YYYY}/{MM}/{YYYY-MM-DD}__{vendor-slug}__{amount}.pdf`. Receipts stored before this layout keep their path, `{YYYY}/{MM}/{YYYY-MM-DD}__{vendor-slug}__{amount}.pdf`, and are read where they are: no file is moved. A new PDF never reuses a name that an older PDF in the same month already has, so it gets a numeric suffix (`_2`) instead.

#### Store configuration

```yaml
store:
  backend: local          # the only backend so far, and the default
  path: ./data/receipts   # the store root
```

`store.backend` selects the evidence store. Any value other than `local` is a config error.

---

## edit

Correct a receipt's fields by hand, or hand them back to re-extraction.

```bash
uv run receipt-index edit RECEIPT_ID [--paid-amount DECIMAL] [--vendor TEXT]
                         [--date YYYY-MM-DD] [--order-number TEXT] [--kind KIND]
                         [--reason TEXT] [--as NAME] [--output text|json]
uv run receipt-index edit RECEIPT_ID --clear-manual FIELD [--clear-manual FIELD ...]
                         [--reason TEXT] [--as NAME]
```

### Options

| Option | Description |
|---|---|
| `--paid-amount DECIMAL` | What was charged, or for a refund returned: 0 or more, at most two decimal places |
| `--vendor TEXT` | Vendor name |
| `--date YYYY-MM-DD` | Receipt date (`receipt_date`) |
| `--order-number TEXT` | Order number |
| `--kind KIND` | `purchase`, `refund`, `credit` or `statement` (see [Kinds and totals](#kinds-and-totals)). Refused while the receipt is in a refund link: a linked refund, or a purchase with linked refunds. Unlink it with [`refund unlink`](#refund) first |
| `--clear-manual FIELD` | Hand `FIELD` back to re-extraction: `paid_amount`, `vendor`, `date` (or `receipt_date`), `order_number` or `kind`. Repeatable. Can't be combined with the field options |
| `--reason TEXT` | Why, recorded with each change |
| `--as NAME` | Who made the change. Default: `annotations.actor` in the config, else `operator` |
| `--output text\|json` | Output format (default: `text`). JSON gives the receipt as it now is and the events written |

### What it does

Each field you set is written, added to `manual_fields` so that [re-extraction](#reextract) leaves it alone, and recorded in the receipt's [history](#history) with its old and new value and your reason. Setting a field to the value it already has still marks it hand-edited: you have confirmed it. The field's `field_confidence` entry is removed, since it was the extractor's confidence in the value you replaced. A field can't be set to empty; only re-extraction decides that a figure is unknown. A card number typed into `--vendor`, `--reason` or `--as` is cut to its last four digits, as at extraction.

Editing `--kind`, `--order-number`, `--vendor` or `--paid-amount` runs the refund linking rule (see [`refund`](#refund)): a receipt that becomes a refund may be linked to its purchase, and a purchase may get its waiting refunds, for example once its paid amount is known. Each link is printed and recorded in the refund's history. Editing a receipt that is already linked doesn't move its link: use `refund unlink` and `refund link`.

`--clear-manual` takes a field off `manual_fields` and records that in the history. The field keeps its value, but the next `reextract --overwrite` may replace it (fill-only runs leave it, since it isn't empty). It is refused, and nothing is written, when a field isn't hand-edited.

Other fields (the total, the currency, the description, the payment) can't be edited by hand yet.

The receipt is locked while the edit is written, so an edit and a re-extraction of the same receipt never interleave: a run that is waiting on the LLM sees the edit before it applies anything.

### Who made the change

Every change records an actor, the name given with `--as`, else the `annotations.actor` setting:

```yaml
annotations:
  actor: operator   # the default
```

The name is attribution, not authentication: the CLI records whatever name it is given, as a person.

### Examples

```bash
# The card statement shows 25.00 was charged
uv run receipt-index edit 01942a3b-1234-7def-8abc-567890abcdef \
    --paid-amount 25.00 --reason "card statement"

# Fix the order number and the date together
uv run receipt-index edit 01942a3b-1234-7def-8abc-567890abcdef \
    --order-number 123-4567890 --date 2026-01-14

# Let re-extraction decide the paid amount again
uv run receipt-index edit 01942a3b-1234-7def-8abc-567890abcdef --clear-manual paid_amount
```

---

## history

List every change made to a receipt after extraction, oldest first: hand edits and fields handed back to re-extraction. Re-extraction runs themselves are kept separately, in the extraction history.

```bash
uv run receipt-index history RECEIPT_ID [--output text|json]
```

### Text Output

One line per change: the time (UTC), the event type, who made it (and whether a person, an agent or a rule), what changed, and the reason:

```
2026-02-01 09:00:00Z  field_edited     operator (person)  paid_amount 30.00 → 25.00  — card statement
2026-02-02 09:00:00Z  manual_cleared   operator (person)  paid_amount handed back to re-extraction
```

`—` stands for an empty value. With `--output json` each event has its `event_type`, `payload` (for an edit: `field`, `old`, `new` and the removed `old_confidence`; for a cleared field: `field` and its `value`), `actor`, `actor_kind`, `reason` and `created_at`.

The history is append-only: nothing in the application can change or delete an event. Deleting a receipt by hand in SQL deletes its history with it.

---

## failures

List the items whose latest attempt failed, one line per item, newest first.

```bash
uv run receipt-index failures [OPTIONS]
```

| Option | Description |
|---|---|
| `--status STATUS` | `failed` (the default: `retrying` and `terminal` together), `retrying` (due again later), `terminal` (not retried automatically), `skipped` (judged not a receipt), `queued` (requeued with `retry`, waiting for the next ingest) or `link-only` (skipped because the receipt is only a link to a hosted receipt, which ingest doesn't follow; `skipped` lists these too) |
| `--since YYYY-MM-DD` | Only items whose latest record is from this date on |
| `--source NAME` | Only items from this source |
| `--limit N` | Print at most N items (1 or more). Default: every match |
| `--offset N` | Skip the first N matches (0 or more). Default: 0 |
| `--orphans` | Instead, list stored PDFs that a failed attempt left behind and no receipt references, whatever the item's state now (for example after a retry succeeded and stored a new one). Nothing is deleted. Takes none of the options above |
| `--output text\|json` | Output format (default: `text`) |

Items are ordered newest first, with ties broken by ID, and `--limit` and `--offset` page that order as they do for [`search`](#search). With either, the text output's footer places the page among all matches, for example `5 failed ingest(s) shown (6-10 of 37).`, and an offset past the last match prints `None on this page (37 match).` Each run reads one snapshot, but between two runs an ingest or a scheduled retry can record a newer attempt for an item. That item then moves to the top of the list, or leaves it, so pages can repeat or skip an item. To work through every item, run again from offset 0 rather than moving on to the next offset.

The text output shows the sender, subject, error, error class and next retry time (`terminal` when there is none). The JSON output is the same page envelope as the MCP [`list_failures`](#mcp) tool's, `{"failures": [...], "total": N, "limit": L, "offset": O, "next_offset": X}`, with the fields meaning what they mean for [`search`](#json-output): `limit` and `next_offset` are `null` without `--limit`. Each entry in `failures` is the item's latest `ingest_log` record, including `attempt`, `counts_attempt`, `next_retry_at`, `error_class`, `stored_ref` and, for a link-only receipt, `receipt_link`. With `--orphans`, the JSON output is a list of `{source_id, stored_ref, exists}`, with no envelope. The link comes from the email and may carry a personal token: the text output shows only its host, and logs never show more than that.

---

## retry

Requeue failed or skipped items, so that the next `ingest` runs them again. Without `--yes` it only previews.

```bash
uv run receipt-index retry [SOURCE_ID ...] [OPTIONS]
```

### Options

| Option | Description |
|---|---|
| `SOURCE_ID ...`, `--source-id ID` | Only these items (repeatable) |
| `--status failed\|skipped\|link-only` | Requeue items whose latest record has this status (default: `failed`). `link-only` selects the link-only receipts, which `skipped` also covers. Drive files skipped for an unsupported file type are never requeued |
| `--error-like TEXT` | Only items whose error contains this text, ignoring case |
| `--class transient\|content\|config` | Only items whose failure has this class |
| `--transient` | The same as `--class transient` |
| `--since YYYY-MM-DD` | Only items whose latest record is from this date on |
| `--source NAME` | Only items from this source |
| `--all` | Allow a selection that matches every item of the status |
| `--yes` | Requeue the items. Without it, nothing is written |
| `--output text\|json` | Output format (default: `text`) |

Filters combine with AND. A selection with nothing narrower than `--status` (and `--source`) is refused unless `--all` is given, so a typo can't requeue every failure.

### What it does

- **Without `--yes`** it lists the matching items (when, status, class, attempt, subject or source ID, error), and the count. Nothing is written.
- **With `--yes`** it selects again and requeues what it lists: failures recorded since an earlier preview are included. To requeue exactly the items you reviewed, pass them as source IDs. For each item it takes the item's lock and checks that its latest record hasn't changed since it was listed. An item another process holds, or that changed, is reported and left alone. Each requeued item gets a new `queued` record; the log is never edited or deleted. The item's attempt count starts again at 1.
- **Each requeue is recorded** in `receipt.retry_requests`: who ran it (the operating-system user and the database user), when, the filters, how many items matched and how many were requeued. The `queued` records point to it by `retry_request_id`.
- **Orphaned files.** A failure after the PDF was stored but before the database write leaves a file no receipt points to. The failure records the file (`stored_ref`), and `retry` lists each matched item's file that still exists and no receipt references. Failures recorded before this existed can't be checked; `retry` says how many there are, so they can be checked by hand. Nothing is deleted.

The next `ingest` runs the queued items, whether it runs from the schedule or by hand.

Exit codes: 0 on success, including a dry run; 1 when an item couldn't be requeued; 2 for a usage error or a refused selection.

### Examples

```bash
# What would requeue the transient failures since a date?
uv run receipt-index retry --transient --since 2026-10-01

# Requeue them
uv run receipt-index retry --transient --since 2026-10-01 --yes

# Requeue failures with a known environmental cause
uv run receipt-index retry --error-like "renderer" --yes

# Take a second look at skipped items from one source
uv run receipt-index retry --status skipped --source personal-email --all --yes

# Requeue the link-only receipts, for example after a prompt change
uv run receipt-index retry --status link-only --all --yes

# Who requeued what
psql "$DATABASE_URL" -c "SELECT requested_at, requested_by, filters, matched, requeued
                         FROM receipt.retry_requests ORDER BY requested_at DESC;"
```

---

## reextract

Re-extract existing receipts with the current extraction prompt and model, and record each run in the receipt's extraction history. This is how receipts ingested before a field existed get it, such as the paid amount.

```bash
uv run receipt-index reextract [SELECTION] [OPTIONS]
```

### Selection

At least one of these, `--limit` or `--all` is required, so that a bare command never re-extracts the whole catalogue. They combine with AND.

| Option | Description |
|---|---|
| `--date-from YYYY-MM-DD` | Receipts dated on or after this day |
| `--date-to YYYY-MM-DD` | Receipts dated on or before this day |
| `--source NAME` | Receipts from this configured source |
| `--id UUID` | This receipt (repeatable) |
| `--model-not MODEL` | Receipts whose latest extraction used a different model, or that have none |
| `--confidence-below X` | Receipts whose overall confidence is below `X`; or, as `FIELD=X` (`vendor`, `amount`, `paid_amount`, `date`), whose confidence for that field is below `X` or missing. A receipt where that field is hand-edited is left out, since a run can't change it |
| `--refund-candidates` | Receipts still marked `purchase`, their kind not hand-edited, whose email subject, description, vendor or file name has one of these whole words, in any case: `refund`, `refunds`, `refunded`, `credit`, `credits`, `credited`, `return`, `returned`, `reversal`, `reversals`, `reversed`, `chargeback`, `chargebacks`, `statement`, `statements`, `estatement`, `estatements`, `cancel`, `cancelled`, `canceled`, `cancellation` or `cancellations`. A word followed by `card` (`credit card`) doesn't count. The back-fill for [kind](#kinds-and-totals): run it with `--dry-run` first |
| `--all` | Every receipt |
| `--limit N` | At most `N` receipts, oldest first |

A receipt whose latest extraction already used the current prompt version **and** the configured model (`llm.model`) is left out, unless `--force` is given. So after a prompt change, or after changing the model, the same command picks up every receipt that is now stale, and an interrupted run can be started again with the same arguments. A run that failed doesn't count: the next run tries that receipt again. Each receipt is checked again once its lock is held, so two runs that overlap never re-extract the same receipt twice.

### Options

| Option | Description |
|---|---|
| `--overwrite` | Also replace fields an earlier extraction set (see below) |
| `--force` | Include receipts already at the current prompt version and model |
| `--input auto\|rendition` | `auto` (default): fetch the item from its source again, and use the stored PDF if it's gone. `rendition`: use the stored PDF only, with no network access, the cheapest |
| `--dry-run` | List the selection and the input each would use, with a token and cost estimate. Writes nothing and calls no model |
| `--output text\|json` | Output format (default: `text`) |

### What a run writes

By default a run fills only **empty** fields: a `null` `paid_amount`, `description`, order number or payment column, a missing `field_confidence` entry, or a `kind` still at its default (`purchase`) that no earlier run set. On a receipt extracted under the original prompt (ingested before extraction history was recorded, with an empty `field_confidence`), `amount` counts as empty too, so it is replaced by the total (see [Amounts](#amounts)). A gated or failed run doesn't change that; the first run that applies a result does.

With `--overwrite`, a run also replaces fields an earlier extraction set, and the overall `confidence`. On a receipt ingested before extraction history was recorded, every field counts as machine-extracted, before and after its first re-extraction: only `manual_fields` marks a hand edit. A field handed back with [`edit --clear-manual`](#edit) counts as machine-extracted too, even when ingest found it empty.

In both modes:

- a field a person has edited (`manual_fields`) is never written;
- a field's confidence entry changes with the field. One `payment` entry covers the three payment columns: it is written when they all hold the extracted values afterwards, and removed (and kept with the run) when a run changes some of them but leaves another that no longer matches, so it never describes values the receipt doesn't hold;
- a `null` result never clears a value;
- every change is printed as old and new, and kept with the run in the receipt's history (`receipt.extractions`: the full output, the fields applied and the values they replaced), so any run can be undone by hand.

If the new overall confidence is below 0.5, the run is recorded and nothing is applied (`gated`). The rendition's file name is never changed.

When a run changes a receipt's `kind`, order number, vendor, currency or paid amount, it runs the refund linking rule (see [`refund`](#refund)). A run never changes the kind of a receipt in a refund link (a linked refund, or a purchase with linked refunds): it keeps the stored kind and prints `kind: kept` with the extracted one (`kind_kept` in JSON). Unlink it with `refund unlink`, then edit or re-extract it.

### Inputs

Under `--input auto`, the source item is fetched again by its ID: IMAP searches the source's folder for the message's `Message-ID` and uses it only if exactly one message has that ID; Drive gets the file by its ID. A receipt whose source is no longer configured is looked up in each configured source of the same type. If the item is gone, or has no `Message-ID`, or the server refuses that one message, the stored PDF is used, with the stored subject and sender. If the source can't be reached at all, its receipts are reported as `unavailable`, nothing is written for them, and the source isn't contacted again in that run; the next run tries them again.

### Output

Each receipt gets one line: its status, the input used and the extracted paid amount with its confidence, whether or not it was applied. Changed fields follow, old and new. The statuses are `applied`, `unchanged`, `current` (another run finished it after this one selected it), `gated`, `failed`, `locked` (another process is working on the item) and `unavailable`. JSON output has the same per receipt (`changes`, `extracted_paid_amount`, `paid_amount_confidence`, `error`).

A run stops after 5 failures in a row, since the model or the store is then most likely down; the receipts it didn't reach are tried by the next run.

Exit codes: 0 when every receipt was applied, unchanged or current; 1 when any failed, was gated, locked or unavailable, or the run stopped early. [`search --needs-review`](#search) lists what is still missing a paid amount afterwards.

### Cost estimates

`--dry-run` estimates tokens from measured averages: the runs at the current prompt version and model once there are at least 10, otherwise successful ingests with that model in the last 30 days. With prices in the configuration it also estimates the cost:

```yaml
llm:
  model: claude-haiku-5-5
  price_per_mtok_input: 0.10    # optional, per million input tokens
  price_per_mtok_output: 0.50   # optional, per million output tokens
```

Prices change, so they are configuration, not code.

### Examples

```bash
# What would a run over 2025 cost?
uv run receipt-index reextract --date-from 2025-01-01 --date-to 2025-12-31 --dry-run

# Re-extract one source's receipts for a quarter
uv run receipt-index reextract --source personal-email --date-from 2026-01-01 --date-to 2026-03-31

# After changing llm.model: receipts last extracted by another model
uv run receipt-index reextract --model-not claude-haiku-5-5 --all --dry-run

# Look again at receipts whose paid amount was uncertain
uv run receipt-index reextract --confidence-below paid_amount=0.5 --force --all

# Back-fill kind: the likely refunds, credits and statements
uv run receipt-index reextract --refund-candidates --dry-run
uv run receipt-index reextract --refund-candidates
```

### Accuracy check

Before re-extracting the whole back catalogue after a prompt or model change, check a sample by hand:

1. Choose at least 30 receipts, including gift cards, invoices, statements and refunds.
2. Run `uv run receipt-index reextract --id ID1 --id ID2 ... --output json > sample.json`.
3. For each receipt, compare `extracted_paid_amount` (present whether or not it was applied) with what was actually charged, and mark it correct, wrong or null.
4. For each null, note whether the document names a card or account with no amount, such as an order confirmation with a card on file. Such a document keeps `paid_amount` null by design; the tally shows how often that happens.
5. Record the tally, and whether it meets the success criterion, before running over the back catalogue.

---

## refund

Link a refund to the purchase it returns money for, unlink it, and list refunds. A linked refund lowers its purchase's `net_paid` (see [Kinds and totals](#kinds-and-totals)).

```bash
uv run receipt-index refund link REFUND_ID PURCHASE_ID [--reason TEXT] [--as NAME]
uv run receipt-index refund unlink REFUND_ID [--reason TEXT] [--as NAME]
uv run receipt-index refund list [--unlinked] [--output text|json]
```

### The linking rule

Ingest, re-extraction and [`edit`](#edit) link a refund by rule. An unlinked refund is linked when **exactly one** purchase has the same order number, the same vendor (ignoring case), the same currency and a paid amount above zero. The paid-amount condition leaves out confirmation and shipping emails, and unpaid invoices, that share the order number but charged nothing. When several purchases match (an order charged on each shipment), or none does, the refund stays unlinked: `refund list --unlinked` shows it, and `refund link` links it by hand.

The rule runs at ingest, and whenever re-extraction or `edit` changes a field it matches on (kind, order number, vendor, currency or paid amount), so a purchase that arrives after its refund, or gets its paid amount later, still links it. A link already made isn't moved by a later change to either receipt: `refund unlink` and `refund link` do that. Each link is a `refund_linked` event in the refund's [history](#history), with the actor `rule`. The rule trusts the email: a message that claims to be a refund for a known order is linked, and lowers that purchase's net paid. `refund list` and `history` show every link it made.

Credits and statements are never linked.

### Linking by hand

`refund link` links an unlinked refund to a purchase in the same currency. It refuses, writing nothing, when the first receipt isn't a refund, the second isn't a purchase, the currencies differ, or the refund is already linked (unlink it first). `refund unlink` removes a refund's link. Both write one event with your reason, as the `--as` actor (default: `annotations.actor` in the config, else `operator`). If another change to the same receipts runs at the same moment, Postgres may stop one of them: the command then says nothing was saved, and you run it again.

**A refund you unlink stays unlinked.** The rule never links a refund that has been unlinked by hand; only `refund link` links it again.

### Examples

```bash
# Refunds the rule couldn't link
uv run receipt-index refund list --unlinked

# Link one by hand
uv run receipt-index refund link 01942a3d-1234-7def-8abc-567890abcdef 01942a3b-1234-7def-8abc-567890abcdef --reason "partial refund for the March order"

# Undo a link the rule made
uv run receipt-index refund unlink 01942a3d-1234-7def-8abc-567890abcdef --reason "refund for a different order"
```

---

## store

Record what the evidence store holds for each receipt.

```bash
uv run receipt-index store backfill-hashes [--dry-run]
```

### backfill-hashes

Ingest records the sha256 of each rendition it stores, and where the file lives. Receipts stored before that have no record. `store backfill-hashes` reads each of their rendition files, hashes it, and records three things: the stored file (one per distinct content, so two receipts with identical PDFs share it), the receipt's own local location, and the receipt's link to the file.

- **Run it once after upgrading** to the release that brings the provenance schema, as the application's login. The later move of the store to Google Drive reads these records.
- **It is safe to run again.** It reads only receipts with no record yet, so a second run finds nothing to do and changes nothing. A run cut short carries on from where it stopped.
- **A missing or unreadable file** is named by receipt ID and left unrecorded, and the command exits 1. Restore the file and run it again. So is a receipt whose file path another receipt's record already holds, which a re-run can't fix: it needs a look by hand.
- `--dry-run` reads and hashes the files and writes nothing. It reports the same unrecorded receipts the real run would.
- **Each receipt's `updated_at` changes once**, when its record is written: the receipt row gains the link to its file.

It prints how many renditions it recorded, and how many share bytes with another receipt. A catalogue of a few thousand receipts takes well under a minute.

---

## mcp

Run a read-only [MCP](https://modelcontextprotocol.io) server over stdio, so that agents can search receipts and fetch their PDFs. It uses the same search and repository code as the commands above. It never changes the database or the file store.

```bash
uv run receipt-index mcp [OPTIONS]
```

An MCP client normally starts this command itself (see [MCP client setup](#mcp-client-setup)). Stdout carries the MCP protocol; logs go to stderr.

### Options

| Option | Description |
|---|---|
| `--access-log PATH` | JSON-lines file that records every file fetch. Env: `RECEIPT_INDEX_MCP_ACCESS_LOG`. Default: `$XDG_STATE_HOME/receipt-index/mcp-access.jsonl` (`~/.local/state/...`) |
| `--max-content-bytes N` | Largest PDF returned as content (default: 10 MiB, 10485760 bytes) |

### Database role

The server connects with `RECEIPT_INDEX_MCP_DATABASE_URL`, or with `database.url` from the config when that is unset. **It refuses to start unless the role is read-only:** the role must hold `SELECT` on `receipt.receipts` and `receipt.ingest_log`, and no `INSERT`, `UPDATE`, `DELETE` or `TRUNCATE` on either (column-level grants count), nor `CREATE` on schema `receipt`. That includes privileges of any role it is a member of, even one whose privileges it doesn't inherit, since it could `SET ROLE` to that role. In devtest, use `receipt_index_dev_read`. For production, see [Server Deployment: Read-only MCP server](server-deployment.md#10-read-only-mcp-server).

Every connection is also opened read-only (`default_transaction_read_only`), and each tool call runs in one `REPEATABLE READ, READ ONLY` transaction, so a page and its `total` come from the same snapshot.

### Tools

All four tools are read-only. Amounts are decimal strings (`"42.99"`) in both directions, so an amount never passes through a float. Dates are `YYYY-MM-DD`. Each receipt carries `amount` (the total), `paid_amount` and `effective_amount`, with the meanings in [Amounts](#amounts), and the `amount` filters match the effective amount.

| Tool | Inputs | Returns |
|---|---|---|
| `search_receipts` | `vendor` (substring), `amount`, `amount_min`, `amount_max`, `date_from`, `date_to`, `source`, `order_number` (exact), `last4` (four digits), `payer` (substring), `kind` (a list of kinds; default every kind) (all optional); `limit` (1-200, default 50); `offset` (default 0) | `{receipts, total, limit, offset, next_offset}`. `amount` can't be combined with `amount_min`/`amount_max`. Each receipt carries the [order number and payment](#order-number-and-payment) fields, and `kind`, `signed_amount`, `refund_of`, `refunded` and `net_paid` ([Kinds and totals](#kinds-and-totals)). |
| `get_receipt` | `receipt_id` | One receipt. An unknown ID or a malformed UUID is a tool error. |
| `get_receipt_file` | `receipt_id`, `include_content` (default false) | `{receipt_id, path, size_bytes, mime_type, content_included, content_omitted_reason}`, plus the PDF as an embedded resource when content is asked for and fits the size limit |
| `list_failures` | `limit`, `offset`, `status` (`failed`, the default, or `link-only`) | `{failures, total, limit, offset, next_offset}`, as `failures --output json` gives for that status, newest first. A link-only entry carries `receipt_link`, an unverified link from the email that may carry a personal token; the consumer agent must not fetch it automatically |

Each receipt is the `show --output json` record plus `file: {path, exists}`. `path` is the PDF's absolute path on the host running the server. The `source` filter is an exact match on `source_name`. Results carry no file content; only `get_receipt_file` returns content.

**Paging.** Call again with `offset` set to `next_offset` until `next_offset` is `null`, then check that you received `total` results. Results are ordered newest first, then by vendor, then by ID. If an ingest commits between two calls, rows can shift between pages, and `total` changes with them. Re-run from offset 0, or narrow the date range, when you need a complete set.

**File content.** `content_omitted_reason` is `null` when the content is included, `"not_requested"` when `include_content` is false, and `"too_large"` when the PDF is over `--max-content-bytes`; the path is returned either way. A receipt whose PDF is missing from the store, or whose stored path points outside it, is a tool error.

### Access log

Every `get_receipt_file` call appends one JSON line to the access log, and the same line is logged at INFO on stderr:

```json
{"client": {"name": "example-agent", "version": "1.0"}, "content_included": true, "outcome": "ok", "pdf_path": "2026/01/2026-01-15__acme-parking-u-s-llc__12.50.pdf", "receipt_id": "01942a3b-1234-7def-8abc-567890abcdef", "size_bytes": 48213, "tool": "get_receipt_file", "ts": "2026-01-20T09:12:44.512408+00:00"}
```

`outcome` is one of `ok`, `too_large`, `not_found`, `file_missing`, `path_outside_store`, `invalid_id` or `error` (a database or file-system failure; nothing is returned). The `client` name and version are whatever the MCP client sent when it connected, or `null`. The server checks the log is writable at startup, and if a write fails later, it withholds the file rather than return it unlogged. A `receipt_id` that isn't a UUID is cut to its first 64 characters in the log line and the error. One kind of call writes no line: one the SDK rejects before the tool runs, because an argument is missing or has the wrong type. It gets a protocol error and no file.

### MCP client setup

Register the command with your MCP client. Most clients take a JSON entry like these; for Claude Code, `claude mcp add receipt-index -- <command> <args...>` does the same.

**Local (devtest or a laptop install).** `uv run --directory` runs from the checkout, so the CLI finds its `.env` and config there:

```json
{
  "mcpServers": {
    "receipt-index": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/receipt-index", "receipt-index", "mcp"],
      "env": {
        "RECEIPT_INDEX_MCP_DATABASE_URL": "postgresql://receipt_index_dev_read:<password>@localhost:15432/receipt_index_dev"
      }
    }
  }
}
```

**Production.** The client launches the server **as the account that holds the production credentials**: over SSH as the deploy user, inside the scheduler container, the same way `scripts/receipt-search` reaches it. The container already has `RECEIPT_INDEX_MCP_DATABASE_URL` and `RECEIPT_INDEX_MCP_ACCESS_LOG` from the one-time setup in [Server Deployment](server-deployment.md#10-read-only-mcp-server).

```json
{
  "mcpServers": {
    "receipt-index": {
      "command": "ssh",
      "args": ["receipt-server", "docker", "compose", "-p", "receipt-index",
               "exec", "-T", "scheduler", "receipt-index", "mcp"]
    }
  }
}
```

In production, `path` values are paths inside the container. A client on another machine should fetch PDFs with `include_content: true`.

**Treat results as untrusted input.** Vendor names, descriptions, subjects, senders and the PDFs all come from inbound email, so anyone who can send mail to the receipts inbox controls that text. A client should treat it as data, never as instructions, especially an agent that has other tools.

Out of scope for now: write tools, MCP over HTTP, and authentication.

---

## Example Workflows

### Initial Bulk Ingest

When first setting up, ingest all existing receipts:

```bash
# Preview first
uv run receipt-index ingest --dry-run

# Ingest in batches to monitor progress
uv run receipt-index ingest --limit 50
uv run receipt-index ingest --limit 50
# ... repeat until all processed

# Or ingest everything at once
uv run receipt-index ingest
```

### Reconciling a Transaction

You have a $47.99 charge from January 2026 and need the receipt:

```bash
# Search by amount and date
uv run receipt-index search --amount 47.99 --date-from 2026-01-01 --date-to 2026-01-31

# Get the full details including PDF location
uv run receipt-index show 01942a3b-1234-7def-8abc-567890abcdef
```

The PDF is under the store root (`store.path`), at the `PDF` path `show` prints, for example `renditions/2026/01/2026-01-15__amazon__47.99.pdf`.

### Finding All Receipts from a Vendor

```bash
uv run receipt-index search --vendor "whole foods" --date-from 2026-01-01 --date-to 2026-03-31
```

### Exporting Search Results

Pipe JSON output to other tools:

```bash
# Save search results to a file
uv run receipt-index search --vendor amazon --output json > amazon-receipts.json

# Count receipts per vendor with jq
uv run receipt-index search --date-from 2026-01-01 --output json | jq '.receipts | group_by(.vendor) | map({vendor: .[0].vendor, count: length})'

# Sum the amounts paid with jq (effective_amount: paid when known, else the total)
uv run receipt-index search --vendor amazon --output json | jq '[.receipts[].effective_amount | tonumber] | add'
```

### Weekly Ingestion

Run periodically to pick up new receipts:

```bash
uv run receipt-index ingest
```

Previously ingested messages are skipped automatically, so this is safe to run on a schedule.
