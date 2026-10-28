# Technical Design: Receipt Data Model

**Version:** 0.1 — Draft
**Date:** 2026-10-25
**Product Spec:** [receipt-search-index-spec.md](../../product/features/receipt-search-index-spec.md)
**Related designs:** [Evidence Store](evidence-store.md) (files, hashes and provenance), [Always-On Ingest](always-on-ingest.md) (sources, archive folders and backfill)

---

## Context

A receipt row today holds a vendor, one amount, a currency, a date, a description, an overall confidence score, the rendered PDF's path, the email headers and the source it came from. Search filters on a vendor substring, an amount or amount range, a date range and the source.

That was enough to find a receipt for a bank line by hand. The main consumer now is an agent that builds claim packets and reconciliation packets from receipts, and it needs more from each record:

- **The amount actually paid.** The extractor is told to take "the total amount charged", and in practice often takes the order or statement total. Claim schedules and bank reconciliation need what was paid: after discounts, gift cards and store credit, and as charged to the card or account.
- **The items bought.** Some uses match purchases item by item, by description or product ID.
- **Who paid, and how.** In a household several people and cards are involved, and receipts are forwarded between members.
- **The order or invoice number**, which ties confirmations, shipments, refunds and invoices together.
- **Refunds and returns**, linked to the purchase they reverse, so that a net figure is right.
- **Tags** that map to accounting tracking codes, set by a person or by an agent, with a record of who changed what.
- **Links** to records in other systems, such as an accounting transaction or an inventory item.
- **Text search** over more than a vendor substring.
- **Per-field confidence**, so that a consumer knows which figures to check.

The consumer values completeness and correct paid amounts over latency and over extraction cost. Old receipts matter as much as new ones, so every new field needs a way to reach the back catalogue.

### Key technical challenges

1. **Paid versus total.** The two differ on a minority of receipts, and the document doesn't always say which is which. The extractor needs a clear rule, and the record needs both figures.
2. **Back catalogue.** Existing receipts were extracted with the old prompt. Re-extraction has to be cheap to run, safe to repeat, and must not undo a person's corrections.
3. **History of changes.** Tags and links are set by more than one kind of actor. The record of who set what must not be editable by the actors it records.
4. **The split with the evidence store.** Email headers, source IDs and file paths are moving to provenance and file tables. Text search and re-extraction read from them, wherever they end up.

### Architectural decisions

- [ADR-0011: Paid Amount Alongside the Total, and Line Items in Their Own Table](../adr/0011-paid-amount-and-line-item-storage.md)
- [ADR-0012: Receipt Annotations: A Controlled Tag Vocabulary and an Append-Only History](../adr/0012-tag-governance.md)

### Standards

- [Python Standards](https://github.com/rmorison/engineering-standards/blob/main/code/python-standards.md)
- [Database Standards](https://github.com/rmorison/engineering-standards/blob/main/code/database-standards.md)

---

## Approach

### Solution overview

1. **Keep `amount` as the total and add `paid_amount`.** Search, `show` and JSON output use the paid amount where it is known, and the total where it isn't. This change ships first and on its own.
2. **Record every extraction run**, and add a `reextract` command that brings existing receipts up to the current prompt. A field a person has edited is never overwritten.
3. **Add the receipt's own fields** in small steps: order number and payment, line items, refunds, text search, tags, links. Each step is one migration and one implementation ticket.
4. **Keep one append-only history per receipt** for every change made after extraction: hand edits, tag changes, links and refund links. A separate database role limits what an agent can change.

### Schema split with the evidence store

The two designs divide the `receipt` schema as follows.

| This design owns | The [evidence-store design](evidence-store.md) owns |
|---|---|
| The receipt's own fields: vendor, amounts, date, currency, description, confidence, order number, payment, kind and refund link | Stored files: originals, attachments and renditions, with their hashes and storage references |
| `line_items`, `tags`, `receipt_tags`, `external_links`, `receipt_events`, `extractions`, the `receipt_net` view | Provenance records (one per source item), which take over `source_id`, `source_type`, `source_name`, `file_name` and the `email_*` columns |
| The search document | Today's `pdf_path`, and the rendition reference that replaces it |
| | Duplicate detection (its `duplicate_candidates` table references provenance records, not receipts), and linking and unlinking duplicates |

Where they meet:

- **Text search reads the email subject** from wherever the evidence-store design puts it. Until that design's migration lands, it is `receipts.email_subject`.
- **Refunds are not duplicates.** A refund shares a vendor and an order number with its purchase. Duplicate detection excludes pairs whose `kind` differs.
- **Re-extraction reads originals** from the evidence store once they exist, and falls back as described under [Re-extraction](#re-extraction).
- **Merging duplicates.** How a merge works is the evidence-store design's decision. A merge moves provenance records and deletes nothing: the receipt it empties keeps its history, extractions, rendition and `refund_of`, and gets `merged_into_id` (the receipt that remains) and `merged_at`, columns owned by the evidence-store design. A merge is refused when the emptied receipt has tags, external links or hand-edited fields. This design treats a merged receipt as described in [Merged receipts](#merged-receipts).
- **Which document held the receipt.** The evidence-store design treats an email's receipt document as certain only when the extraction names it, so the extraction reports it; see [Output model](#output-model).
- **Renaming renditions.** The rendition's file name includes the vendor and amount. Renaming it after a re-extraction changes either is the evidence-store design's business. This design only changes the fields.

### Alternatives considered

- **Replace `amount` with the paid amount.** Rows extracted before the change would then mean two different things under one name, with no way to tell which. Rejected; see ADR-0011.
- **Line items as a JSONB array on the receipt.** Simpler to write, but item-by-item matching, SKU lookup and text search over descriptions all want rows. Rejected; see ADR-0011.
- **Free-form tags.** Fast to start, but tags then drift ("parking", "Parking", "car-parking") and can't map cleanly to tracking codes. Rejected; see ADR-0012.
- **One history table per kind of change** (tag events, link events, edit events). More tables for the same columns, and "what happened to this receipt" becomes a union. Rejected in favour of one `receipt_events` table.
- **A separate extraction call for line items.** It would keep the main call's output short, but it pays for the input twice. Rejected: one call returns all fields.
- **Maintain the search document in application code.** Every write path (ingest, re-extract, edit, migrations) would have to remember it. Rejected in favour of triggers.

---

## Design

### 1. Paid amount

#### Meaning of each amount

| Column | Meaning |
|---|---|
| `amount` (existing) | The order, invoice or statement total as the document states it |
| `paid_amount` (new, nullable) | What this transaction charged to the payer's payment methods (card, bank account, wallet or cash), or, for a refund, what it returned to them |

The extraction rules for `paid_amount`:

- It includes tax, shipping and tip, and is after discounts and coupons.
- It excludes what was paid with gift cards, store credit, points or vouchers, since those never reach a bank or card statement. The description says when they were used.
- For an invoice or statement that shows a payment, it is the payment, not the balance or the invoice total.
- For an order charged in parts (for example on each shipment), it is what this document says was charged.
- A pre-authorization or hold is not a payment.
- When the document shows that nothing has been paid (an unpaid invoice), it is `0`.
- For a refund, it is what was returned to the payer's payment methods, excluding any part returned as store credit, a gift card or points.
- When the document doesn't say what was paid or returned, it is `null`.

`paid_amount` is never inferred from `amount`. A `null` means "not known", and `0` means "nothing was charged", so an unpaid invoice doesn't show up in reconciliation at its full total.

#### The effective amount

Search filters, sort order, the text output and the JSON output's `effective_amount` all use `COALESCE(paid_amount, amount)`. The JSON output keeps `amount` and adds `paid_amount` and `effective_amount`, so a consumer that reads `amount` today keeps working. The approach had output use the effective amount; it gets its own key instead, because putting it under `amount` would make that key mean the payment on some rows and the total on others, which is the mixing ADR-0011 rejects.

#### Existing totals

The old prompt asked for "the total amount charged", so an existing `amount` is sometimes the total and sometimes the payment, and nothing records which. The first re-extraction of a receipt extracted under the old prompt therefore replaces `amount` as well, so that after the back-catalogue run `amount` means the total on every row. A receipt was extracted under the old prompt when it has no ingest `extractions` row (it was ingested before the history existed), an empty `field_confidence`, which ticket 1's prompt always fills, and no run has applied `amount`. A gated or failed run changes none of these, so it doesn't end that treatment. Each replaced value is printed and kept in the run's `extractions` row.

This differs from the signed-off approach, which kept every existing `amount`. The effect for consumers: a figure already read from `amount` for an old receipt can change after the back-catalogue run. A consumer that has copied amounts elsewhere should re-read them, or compare against the run's printed changes.

The file-store naming (`{date}__{vendor}__{amount}.pdf`) is left as it is. New file names, and renaming, belong to the evidence-store design.

#### Migration

```sql
ALTER TABLE receipt.receipts
    ADD COLUMN paid_amount      NUMERIC(12,2),
    ADD COLUMN field_confidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD CONSTRAINT ck_receipts_paid_amount_non_negative
        CHECK (paid_amount IS NULL OR paid_amount >= 0);

CREATE INDEX IF NOT EXISTS idx_receipts_effective_amount
    ON receipt.receipts ((COALESCE(paid_amount, amount)));

ALTER TABLE receipt.ingest_log
    ADD COLUMN paid_amount NUMERIC(12,2);

GRANT INSERT (paid_amount, field_confidence), UPDATE (paid_amount, field_confidence)
    ON receipt.receipts TO receipt_index_dev_write;
GRANT SELECT (paid_amount, field_confidence)
    ON receipt.receipts TO receipt_index_dev_read;
GRANT INSERT (paid_amount) ON receipt.ingest_log TO receipt_index_dev_write;
GRANT SELECT (paid_amount) ON receipt.ingest_log TO receipt_index_dev_read;
```

Existing rows get `paid_amount = NULL` and keep working through the fallback. Re-extraction fills them in. Every migration in this design is mirrored in `_run_migrations()` in `tests/integration/conftest.py`, and takes the next free number in `db/migrations/receipt/` when it is written.

### 2. Extraction

#### Output model

One extraction call returns every field. The output model grows ticket by ticket; at the end it is:

```python
class LineItem(BaseModel):
    description: str = Field(min_length=1)
    quantity: Decimal | None = Field(default=None, gt=0)
    unit_price: Decimal | None = None
    line_total: Decimal | None = None   # negative for a discount line
    sku: str | None = None              # SKU, part or product number


class Payment(BaseModel):
    method: Literal["card", "bank", "wallet", "cash", "transfer", "other"] | None = None
    last4: str | None = Field(default=None, pattern=r"^[0-9]{4}$")
    payer: str | None = None            # cardholder or bill-to name as printed


class FieldConfidence(BaseModel):
    vendor: float = Field(ge=0.0, le=1.0)
    amount: float = Field(ge=0.0, le=1.0)
    paid_amount: float = Field(ge=0.0, le=1.0)
    date: float = Field(ge=0.0, le=1.0)
    order_number: float | None = Field(default=None, ge=0.0, le=1.0)
    payment: float | None = Field(default=None, ge=0.0, le=1.0)
    line_items: float | None = Field(default=None, ge=0.0, le=1.0)
    kind: float | None = Field(default=None, ge=0.0, le=1.0)


class ReceiptMetadata(BaseModel):
    vendor: str = Field(min_length=1)
    amount: Decimal = Field(ge=0)                    # order or statement total
    paid_amount: Decimal | None = Field(default=None, ge=0)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    date: date
    description: str | None = None
    order_number: str | None = None
    payment: Payment | None = None
    kind: Literal["purchase", "refund"] = "purchase"
    receipt_document: str | None = None             # attachment file name, or "body"
    line_items: list[LineItem] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)        # is this a receipt at all
    field_confidence: FieldConfidence
```

Ticket 1 adds only `paid_amount` and `field_confidence` (with its first four entries). Ticket 2 adds `receipt_document`, which names the attachment that held the receipt (its file name exactly as the message's MIME headers give it, so it can be matched to the stored attachment; the extractor's prompt shows an ordinary name unchanged, but cleans one with control characters, a run of three or more dashes, or over 255 characters, so such a name won't match), or `body` when the receipt was in the email itself; it is `null` when the extractor can't tell, for example when a lone PDF holds only terms and conditions. It is kept in the extraction record, where the evidence-store design's duplicate rules read it. Later tickets add the rest.

#### Prompt changes

Both system prompts (email and document) change in the same way:

- `amount` is described as the order, invoice or statement total, and `paid_amount` by the rules in [Meaning of each amount](#meaning-of-each-amount). Today's "for multi-item orders, use the total amount" line goes.
- Each field gets a confidence from 0 to 1, with an instruction to go below 0.5 when a figure is inferred rather than printed.
- Later tickets add order number, payment, kind and line items. The prompt asks for at most 100 line items. The model doesn't enforce the cap, so that a longer list never fails validation and loses the whole receipt: the pipeline keeps the first 100 and adds a note to the description.

The extraction module gets a constant, `EXTRACTION_PROMPT_VERSION`, which is raised whenever the prompt or the output model changes. Each extraction run records it.

#### Confidence

The overall `confidence` keeps its current job: below 0.5 the item is skipped as "not a receipt". The per-field values are stored in `receipts.field_confidence` as a JSON object keyed by field name, shown by `show`, and returned in JSON output.

These values are the model's own estimate, not a measured probability. They are good for ordering a review ("check the lowest paid-amount confidences first") and not for a hard threshold. The success criteria include a hand-checked sample, which also shows how far the values can be trusted.

#### Cost per receipt

The configured model is Claude Haiku 4.5, at list prices (October 2026) of $1 per million input tokens and $5 per million output tokens. The devtest database holds no real receipts, so these figures are estimates; ticket 1 measures them.

| | Input tokens | Output tokens | Cost |
|---|---|---|---|
| Today (estimate) | about 2,500 | about 150 | about $0.003 |
| Ticket 1 (paid amount, field confidence) | about 2,700 | about 200 | about $0.004 |
| All fields, 5 line items | about 2,900 | about 400 | about $0.005 |
| All fields, 30 line items | about 2,900 | about 1,300 | about $0.009 |

Input grows by the longer prompt only. Output grows by about 35 tokens per line item, which is most of the added cost. A scanned document sent to vision costs more on input (on the order of 1,500 to 3,000 tokens a page) whatever the fields. At these rates re-extracting 1,000 receipts costs roughly $5 to $10.

Ticket 1 reports the measured averages before and after, from the `ingest_log` token columns that already exist:

```sql
SELECT llm_model,
       count(*)                       AS runs,
       round(avg(llm_input_tokens))   AS avg_input,
       round(avg(llm_output_tokens))  AS avg_output
FROM receipt.ingest_log
WHERE status = 'success' AND llm_input_tokens IS NOT NULL
  AND created_at >= now() - interval '30 days'
GROUP BY llm_model;
```

Changing the model (for example to a newer, cheaper Haiku) is a configuration change with its own accuracy check, and isn't part of this design.

### 3. Re-extraction

#### Extraction history

```sql
CREATE TABLE IF NOT EXISTS receipt.extractions (
    id                    UUID NOT NULL DEFAULT uuidv7(),
    receipt_id            UUID NOT NULL,
    origin                TEXT NOT NULL,
    input_kind            TEXT NOT NULL,
    input_file_id         UUID,
    prompt_version        INTEGER NOT NULL,
    llm_model             TEXT NOT NULL,
    output                JSONB NOT NULL,
    applied_fields        TEXT[] NOT NULL DEFAULT '{}',
    replaced              JSONB NOT NULL DEFAULT '{}'::jsonb,
    llm_input_tokens      INTEGER,
    llm_output_tokens     INTEGER,
    llm_cache_read_tokens INTEGER,
    llm_requests          INTEGER,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_extractions PRIMARY KEY (id),
    CONSTRAINT fk_extractions_receipt FOREIGN KEY (receipt_id)
        REFERENCES receipt.receipts (id) ON DELETE CASCADE,
    CONSTRAINT ck_extractions_origin CHECK (origin IN ('ingest', 'reextract')),
    CONSTRAINT ck_extractions_input_kind
        CHECK (input_kind IN ('original', 'source', 'rendition'))
);

CREATE INDEX IF NOT EXISTS idx_extractions_receipt_created
    ON receipt.extractions (receipt_id, created_at DESC);

ALTER TABLE receipt.receipts
    ADD COLUMN manual_fields TEXT[] NOT NULL DEFAULT '{}';
```

- `input_file_id` records which stored file the run read, when it read one from the evidence store (an original or the rendition), so a run shows what it worked from. Its foreign key to `receipt.stored_files` is added by whichever of the two designs' migrations lands second.
- `output` is the whole validated output model, so a run can be inspected or compared later.
- `applied_fields` lists the fields this run actually wrote to the receipt, and `replaced` holds the values those fields had before, so any run can be undone by hand.
- `manual_fields` lists the fields a person has edited. Re-extraction never writes them.
- Ingest writes an `extractions` row with `origin = 'ingest'` for every receipt it creates. `ingest_log` keeps its token columns, since it also covers runs that produced no receipt.
- The application roles get `INSERT` and `SELECT` on `extractions` and no `UPDATE` or `DELETE`. Grants alone aren't enough in production, where the application connects as the role that owns the tables, so the table is also guarded by a trigger; see [Keeping history append-only](#keeping-history-append-only).

Existing receipts have no `extractions` row, and count as prompt version 0. That includes receipts ingested between tickets 1 and 2. They already have a paid amount and a filled `field_confidence`, so they don't count as extracted under the old prompt and their `amount` isn't replaced; re-extracting them costs a little and, in the default fill-only mode, changes nothing they already have.

#### The command

```
receipt-index reextract [--date-from YYYY-MM-DD] [--date-to YYYY-MM-DD]
                        [--source NAME] [--id UUID ...]
                        [--model-not MODEL] [--confidence-below [FIELD=]X]
                        [--all] [--overwrite] [--force]
                        [--input auto|rendition] [--limit N]
                        [--dry-run] [--output text|json]
```

- **Selection.** By receipt date range, source and IDs, combined with AND. `--model-not` keeps receipts whose latest extraction used another model, and `--confidence-below` those whose overall confidence, or one field's, is below a value. A selector, `--limit` or `--all` is required, so that a bare command never re-extracts the whole catalogue. (The sign-off dropped a `--stale` option: the default already selects only stale receipts.)
- **Repeat runs.** A receipt whose latest extraction already has the current prompt version and the configured model is skipped unless `--force` is given, so a model change makes receipts stale as a prompt change does. An interrupted run can be started again with the same arguments. A failed run is recorded with `output` holding only its `error`, and doesn't count as the latest extraction for this rule, so the next run retries it.
- **Applying a result.** By default a run writes only fields that are empty. A field is empty when it is `NULL`, when a receipt has no line items, or when it holds only its column default (`kind`, and each entry of `field_confidence`) and no earlier `extractions` row lists it in `applied_fields`. On a receipt extracted under the old prompt, `amount` counts as empty too (see [Existing totals](#existing-totals)). With `--overwrite` a run also replaces fields it extracted earlier, and the overall confidence; on a receipt ingested before the history existed (no ingest row), every field counts as machine-extracted, before and after its first re-extraction, since only `manual_fields` marks a hand edit. A field's `field_confidence` entry is written with the field. In both modes, fields in `manual_fields` are left alone, and every field whose value changes is printed as old and new and kept in `replaced`.
- **Refund links.** When a run sets `kind = 'refund'` or an order number, it runs the same linking rule as ingest; see [Refunds and returns](#6-refunds-and-returns). A run never changes the kind of a refund that is linked to a purchase (the constraint `ck_receipts_refund_of_kind` would refuse it); it lists the receipt instead, and a person unlinks it with `refund unlink` before editing it.
- **Confidence gate.** If the new overall confidence is below 0.5, the run records the extraction and applies nothing, and the summary lists the receipt.
- **What still needs review.** `search --needs-review` lists receipts whose `paid_amount` is `NULL`, or whose latest extraction applied nothing because of the confidence gate or a failure, lowest paid-amount confidence first. Both are read from the run's `output` (its `confidence`, or an `error`), with no extra column; a run that applied nothing because nothing was empty doesn't count. It is the list success criterion 6 is checked against, and it survives after the run's own output has gone.
- **Transactions.** Each receipt is its own transaction: the `extractions` row, the receipt update and any line items commit together. A failure is logged and the run moves on.
- **Dry run.** `--dry-run` lists the selected receipts, the input each would use and the estimated tokens, from the average of recent extractions. If the configuration gives token prices (`llm.price_per_mtok_input` and `llm.price_per_mtok_output`, both optional, beside the model they price), it prints an estimated cost too. Prices change, so they are configuration rather than code.

#### What it reads

The input for each receipt, in order of preference (`--input auto`):

1. **The original**, from the evidence store, once that design's originals exist.
2. **The source**, fetched again by its ID. Each adapter gains a `fetch_by_id(source_id) -> RawReceipt | None`: IMAP searches by the `Message-ID` header in the folders the [always-on ingest design](always-on-ingest.md)'s backfill searches: the watched folder, the archive folder and `needs-attention`. Drive gets the file by ID, which still works after the file has moved into the evidence store's `originals/`. A `Message-ID` is set by the sender and isn't guaranteed unique, so the IMAP search quotes it as an IMAP string and returns `None` unless exactly one message matches. Receipts stored under the adapter's hash fallback (messages with no `Message-ID`) can't be searched for and also return `None`.
3. **The rendition**, the stored PDF, through the same text-first, vision-fallback path that Drive documents use today, with the stored subject and sender prepended.

`--input rendition` goes straight to the third, which needs no network access and is the cheapest. The extraction row's `input_kind` records which was used.

#### Running it on the back catalogue

Re-extraction costs money each time the prompt version changes. The suggested runs are:

- after ticket 2, so that the back catalogue gets paid amounts as soon as possible, since the consumer needs them;
- once more after tickets 3, 4 and 5 have all merged, which picks up order numbers, payment, line items and refund kinds together, rather than one run per ticket.

### 4. Order number and payment

```sql
ALTER TABLE receipt.receipts
    ADD COLUMN order_number   TEXT,
    ADD COLUMN payment_method TEXT,
    ADD COLUMN payment_last4  TEXT,
    ADD COLUMN payer          TEXT,
    ADD CONSTRAINT ck_receipts_payment_method CHECK (payment_method IS NULL OR
        payment_method IN ('card', 'bank', 'wallet', 'cash', 'transfer', 'other')),
    ADD CONSTRAINT ck_receipts_payment_last4
        CHECK (payment_last4 IS NULL OR payment_last4 ~ '^[0-9]{4}$');

CREATE INDEX IF NOT EXISTS idx_receipts_order_number
    ON receipt.receipts (order_number);
CREATE INDEX IF NOT EXISTS idx_receipts_payment_last4
    ON receipt.receipts (payment_last4);
```

- **Order number** is not unique: a confirmation, a shipment, a refund and an invoice can share one.
- **Payer** is the name printed on the receipt, as the cardholder or bill-to. Who forwarded the email is provenance and belongs to the evidence store. Matching a card to a household member is a later step: a `payers` map in the YAML configuration, from last four to a name, applied at display time. Configuration, not a table, since it is a handful of entries the operator edits.
- **Last four only.** No full card or account number is ever stored. If a document shows more, the extractor returns only the last four digits.
- Search gains `--order-number` (exact), `--last4` and `--payer` (substring).

#### Editing a receipt by hand

The same ticket adds `receipt-index edit <id> --paid-amount 12.34 [--vendor ...] [--date ...] [--order-number ...] [--reason TEXT]`. Each edited field is added to `manual_fields` and recorded in `receipt_events` (below) with its old and new value. `edit --clear-manual FIELD` hands a field back to re-extraction. Line items can't be edited by hand in this design; re-extraction replaces them.

#### The receipt history

This ticket also creates the history table that edits, tags, links and refund links all write to:

```sql
CREATE TABLE IF NOT EXISTS receipt.receipt_events (
    id          UUID NOT NULL DEFAULT uuidv7(),
    receipt_id  UUID NOT NULL,
    event_type  TEXT NOT NULL,
    payload     JSONB NOT NULL,
    actor       TEXT NOT NULL,
    actor_kind  TEXT NOT NULL,
    reason      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_receipt_events PRIMARY KEY (id),
    CONSTRAINT fk_receipt_events_receipt FOREIGN KEY (receipt_id)
        REFERENCES receipt.receipts (id) ON DELETE CASCADE,
    CONSTRAINT ck_receipt_events_type CHECK (event_type IN (
        'field_edited', 'manual_cleared',
        'tag_added', 'tag_removed',
        'link_added', 'link_removed',
        'refund_linked', 'refund_unlinked')),
    CONSTRAINT ck_receipt_events_actor_kind
        CHECK (actor_kind IN ('person', 'agent', 'rule'))
);

CREATE INDEX IF NOT EXISTS idx_receipt_events_receipt_created
    ON receipt.receipt_events (receipt_id, created_at);
```

Application roles get `INSERT` and `SELECT` only. `receipt-index history <id>` prints the events. ADR-0012 records the decision.

#### Keeping history append-only

Grants alone don't make `receipt_events` and `extractions` append-only in production. There the application connects as `receipt_index`, which owns the database and every table in it (see `deploy/db-init/01-create-roles.sh`), and a table's owner can update and delete whatever the grants say. In devtest the application connects as `receipt_index_dev_write`, which doesn't own anything, so a test there would pass and hide the gap.

So the migration that creates each history table also adds a guard trigger, which applies to the owner too:

```sql
CREATE OR REPLACE FUNCTION receipt.forbid_history_change()
RETURNS TRIGGER AS $$
BEGIN
    -- Allow the cascade from deleting the receipt itself: by then the parent
    -- row is gone. Refuse every other update or delete.
    IF TG_OP = 'DELETE' AND NOT EXISTS (
        SELECT 1 FROM receipt.receipts WHERE id = OLD.receipt_id
    ) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION '% on %.% is not allowed: history is append-only',
        TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER trg_receipt_events_append_only
    BEFORE UPDATE OR DELETE ON receipt.receipt_events
    FOR EACH ROW EXECUTE FUNCTION receipt.forbid_history_change();

CREATE OR REPLACE TRIGGER trg_receipt_events_no_truncate
    BEFORE TRUNCATE ON receipt.receipt_events
    FOR EACH STATEMENT EXECUTE FUNCTION receipt.forbid_history_change();
```

The same two triggers go on `extractions`. On a truncate `TG_OP` is `TRUNCATE`, so the function raises without reading `OLD`. An integration test shows that deleting a receipt still cascades, and that an update or delete by the owning role is refused.

This stops application code. It doesn't stop the owning role from dropping the trigger, which takes a deliberate schema change. Closing that last gap means connecting the application as a role that owns nothing, with `receipt_index` kept for migrations only. That changes the production deployment, so it is a question for the operator, listed under [Open questions](#open-questions).

#### Deleting a receipt

Deleting a receipt deletes its history with it (`ON DELETE CASCADE`), which is the one exception to the rule above. The CLI has no command that deletes a receipt, and merging duplicates deletes nothing, so this only happens through SQL run by hand. Deleting a purchase that way also sets its refunds' `refund_of` to `NULL` with no event, since a cascade writes none; that is part of the same exception.

### 5. Line items

```sql
CREATE TABLE IF NOT EXISTS receipt.line_items (
    id           UUID NOT NULL DEFAULT uuidv7(),
    receipt_id   UUID NOT NULL,
    position     SMALLINT NOT NULL,
    description  TEXT NOT NULL,
    quantity     NUMERIC(12,3),
    unit_price   NUMERIC(12,2),
    line_total   NUMERIC(12,2),
    sku          TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_line_items PRIMARY KEY (id),
    CONSTRAINT fk_line_items_receipt FOREIGN KEY (receipt_id)
        REFERENCES receipt.receipts (id) ON DELETE CASCADE,
    CONSTRAINT uq_line_items_receipt_position UNIQUE (receipt_id, position),
    CONSTRAINT ck_line_items_quantity_positive CHECK (quantity IS NULL OR quantity > 0)
);

CREATE INDEX IF NOT EXISTS idx_line_items_sku ON receipt.line_items (sku);
CREATE INDEX IF NOT EXISTS idx_line_items_description
    ON receipt.line_items USING gin (description gin_trgm_ops);
```

- `position` keeps the document's order.
- A discount line has a negative `line_total`. Tax, shipping and tip are not line items; they are part of the paid amount.
- Quantities, unit prices and totals are stored as extracted, with no check that they multiply out, since rounding and bundles make such a check fail on real receipts. A consumer that needs consistency can test for it.
- Writing line items replaces all of a receipt's rows in one transaction.
- Search gains `--sku` (exact) and `--item` (substring over line-item descriptions). `show` and JSON output list the items.

### 6. Refunds and returns

```sql
ALTER TABLE receipt.receipts
    ADD COLUMN kind      TEXT NOT NULL DEFAULT 'purchase',
    ADD COLUMN refund_of UUID,
    ADD CONSTRAINT ck_receipts_kind CHECK (kind IN ('purchase', 'refund')),
    ADD CONSTRAINT fk_receipts_refund_of FOREIGN KEY (refund_of)
        REFERENCES receipt.receipts (id) ON DELETE SET NULL,
    ADD CONSTRAINT ck_receipts_refund_of_kind
        CHECK (refund_of IS NULL OR kind = 'refund'),
    ADD CONSTRAINT ck_receipts_refund_of_not_self
        CHECK (refund_of IS NULL OR refund_of <> id);

CREATE INDEX IF NOT EXISTS idx_receipts_refund_of ON receipt.receipts (refund_of);

CREATE OR REPLACE VIEW receipt.receipt_net AS
WITH live_refunds AS (
    -- A refund counts against the purchase that remains after any merge,
    -- and not at all if the refund was itself merged away.
    SELECT r.id,
           COALESCE(r.paid_amount, r.amount)  AS refund_amount,
           COALESCE(t.merged_into_id, t.id)   AS purchase_id
    FROM receipt.receipts r
    JOIN receipt.receipts t ON t.id = r.refund_of
    WHERE r.kind = 'refund' AND r.merged_into_id IS NULL
)
SELECT p.id                                          AS receipt_id,
       COALESCE(p.paid_amount, p.amount)             AS paid,
       COALESCE(SUM(lr.refund_amount), 0)            AS refunded,
       COALESCE(p.paid_amount, p.amount)
         - COALESCE(SUM(lr.refund_amount), 0)        AS net_paid,
       COUNT(lr.id)                                  AS refund_count
FROM receipt.receipts p
LEFT JOIN live_refunds lr ON lr.purchase_id = p.id
WHERE p.kind = 'purchase' AND p.merged_into_id IS NULL
GROUP BY p.id;
```

- **A refund is its own receipt**, with `kind = 'refund'`, its own date and its own evidence. Its amounts are stored as positive numbers; `kind` gives the direction. A bank feed shows a refund as a credit, and the consumer maps the sign. Its `paid_amount` is what went back to the payer's card or account, so a refund given as store credit doesn't lower the net paid.
- **Net paid is computed, not stored**, so it can't fall out of step with the refunds. A purchase with several partial refunds gets the sum.
- **The linking rule.** An unlinked refund is linked to a purchase when exactly one purchase has the same order number, the same vendor (compared case-insensitively), the same currency and a paid amount above zero (`paid_amount > 0`). The paid-amount condition leaves out confirmation and shipping emails, and unpaid invoices, that charged nothing, which share an order number with the purchase that did. If several purchases still match (an order charged on each shipment), or none does, the refund stays unlinked.
- **When the rule runs.** Whenever an unlinked refund gains its kind or order number (at ingest, re-extraction or a hand edit), and whenever a purchase is ingested or re-extracted with an order number that matches an unlinked refund. Each link it makes is recorded as a `refund_linked` event with `actor_kind = 'rule'`.
- **The rule trusts the email.** An email in the watched folder that claims to be a refund for a known order is linked, and lowers that purchase's net paid. The event, `refund list` and `history` make every automatic link visible, and `refund unlink` undoes it.
- **Linking by hand.** `receipt-index refund link <refund-id> <purchase-id>` and `refund unlink <refund-id>`, both recorded. `refund list --unlinked` shows refunds with no purchase. Linking checks that the currencies match.
- Search gains `--kind purchase|refund` (default both), and JSON output adds `kind`, `refund_of` and, for purchases, `refunded` and `net_paid`.
- Duplicate detection in the evidence store excludes pairs whose `kind` differs.

### 7. Text search

```sql
ALTER TABLE receipt.receipts
    ADD COLUMN search_document TSVECTOR NOT NULL DEFAULT ''::tsvector;

CREATE INDEX IF NOT EXISTS idx_receipts_search_document
    ON receipt.receipts USING gin (search_document);
```

The document is built by one SQL function, `receipt.build_search_document(receipt_id)`, from:

| Source | Weight | Text search configuration |
|---|---|---|
| Vendor, order number | A | `simple` (names and codes aren't stemmed) |
| Line-item descriptions and SKUs | B | `english` and `simple` |
| Description | B | `english` |
| Email subject | C | `english` |

Triggers keep it current. They are `AFTER` triggers that update the receipt's `search_document`, since `build_search_document(receipt_id)` reads the stored rows: on `receipts` (insert, and update of the columns it reads, which don't include `search_document`, so the update doesn't fire it again), and on `line_items` (insert, update and delete, which refresh the parent). The function and the trigger functions are `SECURITY DEFINER`, owned by the migration role, with `SET search_path = receipt, pg_catalog`. A trigger otherwise runs with the privileges of whoever fired it, and the `_write` role has no `UPDATE` on `search_document`, so without this every write of line items would fail in devtest. When the evidence-store design moves the subject to its provenance table, the function reads the subject of every provenance record of the receipt, and a trigger on that table joins these. Whichever of the two migrations lands second makes that change. The migration that adds the column fills it for every existing row.

Search gains `--text "query"`, parsed with `websearch_to_tsquery` in both configurations and combined with OR. With `--text`, results are ordered by `ts_rank` and then by date. The trigram index on vendor stays, and `--vendor` keeps working as a substring match.

### 8. Tags

```sql
CREATE TABLE IF NOT EXISTS receipt.tags (
    id             UUID NOT NULL DEFAULT uuidv7(),
    name           TEXT NOT NULL,
    description    TEXT,
    tracking_code  TEXT,
    active         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_tags PRIMARY KEY (id),
    CONSTRAINT uq_tags_name UNIQUE (name),
    CONSTRAINT ck_tags_name_format
        CHECK (name ~ '^[a-z0-9][a-z0-9-]*(:[a-z0-9][a-z0-9-]*)?$')
);

CREATE TABLE IF NOT EXISTS receipt.receipt_tags (
    receipt_id   UUID NOT NULL,
    tag_id       UUID NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_receipt_tags PRIMARY KEY (receipt_id, tag_id),
    CONSTRAINT fk_receipt_tags_receipt FOREIGN KEY (receipt_id)
        REFERENCES receipt.receipts (id) ON DELETE CASCADE,
    CONSTRAINT fk_receipt_tags_tag FOREIGN KEY (tag_id)
        REFERENCES receipt.tags (id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_receipt_tags_tag ON receipt.receipt_tags (tag_id);
```

- **A controlled vocabulary.** A tag must be defined before it is applied. A name is lower case, optionally with one namespace (`category:value`), and can carry the accounting system's tracking code. Tags are deactivated, never deleted, so old history keeps its meaning. A deactivated tag can't be applied, and stays on the receipts that have it.
- **Who may do what.**

  | Action | Person (CLI) | Agent (future MCP write tools) |
  |---|---|---|
  | Define, rename, deactivate a tag | Yes | No |
  | Apply or remove a defined, active tag | Yes | Yes |
  | Add or remove an external link | Yes | Yes |
  | Edit an extracted field, link a refund | Yes | No |
  | Read tags, links and history | Yes | Yes |

- **How changes are recorded.** Triggers on `receipt_tags` and `external_links` write the `tag_added`, `tag_removed`, `link_added` and `link_removed` events, after each insert and delete, in the same transaction. Application code can't skip an event or write one that doesn't match the change. The trigger functions are `SECURITY DEFINER`, owned by the migration role, with a fixed `search_path`. They read the actor and the reason from transaction-local settings (`SET LOCAL receipt_index.actor = ...`, `receipt_index.actor_kind`, `receipt_index.reason`), and refuse the change if no actor is set. They write nothing when the parent receipt no longer exists, the same test the history guard uses, so the cascade from deleting a receipt still works. The trigger on `receipt_tags` also refuses to apply a deactivated tag, so the database, not only the CLI, keeps the vocabulary rule. A tag event's payload holds the tag's ID, name and tracking code at the time, so a later rename doesn't change what old events mean. Defining, renaming or deactivating a tag is rare and isn't per receipt; `tags.updated_at` records it.
- **Who the actor is.** The CLI sets the name in its configuration (`annotations.actor`, default `operator`), or the one given with `--as`, with `actor_kind = 'person'`. An agent tool sets the MCP client's name. This is attribution, not authentication: the tool runs for one household.
- **How it is enforced.** Agent write tools connect through a role that can do only what the table above allows:
  - The migrations grant to a new `NOLOGIN` role, `receipt_index_dev_annotate`, following the `_dev_` names the existing migrations use as grant targets. It gets `USAGE` on the schema, `SELECT` on the receipt tables and the vocabulary, and `INSERT` and `DELETE` on `receipt_tags` and `external_links`. It gets nothing on `receipt_events`: the triggers write the events for it. It can't define tags, edit receipt fields or touch history.
  - When the session's login role is a member of `receipt_index_dev_annotate`, the triggers record `actor_kind = 'agent'` whatever the setting says, so an agent can't pass itself off as a person. Membership is checked through `pg_auth_members`, directly or through other roles, and not with `pg_has_role`, which is true for every superuser; devtest and the integration conftest connect as one, and would otherwise record every change as an agent's.
  - `receipt_index_dev_annotate` is never granted to `receipt_index`, the production application login, nor to the `_dev_all`, `_dev_write` or `_dev_read` roles. Otherwise every CLI change in production would be recorded as an agent's.
  - A migration can't create a role: in production it runs as `receipt_index`, which has no `CREATEROLE`, and the init scripts run only on an empty data directory. So ticket 7 adds an idempotent SQL step, run once by the database superuser before `migrate-up`, that creates `receipt_index_dev_annotate` if it doesn't exist and grants it `CONNECT`. The same block goes into `db/init/01-create-roles.sql` and `deploy/db-init/01-create-roles.sh` for new databases, outside the loop in the production script that grants each `_dev_` role to `receipt_index`, and the step is documented in the server deployment guide and the devtest notes.
  - The login that agent tools use (for example `receipt_index_annotate`, a member of `receipt_index_dev_annotate` only, owning nothing, with its own password and its own connection string, never `DATABASE_URL`) comes with the MCP write tools, which are the first thing to use it. That ticket adds a post-deploy check, run as that login, that an update of `receipt.receipts` and an insert into `receipt.tags` are refused.
  - The CLI keeps the `_write` role.
- **Commands.** `tags define NAME [--tracking-code CODE] [--description TEXT]`, `tags rename OLD NEW`, `tags deactivate NAME`, `tags list`, `tag <id> add|remove NAME [--reason TEXT]`.
- **Search.** `--tag NAME` (repeatable, all must match) and `--untagged`, which lists receipts still waiting to be coded.
- ADR-0012 records the decision.

### 9. External links

```sql
CREATE TABLE IF NOT EXISTS receipt.external_links (
    id               UUID NOT NULL DEFAULT uuidv7(),
    receipt_id       UUID NOT NULL,
    system           TEXT NOT NULL,
    external_id      TEXT NOT NULL,
    url              TEXT,
    note             TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_external_links PRIMARY KEY (id),
    CONSTRAINT fk_external_links_receipt FOREIGN KEY (receipt_id)
        REFERENCES receipt.receipts (id) ON DELETE CASCADE,
    CONSTRAINT uq_external_links_receipt_system_id
        UNIQUE (receipt_id, system, external_id),
    CONSTRAINT ck_external_links_system_format CHECK (system ~ '^[a-z0-9][a-z0-9-]*$')
);

CREATE INDEX IF NOT EXISTS idx_external_links_system_id
    ON receipt.external_links (system, external_id);
```

- `system` is free text in a fixed format (for example `accounting` or `inventory`), so that new systems need no migration. `external_id` is whatever the other system calls the record.
- A receipt can link to several records, and one record can link to several receipts (one transaction paid by two receipts, or one receipt split over two transactions).
- Adding and removing a link writes `link_added` and `link_removed` events, through the same triggers as tags. Who added a link is in its event, not on the row.
- Commands: `link <id> add SYSTEM EXTERNAL_ID [--url URL] [--note TEXT]`, `link <id> remove SYSTEM EXTERNAL_ID`. Search gains `--linked-to SYSTEM:ID` (the reverse lookup) and `--not-linked SYSTEM`, which lists receipts not yet attached in that system.

### 10. Output and other consumers

`Receipt` (the model) gains every new column, and `show` and the JSON output include them, with line items, tags, links and, for purchases, net paid. A read-only MCP server is being built separately. If it returns this model, it picks up the new fields without its own change; either way, its output follows the same meanings for `amount`, `paid_amount` and `effective_amount`. Write tools for tags and links are a follow-up after tickets 7 and 8.

### Merged receipts

The evidence-store design marks a merged receipt with `merged_into_id` instead of deleting it. In this design:

- Search, `--needs-review`, `--untagged`, `--not-linked`, text search, totals and the JSON output leave out merged receipts (`merged_into_id IS NULL`). `show` on a merged receipt still works, and says which receipt it was merged into.
- `receipt_net` counts a refund against the purchase that remains, and leaves out a refund that was itself merged away (above). No `refund_of` is rewritten, so undoing a merge needs no fix-up.
- The refund linking rule and `reextract` skip merged receipts.
- A merge always points at a receipt that isn't itself merged; the evidence-store design keeps it so (it flattens merges and checks this with a trigger), which is why one `COALESCE` is enough.
- These conditions need the evidence-store design's columns. Whichever ticket lands second, this design's or that design's merge, adds them.

### 11. The finished `receipts` table

For reference, the columns this design adds to `receipt.receipts`:

| Column | Type | Ticket |
|---|---|---|
| `paid_amount` | `NUMERIC(12,2)`, nullable | 1 |
| `field_confidence` | `JSONB`, default `{}` | 1 |
| `manual_fields` | `TEXT[]`, default `{}` | 2 |
| `order_number` | `TEXT` | 3 |
| `payment_method` | `TEXT` | 3 |
| `payment_last4` | `TEXT` | 3 |
| `payer` | `TEXT` | 3 |
| `kind` | `TEXT`, default `purchase` | 5 |
| `refund_of` | `UUID`, references `receipts` | 5 |
| `search_document` | `TSVECTOR` | 6 |

New tables: `extractions` (2), `receipt_events` (3), `line_items` (4), `tags` and `receipt_tags` (7), `external_links` (8). New view: `receipt_net` (5). New grant-target role: `receipt_index_dev_annotate` (7).

Every column added to `receipts` gets column-level `INSERT` and `UPDATE` grants for `_write` and `SELECT` for `_read`, as the existing migrations do, apart from `search_document`, which only the trigger writes.

---

## Implementation Plan

These tickets are proposed here and filed only after the operator signs off this design. Each lands as its own pull request with its migration, its tests and its documentation (`docs/user/cli-reference.md`).

| # | Ticket | Files | Depends on |
|---|---|---|---|
| 1 | **Paid amount.** Column, field confidence, extraction rules, search and output on the effective amount, measured token cost | new migration; `models.py`, `extraction.py`, `pipeline.py`, `repository.py`, `cli.py`; `tests/integration/conftest.py`; unit and integration tests; `docs/user/cli-reference.md` | — |
| 2 | **Re-extract and extraction history.** `extractions` table with its append-only guard, `manual_fields`, prompt version, `receipt_document` in the output, `fetch_by_id` on both adapters, the `reextract` command, `search --needs-review` | new migration; `extraction.py`, `pipeline.py`, `repository.py`, `cli.py`, `config.py`, `adapters/base.py`, `adapters/imap.py`, `adapters/gdrive.py`; tests; CLI reference | 1 |
| 3 | **Order number, payment and hand edits.** Columns, `receipt_events` with its append-only guard, the `edit` and `history` commands, new search filters | new migration; `models.py`, `extraction.py`, `repository.py`, `cli.py`; tests; CLI reference | 2 |
| 4 | **Line items.** Table, extraction, `--sku` and `--item` | new migration; `models.py`, `extraction.py`, `pipeline.py`, `repository.py`, `cli.py`; tests; CLI reference | 2 |
| 5 | **Refunds and net paid.** `kind`, `refund_of`, the view, linking at ingest, the `refund` commands | new migration; `models.py`, `extraction.py`, `pipeline.py`, `repository.py`, `cli.py`; tests; CLI reference | 3 |
| 6 | **Text search.** Column, function, triggers, backfill, `--text` | new migration; `repository.py`, `cli.py`; tests; CLI reference | 3, 4 |
| 7 | **Tags.** Vocabulary, `receipt_tags`, the event triggers, the annotate grant-target role and its superuser setup step, commands, `--tag` and `--untagged` | new migration; `db/init/01-create-roles.sql`, `deploy/db-init/01-create-roles.sh`; `models.py`, `repository.py`, `cli.py`, `config.py`; tests; CLI reference and server deployment guide | 3 |
| 8 | **External links.** Table, event triggers, commands, `--linked-to` and `--not-linked` | new migration; `models.py`, `repository.py`, `cli.py`; tests; CLI reference | 3, 7 (role) |

Tickets 1 and 2 come first and in that order: the consumer needs paid amounts on the back catalogue, and only re-extraction gives old receipts one. After ticket 2, and after the accuracy check, the back catalogue is re-extracted once; after tickets 3, 4 and 5 have all merged, once more.

Ticket 1 changes nothing that later tickets depend on, beyond its column, and can ship and run in production on its own.

Tickets 3 to 8 can run partly in parallel once their dependencies are merged; tickets that touch the same files go one after another. Any ticket landing after the evidence-store design's provenance migration reads the subject and source IDs from provenance instead of `receipts`.

### Follow-ups, not tickets yet

- MCP write tools for tags and links, with the annotate login, its connection string and the post-deploy check.
- A `payers` map in the configuration, from last four to a household member.

---

## Testing Strategy

### Unit tests

- Output model validation: `paid_amount` bounds, `last4` format, line-item quantities, the 100-item cap, field confidence ranges.
- Prompt text: each rule for the paid amount is present (a guard against an edit dropping one).
- Effective amount: search filters and sort use paid amount, then total.
- Re-extraction rules: fill-only versus `--overwrite`, `manual_fields` never written, the confidence gate, skipping receipts at the current prompt version, input preference order.
- Refund linking: one candidate links, none or several leave it unlinked, a currency mismatch is refused.
- Tags: unknown and inactive tags are refused; each apply and remove writes one event.
- Mocked LLM results use real integers for `result.usage`.

### Integration tests

Against the devtest database, with every migration mirrored in the integration conftest:

- Each migration applies and rolls back on a database with existing rows.
- Ingest of a fixture receipt fills `paid_amount`, `field_confidence` and an `extractions` row.
- `reextract --input rendition` over fixtures: the first run fills fields, a second run with the same prompt version does nothing, an edited field survives `--overwrite`.
- The search document follows changes to line items, and `--text` finds a receipt by an item description.
- `receipt_net` gives the right net figure for a purchase with two partial refunds.
- The annotate role can apply a tag and add a link, each with a trigger-written event recorded as `agent`, and is refused when it tries to define a tag, update a receipt or write an event directly.
- An update or delete of a history row is refused even for the table's owner, and deleting a receipt still cascades.
- A fill-only re-extraction sets `kind = 'refund'` on a receipt with no extraction history, and links it to its purchase.

The integration conftest today replays only the table migrations, not the grant migrations, and connects as the superuser, so no grant would ever be tested. Ticket 3 extends `_run_migrations()` to replay the grant migrations and adds a second connection as a restricted role; ticket 7 adds one as the annotate role.

### Fixtures

The test fixtures gain receipts where paid and total differ: a gift card partly used, an unpaid invoice, an invoice with a part payment, a statement with a payment line, an order with a discount line, a confirmation and a shipping email for one order, a refund email for that order, and a refund given as store credit. They use placeholder vendors and amounts.

### Accuracy check

The devtest database holds no real receipts, so this check runs in production after ticket 2 is deployed. The operator re-extracts a sample of at least 30 receipts with `reextract --id ...`, chosen to include gift cards, invoices, statements and refunds, and checks each paid amount by hand against what was actually charged. The result is recorded on ticket 2, and the full back-catalogue run waits until it meets success criterion 5.

---

## Risks & Mitigation

| Risk | Impact | Mitigation |
|---|---|---|
| The extractor confuses paid and total on some formats | Wrong figures in claims and reconciliation | Explicit rules in the prompt, per-field confidence, the hand-checked sample, `edit` for corrections that re-extraction respects |
| A re-extraction run overwrites good values with worse ones | Lost data | Fill-only by default, `--overwrite` prints every change, manual fields are never written, every run's output is kept in `extractions` |
| Re-extraction cost grows with the back catalogue and with each prompt change | Spend | `--dry-run` estimates first, the prompt version and model skip what is current, the suggested two runs |
| The source item is gone (message deleted, file moved) | Re-extraction falls back to the rendition, which can be less complete | The rendition path; originals from the evidence store remove this risk for new receipts |
| Long orders produce long output | Truncated output or failed validation | The 100-item cap and an output token limit large enough for it |
| Tags drift or multiply | Tags stop mapping to tracking codes | Controlled vocabulary; only a person can define a tag |
| An agent applies wrong tags or links | Bad coding of receipts | Every change is in the history with its actor and can be undone; the annotate role limits what an agent can change |
| An email falsely claims to be a refund for a known order | A purchase's net paid is too low | Every automatic link is an event, `refund list` shows links, `refund unlink` undoes one |
| The application's role can drop the history guard | History could be altered by a deliberate schema change | The guard stops application code; separating the owner role is an open question for the operator |
| The subject moves to provenance while text search is being built | A broken search document | The function and trigger are adapted by whichever migration lands second, as both designs state |

---

## Out of Scope

- Paying in a different currency from the total (a foreign-currency charge on a home-currency card). Each receipt keeps one currency.
- Splitting one payment over several receipts, or one receipt over several payments, beyond what external links can say.
- Tags on line items rather than receipts.
- Syncing with an accounting system. Links record a relationship; they don't create or read records elsewhere.
- File names, folder layout, originals, hashes, provenance and duplicates, which the evidence-store design covers.
- Changing the extraction model.
- Editing line items by hand.

---

## Open Questions

| Question | Who decides | When |
|---|---|---|
| Should the application connect as a role that owns nothing, with `receipt_index` kept for migrations? That closes the last way to alter history (dropping the guard trigger), and changes the production deployment | The operator | Before ticket 3 is filed |
| Should a tag namespace allow only one tag per receipt (for example one `class:` value)? | The operator | When ticket 7 is planned |
