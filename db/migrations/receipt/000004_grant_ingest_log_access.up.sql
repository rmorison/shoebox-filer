-- Grant ingest_log table privileges to application roles.

-- _all: full DDL access (runs migrations)
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA receipt TO receipt_index_dev_all;

-- _write: insert + select on ingest_log
GRANT SELECT ON receipt.ingest_log TO receipt_index_dev_write;
GRANT INSERT (source_id, source_type, status, receipt_id, vendor, amount,
              email_subject, email_sender, email_date, error_message)
    ON receipt.ingest_log TO receipt_index_dev_write;

-- _read: SELECT only
GRANT SELECT ON receipt.ingest_log TO receipt_index_dev_read;
