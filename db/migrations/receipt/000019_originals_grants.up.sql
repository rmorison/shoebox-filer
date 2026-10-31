-- Grants for keeping originals on the store (evidence-store design,
-- Implementation Plan ticket 3). Ingest now records each email's original
-- and its attachments: the provenance row's hash, original and receipt
-- document, and one provenance_files row per file. Each row is inserted
-- whole, so no UPDATE is granted. owner, content_kind, pdf_status,
-- last_verified_at and original_unavailable_reason stay unwritten.

-- _write
GRANT INSERT (source_sha256, original_file_id, receipt_document_file_id,
              document_certainty)
    ON receipt.provenance TO receipt_index_dev_write;
GRANT INSERT (provenance_id, position, file_id, role, file_name)
    ON receipt.provenance_files TO receipt_index_dev_write;

-- _read and _all: nothing new; 000017 grants SELECT and ALL on these tables.
