-- Grants for backfill --originals-only (always-on ingest design, §8;
-- evidence-store design, §7 "Backfill"). A receipt ingested before originals
-- were kept has a provenance row whose original is pending. Backfill
-- re-fetches the item, stores it and fills the row in (attach_original), or
-- marks it unavailable with a reason (mark_original_unavailable). It also
-- records the whole original's hash where the row has none, so the
-- exact-hash duplicate rule covers the back catalogue.

-- _write
GRANT UPDATE (source_sha256, original_file_id, receipt_document_file_id,
              document_certainty, original_status, original_unavailable_reason)
    ON receipt.provenance TO receipt_index_dev_write;

-- _read and _all: nothing new; 000017 grants SELECT and ALL on this table.
