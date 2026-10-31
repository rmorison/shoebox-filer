-- Duplicate detection (evidence-store design, §6 and §8, Implementation Plan
-- ticket 7): suspected duplicate pairs, the append-only history of every
-- link and unlink, the merge columns and their triggers, and the merge
-- conditions of v_receipt_net (receipt data-model design, "Merged receipts").

-- Suspected duplicates awaiting review (metadata-only matches, and hash or
-- order-number matches a guard refused). Pairs reference provenance, not
-- receipts: provenance survives a link, so a decided pair stays valid after a
-- merge. provenance_id is the newer record, candidate_of_provenance_id the
-- older; the code checks both orders before it inserts.
CREATE TABLE IF NOT EXISTS receipt.duplicate_candidates (
    id                          UUID        NOT NULL DEFAULT uuidv7(),
    provenance_id               UUID        NOT NULL,
    candidate_of_provenance_id  UUID        NOT NULL,
    reason                      TEXT        NOT NULL,
    status                      TEXT        NOT NULL DEFAULT 'open',
    decided_by                  TEXT,
    decided_at                  TIMESTAMPTZ,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_duplicate_candidates PRIMARY KEY (id),
    CONSTRAINT uq_duplicate_candidates_pair
        UNIQUE (provenance_id, candidate_of_provenance_id),
    CONSTRAINT fk_duplicate_candidates_provenance FOREIGN KEY (provenance_id)
        REFERENCES receipt.provenance (id),
    CONSTRAINT fk_duplicate_candidates_candidate_of
        FOREIGN KEY (candidate_of_provenance_id)
        REFERENCES receipt.provenance (id),
    CONSTRAINT ck_duplicate_candidates_status
        CHECK (status IN ('open', 'linked', 'dismissed'))
);

CREATE INDEX IF NOT EXISTS idx_duplicate_candidates_candidate_of
    ON receipt.duplicate_candidates (candidate_of_provenance_id);

-- Append-only record of every link, unlink, merge and unmerge.
CREATE TABLE IF NOT EXISTS receipt.provenance_link_history (
    id                UUID        NOT NULL DEFAULT uuidv7(),
    provenance_id     UUID        NOT NULL,
    action            TEXT        NOT NULL,
    from_receipt_id   UUID,
    to_receipt_id     UUID        NOT NULL,
    match_method      TEXT        NOT NULL,
    actor             TEXT        NOT NULL,
    actor_kind        TEXT        NOT NULL,
    reason            TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_provenance_link_history PRIMARY KEY (id),
    CONSTRAINT fk_provenance_link_history_provenance FOREIGN KEY (provenance_id)
        REFERENCES receipt.provenance (id),
    CONSTRAINT ck_provenance_link_history_action
        CHECK (action IN ('link', 'unlink', 'merge', 'unmerge')),
    CONSTRAINT ck_provenance_link_history_actor_kind
        CHECK (actor_kind IN ('system', 'person', 'agent'))
);

CREATE INDEX IF NOT EXISTS idx_provenance_link_history_provenance_id
    ON receipt.provenance_link_history (provenance_id);

-- A merge keeps the receipt it empties, marked with the receipt that remains.
ALTER TABLE receipt.receipts
    ADD COLUMN IF NOT EXISTS merged_into_id UUID,
    ADD COLUMN IF NOT EXISTS merged_at TIMESTAMPTZ;
ALTER TABLE receipt.receipts
    ADD CONSTRAINT fk_receipts_merged_into FOREIGN KEY (merged_into_id)
        REFERENCES receipt.receipts (id),
    ADD CONSTRAINT ck_receipts_merged_not_self
        CHECK (merged_into_id IS NULL OR merged_into_id <> id),
    ADD CONSTRAINT ck_receipts_merged_at
        CHECK ((merged_into_id IS NULL) = (merged_at IS NULL));
CREATE INDEX IF NOT EXISTS idx_receipts_merged_into_id
    ON receipt.receipts (merged_into_id);

-- Refuses every change. No cascade exception: nothing deletes provenance or
-- receipts. Guards the owner too, which grants alone can't.
CREATE OR REPLACE FUNCTION receipt.forbid_link_history_change()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION '% on %.% is not allowed: history is append-only',
        TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER trg_provenance_link_history_append_only
    BEFORE UPDATE OR DELETE ON receipt.provenance_link_history
    FOR EACH ROW EXECUTE FUNCTION receipt.forbid_link_history_change();
CREATE OR REPLACE TRIGGER trg_provenance_link_history_no_truncate
    BEFORE TRUNCATE ON receipt.provenance_link_history
    FOR EACH STATEMENT EXECUTE FUNCTION receipt.forbid_link_history_change();

-- merged_into_id never names a receipt that is itself merged. A merge
-- flattens chains before setting it, so v_receipt_net needs one COALESCE.
CREATE OR REPLACE FUNCTION receipt.check_merged_into()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.merged_into_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM receipt.receipts
        WHERE id = NEW.merged_into_id AND merged_into_id IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'receipt % would be merged into merged receipt %',
            NEW.id, NEW.merged_into_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER trg_receipts_check_merged_into
    BEFORE INSERT OR UPDATE OF merged_into_id ON receipt.receipts
    FOR EACH ROW EXECUTE FUNCTION receipt.check_merged_into();

-- The merge conditions 000016 left out: a refund counts against the purchase
-- that remains after any merge, and not at all if it was itself merged away;
-- a merged purchase has no row.
CREATE OR REPLACE VIEW receipt.v_receipt_net AS
WITH linked_refunds AS (
    SELECT r.id,
           COALESCE(r.paid_amount, r.amount)  AS refund_amount,
           COALESCE(t.merged_into_id, t.id)   AS purchase_id
    FROM receipt.receipts r
    JOIN receipt.receipts t ON t.id = r.refund_of
    WHERE r.kind = 'refund' AND r.merged_into_id IS NULL
)
SELECT p.id                                   AS receipt_id,
       COALESCE(p.paid_amount, p.amount)      AS paid,
       COALESCE(SUM(lr.refund_amount), 0)     AS refunded,
       COALESCE(p.paid_amount, p.amount)
         - COALESCE(SUM(lr.refund_amount), 0) AS net_paid,
       COUNT(lr.id)                           AS refund_count
FROM receipt.receipts p
LEFT JOIN linked_refunds lr ON lr.purchase_id = p.id
WHERE p.kind = 'purchase' AND p.merged_into_id IS NULL
GROUP BY p.id;

-- _all: full DDL access (runs migrations)
GRANT ALL PRIVILEGES ON receipt.duplicate_candidates,
    receipt.provenance_link_history TO receipt_index_dev_all;

-- _write: only what ingest, link, unlink, duplicates and the scan write.
-- History is insert-only; candidates are inserted, then decided.
GRANT SELECT ON receipt.duplicate_candidates,
    receipt.provenance_link_history TO receipt_index_dev_write;
GRANT INSERT (provenance_id, candidate_of_provenance_id, reason)
    ON receipt.duplicate_candidates TO receipt_index_dev_write;
GRANT UPDATE (status, decided_by, decided_at)
    ON receipt.duplicate_candidates TO receipt_index_dev_write;
GRANT INSERT (provenance_id, action, from_receipt_id, to_receipt_id,
              match_method, actor, actor_kind, reason)
    ON receipt.provenance_link_history TO receipt_index_dev_write;
GRANT SELECT (merged_into_id, merged_at), UPDATE (merged_into_id, merged_at)
    ON receipt.receipts TO receipt_index_dev_write;
GRANT UPDATE (receipt_id, is_primary, home_receipt_id, match_method)
    ON receipt.provenance TO receipt_index_dev_write;

-- _read: SELECT only (the nightly backup reads every table). It already
-- reads every column of receipts (000002).
GRANT SELECT ON receipt.duplicate_candidates,
    receipt.provenance_link_history TO receipt_index_dev_read;
