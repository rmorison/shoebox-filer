# ADR-0010: Provenance Records and Duplicate Detection

## Status

Accepted. Amends RD-07 in the [product specification](../../product/features/receipt-search-index-spec.md#resolved-decisions).

## Context

RD-07 tracks each source item by its own ID (IMAP Message-ID, Drive file ID) and puts duplicate receipt detection out of scope. In practice the same receipt arrives more than once: as the vendor's email, as a forward from another household member, and as a scan dropped into Drive. Each arrival becomes its own receipt, so claim and reconciliation totals count it twice or more.

Merging is risky in the other direction too:

- Two identical charges on one day (two parking sessions, two transit fares) are two real expenses. Merging them drops one from a claim.
- Unrelated receipts from one vendor often share boilerplate attachments, such as a terms-and-conditions PDF, a logo or inline images. Their hashes match, but the receipts don't.
- A refund shares its purchase's vendor and order number, and often its amount.

The consumer values correct totals and completeness over automation.

## Decision

1. **A receipt has one or more provenance records**, one per source item (`receipt.provenance`), each with its original and its *receipt document*: the file the receipt was read from. Search, totals and packets count receipts. One provenance record is primary, for display. Idempotency moves from `receipts.source_id` to `provenance.source_id`.
2. **Automatic linking only on exact evidence**, and only between receipts of the same `kind` (purchase or refund):
   1. the same sha256 of the whole original;
   2. the same sha256 of the receipt document, when both documents were identified with certainty and the extracted kind, vendor and paid amount match the existing receipt;
   3. the same order or invoice number, vendor and paid amount.

   **An attachment hash match on its own never links**: only the receipt document is compared.
3. **Metadata-only matches are flagged, never linked.** The same vendor and paid amount with dates within a few days goes into `receipt.duplicate_candidates` for a person or agent to review.
4. **Every link can be undone.** `unlink` moves a provenance record out to a new receipt of its own, and `link` merges by hand. Both, and every automatic link, are recorded in an append-only `receipt.provenance_link_history` with the actor, the actor kind and the reason.
5. **Only the whole-original rule runs before extraction**, so an identical file costs no LLM call. The receipt-document and order-number rules need the extracted kind, vendor and paid amount, so they run after extraction. A receipt document counts as certain only for a Drive source file, or when the extraction names the attachment it read.

## Consequences

### Positive

- A forward or a rescan of a receipt no longer inflates totals.
- Two real purchases that look alike stay two, and the operator sees the pair flagged.
- Every merge is explainable and reversible, and the record says who made it.
- Identical files skip extraction and save its cost.
- Existing duplicates are found by a one-time scan, so past periods stop double-counting too.

### Negative

- More tables, and a review queue that someone has to work through.
- Duplicates with no shared bytes and no order number (an HTML-only receipt and a photo of the paper copy) are only flagged, so they need review before a packet is final.
- Rule 3 depends on the receipt data-model design's order number and paid amount, so it lands after that ticket.
- Picking an email's receipt document needs the extraction to report which attachment it read. Until it does, every email's receipt document is ambiguous (a lone PDF may be boilerplate), so rule 2 applies only to Drive sources.

### Alternatives Considered

- **Keep RD-07 (no duplicate detection):** totals stay wrong, and the consumer has to dedupe by hand. Not chosen.
- **Match on any shared attachment hash:** boilerplate attachments would merge unrelated receipts. Not chosen.
- **Automatic linking on a metadata match:** it would merge repeated identical purchases. Not chosen.
- **Fuzzy scoring with a threshold:** opaque, and hard to undo with confidence. Not chosen for now; it may follow, with data from the review queue.

## Follow-ups

The implementation tickets are in the [evidence store design](../designs/evidence-store.md#implementation-plan).
