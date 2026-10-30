-- One provenance row per existing receipt (evidence-store design, §6,
-- migration step 2). Its original is pending until the originals backfill.
-- A re-run adds nothing.
INSERT INTO receipt.provenance (
    receipt_id, is_primary, source_type, source_name, source_id, received_at,
    email_subject, email_sender, email_date, file_name, home_receipt_id,
    original_status, match_method
)
SELECT r.id, TRUE, r.source_type, r.source_name, r.source_id,
       COALESCE(r.email_date, r.created_at),
       r.email_subject, r.email_sender, r.email_date, r.file_name, r.id,
       'pending', 'migrated'
  FROM receipt.receipts r
 WHERE r.source_id IS NOT NULL
ON CONFLICT (source_id) DO NOTHING;

-- _all: no grants; 000017 holds them.
