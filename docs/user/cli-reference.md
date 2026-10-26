# CLI Reference

## Commands

- [`uv run receipt-index ingest`](#ingest) — Fetch and process receipts from email
- [`uv run receipt-index search`](#search) — Find receipts by metadata
- [`uv run receipt-index show`](#show) — View full details for a receipt
- [`uv run receipt-index reextract`](#reextract) — Re-extract existing receipts with the current prompt and model
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

`paid_amount` includes tax, shipping and tip, and is after discounts. It leaves out anything paid with gift cards, store credit, points or vouchers, since those never reach a bank or card statement; the description says when they were used. It is never assumed from `amount`. When the document shows a payment, such as a card line or an amount charged, that is the paid amount even when it equals the total; for cash, it is the amount charged, not the cash handed over. A payment method named without an amount, such as a card on file or a guarantee, doesn't show a payment. When the document shows no payment, `paid_amount` is `null`, not the total. A refund's `paid_amount` is what it returned to the payer's card or account, never negative, so a sum of effective amounts counts refunds as spending, not as money back.

The search amount filters and the `Amount` column in text output use the effective amount. JSON output keeps `amount` as the total and adds `paid_amount` and `effective_amount`, so a script that reads `amount` keeps working; read `effective_amount` for what was paid.

Receipts ingested before the paid amount was added have `paid_amount: null`, so their effective amount is their `amount`. Their `amount` may be the payment rather than the total, because the earlier extraction prompt asked for "the total amount charged". When these receipts are re-extracted ([`reextract`](#reextract)), `paid_amount` is filled in and `amount` can be corrected to the total, so an `amount` already read from an old receipt can change.

**Field confidence.** `field_confidence` holds the extractor's own confidence, from 0 to 1, for each of `vendor`, `amount`, `paid_amount` and `date`; it is below 0.5 when a figure was inferred rather than printed. It is the model's estimate, not a measured probability: use it to choose which receipts to check first, not as a pass mark. It is empty (`{}`) for receipts ingested before it was recorded.

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
   - If the LLM confidence is too low, the item is skipped (not a receipt)
   - Renders the email as a PDF (using Playwright headless Chromium, with weasyprint fallback)
   - Stores the metadata in PostgreSQL and the PDF in the file store
4. Saves each source's new read position, and reports processed/skipped/failed counts

Re-running `ingest` is safe — previously processed items are automatically skipped. Deleting a source's row from `receipt.sync_state` is safe too: the next run rescans that source from the start and skips everything already processed.

At the start of each run, `ingest` logs a warning listing any item whose processing started more than an hour ago with no outcome recorded, which means the run handling it stopped mid-item. Such an item isn't retried automatically yet.

**Exit codes**:

| Code | Meaning |
|---|---|
| 0 | Success, even if some items were skipped |
| 1 | An item failed to process, or a source couldn't be read |
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
| `--needs-review` | Only receipts still needing a look: `paid_amount` is `null`, or the latest extraction failed or fell below the confidence gate (see [reextract](#reextract)) |
| `--output text\|json` | Output format (default: `text`) |

Note: `--amount` cannot be combined with `--amount-min`/`--amount-max`.

Results are ordered by date (newest first), then vendor name. With `--needs-review` they are ordered by paid-amount confidence, lowest first, with receipts that have none first of all.

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

# Output as JSON for scripting
uv run receipt-index search --vendor amazon --output json
```

### Text Output

The default text output shows a table. `Amount` is the effective amount and `Total` is `amount`; they differ when, for example, part of an order was paid with a gift card:

```
ID                                     Date         Vendor                   Amount      Total
---------------------------------------------------------------------------------------------
01942a3b-...                           2026-01-15   Amazon                    47.99      47.99
01942a3c-...                           2026-01-22   Acme Parking (U.S.)…      30.00      50.00

2 receipt(s) found.
```

### JSON Output

Use `--output json` for machine-readable output:

```json
[
  {
    "id": "01942a3b-...",
    "vendor": "Amazon",
    "amount": "47.99",
    "paid_amount": "47.99",
    "effective_amount": "47.99",
    "currency": "USD",
    "receipt_date": "2026-01-15",
    "field_confidence": {"vendor": 0.98, "amount": 0.95, "paid_amount": 0.9, "date": 0.97},
    "pdf_path": "2026/01/2026-01-15__amazon__47.99.pdf",
    ...
  }
]
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
Description:  Order #123-4567890 - Wireless keyboard
Confidence:   0.95
Field Conf:   vendor 0.98, amount 0.95, paid_amount 0.90, date 0.97
PDF:          renditions/2026/01/2026-01-15__amazon__47.99.pdf
Rendition:    local:renditions/2026/01/2026-01-15__amazon__47.99.pdf
Source:       imap (msg-id-12345@mail.example.com)
Subject:      Your Amazon.com order has shipped
Sender:       ship-confirm@amazon.com
Email Date:   2026-01-15T10:30:00
```

`Amount` is the effective amount. When the paid amount isn't known, it shows the total and says so (`50.00 USD (total; paid amount not known)`), and `Paid` shows `— (not known)`. `Field Conf` is left out for receipts ingested before field confidence was recorded. A `Hand-edited:` line lists the fields a person has edited (`manual_fields` in JSON); re-extraction never writes them.

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
| `--confidence-below X` | Receipts whose overall confidence is below `X`; or, as `FIELD=X` (`vendor`, `amount`, `paid_amount`, `date`), whose confidence for that field is below `X` or missing |
| `--all` | Every receipt |
| `--limit N` | At most `N` receipts, oldest first |

A receipt whose latest extraction already used the current prompt version **and** the configured model (`llm.model`) is left out, unless `--force` is given. So after a prompt change, or after changing the model, the same command picks up every receipt that is now stale, and an interrupted run can be started again with the same arguments. A run that failed doesn't count: the next run tries that receipt again.

### Options

| Option | Description |
|---|---|
| `--overwrite` | Also replace fields an earlier extraction set (see below) |
| `--force` | Include receipts already at the current prompt version and model |
| `--input auto\|rendition` | `auto` (default): fetch the item from its source again, and use the stored PDF if it's gone. `rendition`: use the stored PDF only, with no network access, the cheapest |
| `--dry-run` | List the selection and the input each would use, with a token and cost estimate. Writes nothing and calls no model |
| `--output text\|json` | Output format (default: `text`) |

### What a run writes

By default a run fills only **empty** fields: a `null` `paid_amount` or `description`, or a missing `field_confidence` entry. On a receipt extracted under the original prompt (no extraction history and an empty `field_confidence`), `amount` counts as empty too, so it is replaced by the total (see [Amounts](#amounts)).

With `--overwrite`, a run also replaces fields an earlier extraction set, and the overall `confidence`. A receipt with no extraction history counts as entirely machine-extracted.

In both modes:

- a field a person has edited (`manual_fields`) is never written;
- a field's confidence entry changes with the field;
- a `null` result never clears a value;
- every change is printed as old and new, and kept with the run in the receipt's history (`receipt.extractions`: the full output, the fields applied and the values they replaced), so any run can be undone by hand.

If the new overall confidence is below 0.5, the run is recorded and nothing is applied (`gated`). The rendition's file name is never changed.

### Inputs

Under `--input auto`, the source item is fetched again by its ID: IMAP searches the source's folder for the message's `Message-ID` and uses it only if exactly one message has that ID; Drive gets the file by its ID. A receipt whose source is no longer configured is looked up in each configured source of the same type. If the item is gone, or has no `Message-ID`, or the server refuses that one message, the stored PDF is used, with the stored subject and sender. If the source can't be reached at all, its receipts are reported as `unavailable`, nothing is written for them, and the source isn't contacted again in that run; the next run tries them again.

### Output

Each receipt gets one line: its status, the input used and the extracted paid amount with its confidence, whether or not it was applied. Changed fields follow, old and new. The statuses are `applied`, `unchanged`, `gated`, `failed`, `locked` (another process is working on the item) and `unavailable`. JSON output has the same per receipt (`changes`, `extracted_paid_amount`, `paid_amount_confidence`, `error`).

A run stops after 5 failures in a row, since the model or the store is then most likely down; the receipts it didn't reach are tried by the next run.

Exit codes: 0 when every receipt was applied or unchanged; 1 when any failed, was gated, locked or unavailable, or the run stopped early. [`search --needs-review`](#search) lists what is still missing a paid amount afterwards.

### Cost estimates

`--dry-run` estimates tokens from measured averages: the runs at the current prompt version and model once there are at least 10, otherwise successful ingests with that model in the last 30 days. With prices in the configuration it also estimates the cost:

```yaml
llm:
  model: claude-haiku-4-5-20251001
  price_per_mtok_input: 1.00    # optional, per million input tokens
  price_per_mtok_output: 5.00   # optional, per million output tokens
```

Prices change, so they are configuration, not code.

### Examples

```bash
# What would a run over 2025 cost?
uv run receipt-index reextract --date-from 2025-01-01 --date-to 2025-12-31 --dry-run

# Re-extract one source's receipts for a quarter
uv run receipt-index reextract --source personal-email --date-from 2026-01-01 --date-to 2026-03-31

# After changing llm.model: receipts last extracted by another model
uv run receipt-index reextract --model-not claude-haiku-4-5-20251001 --all --dry-run

# Look again at receipts whose paid amount was uncertain
uv run receipt-index reextract --confidence-below paid_amount=0.5 --force --all
```

### Accuracy check

Before re-extracting the whole back catalogue after a prompt or model change, check a sample by hand:

1. Choose at least 30 receipts, including gift cards, invoices, statements and refunds.
2. Run `uv run receipt-index reextract --id ID1 --id ID2 ... --output json > sample.json`.
3. For each receipt, compare `extracted_paid_amount` (present whether or not it was applied) with what was actually charged, and mark it correct, wrong or null.
4. For each null, note whether the document names a card or account with no amount, such as an order confirmation with a card on file. Such a document keeps `paid_amount` null by design; the tally shows how often that happens.
5. Record the tally, and whether it meets the success criterion, before running over the back catalogue.

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
| `search_receipts` | `vendor` (substring), `amount`, `amount_min`, `amount_max`, `date_from`, `date_to`, `source` (all optional); `limit` (1-200, default 50); `offset` (default 0) | `{receipts, total, limit, offset, next_offset}`. `amount` can't be combined with `amount_min`/`amount_max`. |
| `get_receipt` | `receipt_id` | One receipt. An unknown ID or a malformed UUID is a tool error. |
| `get_receipt_file` | `receipt_id`, `include_content` (default false) | `{receipt_id, path, size_bytes, mime_type, content_included, content_omitted_reason}`, plus the PDF as an embedded resource when content is asked for and fits the size limit |
| `list_failures` | `limit`, `offset` | `{failures, total, limit, offset, next_offset}`: the entries `failures --output json` gives, newest first |

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
uv run receipt-index search --date-from 2026-01-01 --output json | jq 'group_by(.vendor) | map({vendor: .[0].vendor, count: length})'

# Sum the amounts paid with jq (effective_amount: paid when known, else the total)
uv run receipt-index search --vendor amazon --output json | jq '[.[].effective_amount | tonumber] | add'
```

### Weekly Ingestion

Run periodically to pick up new receipts:

```bash
uv run receipt-index ingest
```

Previously ingested messages are skipped automatically, so this is safe to run on a schedule.
