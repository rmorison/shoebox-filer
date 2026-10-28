-- The original constraint rejects 'queued', so remove those rows first.
-- Run `receipt-index ingest` before rolling back, so queued items are
-- processed rather than left on their earlier (terminal) failed row.
DELETE FROM receipt.ingest_log WHERE status = 'queued';

ALTER TABLE receipt.ingest_log DROP CONSTRAINT ck_ingest_log_status;
ALTER TABLE receipt.ingest_log ADD CONSTRAINT ck_ingest_log_status
    CHECK (status IN ('started', 'success', 'failed', 'skipped'));

DROP INDEX IF EXISTS receipt.idx_ingest_log_retry_request_id;
DROP INDEX IF EXISTS receipt.idx_ingest_log_source_id_latest;
DROP INDEX IF EXISTS receipt.idx_ingest_log_next_retry_at;

-- Dropping a column also drops its constraints and column-level grants.
ALTER TABLE receipt.ingest_log
    DROP COLUMN IF EXISTS retry_request_id,
    DROP COLUMN IF EXISTS stored_ref,
    DROP COLUMN IF EXISTS error_class,
    DROP COLUMN IF EXISTS next_retry_at,
    DROP COLUMN IF EXISTS counts_attempt,
    DROP COLUMN IF EXISTS attempt;

DROP TABLE IF EXISTS receipt.retry_requests;
