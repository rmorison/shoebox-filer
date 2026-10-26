# ADR-0011: Paid Amount Alongside the Total, and Line Items in Their Own Table

## Status

Proposed

## Context

Each receipt has one `amount`. The extraction prompt asks for "the total amount charged", and in practice the extractor often returns the order or statement total. The main consumer builds claim and reconciliation packets, and needs what was actually paid: after discounts, gift cards and store credit, and as charged to a card or account. The two figures differ on a minority of receipts, and existing rows don't say which one they hold.

The same consumer matches some purchases item by item, by description or product ID, so the items on a receipt need to be stored too.

Key factors:

- **Existing rows can't be relabelled.** Without re-extraction, nobody can tell whether a stored `amount` is the total or the payment.
- **Compatibility.** The CLI's JSON output already returns `amount`, consumers read it, and a planned read-only MCP server will return the same fields.
- **Unknown is a real value.** An unpaid invoice has a total and no payment.
- **Line items are searched and matched one by one**, by description and by SKU.
- **Re-extraction replaces a receipt's line items as a set.**

## Decision

### Paid amount

Keep `amount` as the order, invoice or statement total, and add a nullable `paid_amount` for what the transaction charged to the payer's payment methods, or for a refund what it returned to them. Gift cards, store credit, points and vouchers are excluded, since they never reach a bank or card statement. `NULL` means "not known" and is never filled in from `amount`; `0` means nothing was charged, as on an unpaid invoice.

The first re-extraction of a receipt extracted under the old prompt also replaces its `amount`, since the old prompt sometimes stored the payment there. Each replaced value is printed and kept in the extraction record. A consumer that has already read such an `amount` can see it change.

Search, sorting and the text output use `COALESCE(paid_amount, amount)`, the effective amount, with an expression index. JSON output keeps `amount` unchanged and adds `paid_amount` and `effective_amount`.

### Line items

Store line items in a child table, `receipt.line_items`, one row per item, with the document's position, description, quantity, unit price, line total and SKU. It has a trigram index on description and a B-tree index on SKU. Rows are replaced as a set for each receipt, in one transaction.

## Consequences

### Positive

- **One meaning per column.** After the back-catalogue run, `amount` is the total on every row and `paid_amount` the payment, and the extraction record keeps what each run replaced.
- **Consumers keep working.** `amount` means what it meant, and the new fields are additions.
- **"Not known" stays visible**, so `search --needs-review` can list receipts that still need a paid amount.
- **Item search uses ordinary indexes**, and text search can include item descriptions through a trigger.
- **Constraints apply per item**, such as a positive quantity.

### Negative

- **Two amount columns** where there was one, and a third name (`effective_amount`) in output. Each needs explaining in the CLI reference.
- **A consumer that reads only `amount`** goes on getting the total until it changes to read `effective_amount`.
- **Old totals can change** on the first re-extraction, where the old prompt stored a payment. The change is printed and recorded, not silent.
- **A join or a second query** to return a receipt with its items.
- **More rows to write** on ingest and re-extraction.

### Alternatives Considered

- **Redefine `amount` as the paid amount.** Mixes two meanings in one column until every row is re-extracted, with no way to tell them apart. Not chosen.
- **Add `total_amount` and keep `amount` for the paid figure.** The same mixing problem in the other direction. Not chosen.
- **Line items as a JSONB array on the receipt.** One write and no join, but SKU lookup, item matching and per-item constraints become JSON path queries with weaker indexes and no constraints. Not chosen.
- **Line items as text in the description.** No structure to match on. Not chosen.

## Follow-ups

- Ticket 1 of the [receipt data-model design](../designs/receipt-data-model.md): the paid amount, on its own.
- Ticket 4: the line-item table.
- The CLI reference explains `amount`, `paid_amount` and `effective_amount`.
