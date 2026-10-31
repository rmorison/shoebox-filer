-- Grants for PDF validation at ingest (evidence-store design, §9 and
-- Implementation Plan ticket 8). Ingest records each stored PDF's
-- pdf_status, and a repaired rendition's repaired_from_id. Each row is
-- inserted whole, so no UPDATE is granted: rows stored before this
-- migration keep pdf_status NULL.

-- _write
GRANT INSERT (pdf_status, repaired_from_id)
    ON receipt.stored_files TO receipt_index_dev_write;

-- _read and _all: nothing new; 000017 grants SELECT and ALL on this table.
