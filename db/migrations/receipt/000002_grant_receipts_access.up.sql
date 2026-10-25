-- Grant schema and table privileges to application roles.
-- Roles are created by db/init/01-create-roles.sql (Docker entrypoint).

-- _all: full DDL access (runs migrations)
GRANT ALL PRIVILEGES ON SCHEMA receipt TO receipt_index_dev_all;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA receipt TO receipt_index_dev_all;

-- _write: CRUD on business columns only
GRANT USAGE ON SCHEMA receipt TO receipt_index_dev_write;
GRANT SELECT ON receipt.receipts TO receipt_index_dev_write;
GRANT INSERT (source_id, source_type, vendor, amount, currency,
              receipt_date, description, confidence, pdf_path,
              email_subject, email_sender, email_date)
    ON receipt.receipts TO receipt_index_dev_write;
GRANT UPDATE (vendor, amount, currency, receipt_date, description,
              confidence, pdf_path)
    ON receipt.receipts TO receipt_index_dev_write;
GRANT DELETE ON receipt.receipts TO receipt_index_dev_write;

-- _read: SELECT only
GRANT USAGE ON SCHEMA receipt TO receipt_index_dev_read;
GRANT SELECT ON receipt.receipts TO receipt_index_dev_read;
