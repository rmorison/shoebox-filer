ALTER TABLE receipt.receipts DROP COLUMN IF EXISTS manual_fields;

DROP TRIGGER IF EXISTS trg_extractions_no_truncate ON receipt.extractions;
DROP TRIGGER IF EXISTS trg_extractions_append_only ON receipt.extractions;
DROP TABLE IF EXISTS receipt.extractions;
DROP FUNCTION IF EXISTS receipt.forbid_history_change();
