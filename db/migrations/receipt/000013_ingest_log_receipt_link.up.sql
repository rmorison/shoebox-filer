-- A link-only receipt: an email whose receipt is only a link to a hosted
-- receipt. The item is recorded as 'skipped' with the link here, so
-- `failures --status link-only` lists it for the operator, who opens the
-- link and saves the receipt by hand; ingest never follows it. The link
-- may carry a personal token: it is stored on this row only, never in the
-- extraction history or the logs.

ALTER TABLE receipt.ingest_log
    ADD COLUMN receipt_link TEXT;

ALTER TABLE receipt.ingest_log ADD CONSTRAINT ck_ingest_log_receipt_link
    CHECK (receipt_link IS NULL
           OR (status = 'skipped' AND char_length(receipt_link) <= 2048));

-- _all: full DDL access (runs migrations); the table grant covers the column.

-- _write: 000004 grants INSERT on ingest_log column by column
GRANT INSERT (receipt_link) ON receipt.ingest_log TO receipt_index_dev_write;

-- _read: the table-level SELECT covers the new column.
