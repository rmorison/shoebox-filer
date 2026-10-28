-- Order number, payment and the receipt history (receipt data-model design,
-- §4). Every change made to a receipt after extraction (a hand edit, and
-- later tags, links and refund links) writes one receipt_events row in the
-- same transaction. Rows are append-only: the guard trigger from 000011
-- refuses UPDATE, DELETE and TRUNCATE even for the role that owns the table,
-- apart from the cascade when a receipt is deleted.

-- Only the last four digits of a card or account number are ever stored.
ALTER TABLE receipt.receipts
    ADD COLUMN IF NOT EXISTS order_number   TEXT,
    ADD COLUMN IF NOT EXISTS payment_method TEXT,
    ADD COLUMN IF NOT EXISTS payment_last4  TEXT,
    ADD COLUMN IF NOT EXISTS payer          TEXT;

ALTER TABLE receipt.receipts
    ADD CONSTRAINT ck_receipts_payment_method CHECK (payment_method IS NULL OR
        payment_method IN ('card', 'bank', 'wallet', 'cash', 'transfer', 'other')),
    ADD CONSTRAINT ck_receipts_payment_last4
        CHECK (payment_last4 IS NULL OR payment_last4 ~ '^[0-9]{4}$');

-- Not unique: a confirmation, a shipment, a refund and an invoice can share one.
CREATE INDEX IF NOT EXISTS idx_receipts_order_number
    ON receipt.receipts (order_number);
CREATE INDEX IF NOT EXISTS idx_receipts_payment_last4
    ON receipt.receipts (payment_last4);

CREATE TABLE IF NOT EXISTS receipt.receipt_events (
    id          UUID NOT NULL DEFAULT uuidv7(),
    receipt_id  UUID NOT NULL,
    event_type  TEXT NOT NULL,
    payload     JSONB NOT NULL,
    actor       TEXT NOT NULL,
    actor_kind  TEXT NOT NULL,
    reason      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_receipt_events PRIMARY KEY (id),
    CONSTRAINT fk_receipt_events_receipt FOREIGN KEY (receipt_id)
        REFERENCES receipt.receipts (id) ON DELETE CASCADE,
    CONSTRAINT ck_receipt_events_type CHECK (event_type IN (
        'field_edited', 'manual_cleared',
        'tag_added', 'tag_removed',
        'link_added', 'link_removed',
        'refund_linked', 'refund_unlinked')),
    CONSTRAINT ck_receipt_events_actor_kind
        CHECK (actor_kind IN ('person', 'agent', 'rule'))
);

CREATE INDEX IF NOT EXISTS idx_receipt_events_receipt_created
    ON receipt.receipt_events (receipt_id, created_at);

-- receipt.forbid_history_change() comes from 000011.
CREATE OR REPLACE TRIGGER trg_receipt_events_append_only
    BEFORE UPDATE OR DELETE ON receipt.receipt_events
    FOR EACH ROW EXECUTE FUNCTION receipt.forbid_history_change();

CREATE OR REPLACE TRIGGER trg_receipt_events_no_truncate
    BEFORE TRUNCATE ON receipt.receipt_events
    FOR EACH STATEMENT EXECUTE FUNCTION receipt.forbid_history_change();

-- _all: full DDL access (runs migrations)
GRANT ALL PRIVILEGES ON receipt.receipt_events TO receipt_index_dev_all;

-- _write: read and append; no UPDATE or DELETE (history is append-only).
GRANT SELECT ON receipt.receipt_events TO receipt_index_dev_write;
GRANT INSERT (receipt_id, event_type, payload, actor, actor_kind, reason)
    ON receipt.receipt_events TO receipt_index_dev_write;
GRANT INSERT (order_number, payment_method, payment_last4, payer),
      UPDATE (order_number, payment_method, payment_last4, payer)
    ON receipt.receipts TO receipt_index_dev_write;

-- _read: SELECT only (the nightly backup reads every table)
GRANT SELECT ON receipt.receipt_events TO receipt_index_dev_read;
GRANT SELECT (order_number, payment_method, payment_last4, payer)
    ON receipt.receipts TO receipt_index_dev_read;
