-- The original constraint rejects 'started', so remove those rows first.
-- Drain pending work before rolling back (see the design's rollback note).
DELETE FROM receipt.ingest_log WHERE status = 'started';

ALTER TABLE receipt.ingest_log DROP CONSTRAINT ck_ingest_log_status;
ALTER TABLE receipt.ingest_log ADD CONSTRAINT ck_ingest_log_status
    CHECK (status IN ('success', 'failed', 'skipped'));

-- Dropping the column also drops its column-level grant.
ALTER TABLE receipt.ingest_log
    DROP COLUMN IF EXISTS source_name;
