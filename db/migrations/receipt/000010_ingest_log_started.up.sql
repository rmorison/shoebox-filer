-- A 'started' row is committed before any work on an item, so a crash leaves
-- a trace, and the sync-state cursor may pass the item (cursor rule).
-- source_name says which configured source an item came from.
-- Design: docs/engineering/designs/always-on-ingest.md, section 6.

ALTER TABLE receipt.ingest_log
    ADD COLUMN source_name TEXT;

ALTER TABLE receipt.ingest_log DROP CONSTRAINT ck_ingest_log_status;
ALTER TABLE receipt.ingest_log ADD CONSTRAINT ck_ingest_log_status
    CHECK (status IN ('started', 'success', 'failed', 'skipped'));

-- Grant write access to the new column (000004 grants INSERT column by column)
GRANT INSERT (source_name) ON receipt.ingest_log TO receipt_index_dev_write;
