-- Extraction history and hand-edited fields (receipt data-model design, §3).
-- Every extraction run, at ingest or by `reextract`, writes one row. Rows are
-- append-only: the guard trigger refuses UPDATE, DELETE and TRUNCATE even for
-- the role that owns the table, apart from the cascade when a receipt is
-- deleted (design §4, "Keeping history append-only").

CREATE TABLE IF NOT EXISTS receipt.extractions (
    id                    UUID NOT NULL DEFAULT uuidv7(),
    receipt_id            UUID NOT NULL,
    origin                TEXT NOT NULL,
    input_kind            TEXT NOT NULL,
    -- The stored file the run read; its foreign key to receipt.stored_files
    -- comes with the evidence store's schema migration.
    input_file_id         UUID,
    prompt_version        INTEGER NOT NULL,
    llm_model             TEXT NOT NULL,
    output                JSONB NOT NULL,
    applied_fields        TEXT[] NOT NULL DEFAULT '{}',
    replaced              JSONB NOT NULL DEFAULT '{}'::jsonb,
    llm_input_tokens      INTEGER,
    llm_output_tokens     INTEGER,
    llm_cache_read_tokens INTEGER,
    llm_requests          INTEGER,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_extractions PRIMARY KEY (id),
    CONSTRAINT fk_extractions_receipt FOREIGN KEY (receipt_id)
        REFERENCES receipt.receipts (id) ON DELETE CASCADE,
    CONSTRAINT ck_extractions_origin CHECK (origin IN ('ingest', 'reextract')),
    CONSTRAINT ck_extractions_input_kind
        CHECK (input_kind IN ('original', 'source', 'rendition'))
);

CREATE INDEX IF NOT EXISTS idx_extractions_receipt_created
    ON receipt.extractions (receipt_id, created_at DESC);

-- Shared by every history table (extractions here; receipt_events later).
CREATE OR REPLACE FUNCTION receipt.forbid_history_change()
RETURNS TRIGGER AS $$
BEGIN
    -- Allow the cascade from deleting the receipt itself: by then the parent
    -- row is gone. Refuse every other update or delete.
    IF TG_OP = 'DELETE' AND NOT EXISTS (
        SELECT 1 FROM receipt.receipts WHERE id = OLD.receipt_id
    ) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION '% on %.% is not allowed: history is append-only',
        TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER trg_extractions_append_only
    BEFORE UPDATE OR DELETE ON receipt.extractions
    FOR EACH ROW EXECUTE FUNCTION receipt.forbid_history_change();

CREATE OR REPLACE TRIGGER trg_extractions_no_truncate
    BEFORE TRUNCATE ON receipt.extractions
    FOR EACH STATEMENT EXECUTE FUNCTION receipt.forbid_history_change();

-- Fields a person has edited; re-extraction never writes them.
ALTER TABLE receipt.receipts
    ADD COLUMN IF NOT EXISTS manual_fields TEXT[] NOT NULL DEFAULT '{}';

-- _all: full DDL access (runs migrations)
GRANT ALL PRIVILEGES ON receipt.extractions TO receipt_index_dev_all;

-- _write: read and append; no UPDATE or DELETE (history is append-only).
GRANT SELECT ON receipt.extractions TO receipt_index_dev_write;
GRANT INSERT (receipt_id, origin, input_kind, input_file_id, prompt_version,
              llm_model, output, applied_fields, replaced,
              llm_input_tokens, llm_output_tokens, llm_cache_read_tokens,
              llm_requests)
    ON receipt.extractions TO receipt_index_dev_write;
GRANT INSERT (manual_fields), UPDATE (manual_fields)
    ON receipt.receipts TO receipt_index_dev_write;

-- _read: SELECT only
GRANT SELECT ON receipt.extractions TO receipt_index_dev_read;
GRANT SELECT (manual_fields) ON receipt.receipts TO receipt_index_dev_read;
