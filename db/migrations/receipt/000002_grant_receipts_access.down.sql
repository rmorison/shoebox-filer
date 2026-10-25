-- Revoke all grants on receipt schema objects.
-- Roles themselves are managed by db/init/01-create-roles.sql.

REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA receipt FROM receipt_index_dev_all;
REVOKE ALL PRIVILEGES ON SCHEMA receipt FROM receipt_index_dev_all;

REVOKE ALL PRIVILEGES ON receipt.receipts FROM receipt_index_dev_write;
REVOKE USAGE ON SCHEMA receipt FROM receipt_index_dev_write;

REVOKE ALL PRIVILEGES ON receipt.receipts FROM receipt_index_dev_read;
REVOKE USAGE ON SCHEMA receipt FROM receipt_index_dev_read;
