DROP TRIGGER IF EXISTS trg_receipts_set_updated_at ON receipt.receipts;
DROP TABLE IF EXISTS receipt.receipts;
DROP EXTENSION IF EXISTS pg_trgm;
DROP SCHEMA IF EXISTS receipt CASCADE;
