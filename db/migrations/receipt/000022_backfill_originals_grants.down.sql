REVOKE UPDATE (source_sha256, original_file_id, receipt_document_file_id,
               document_certainty, original_status, original_unavailable_reason)
    ON receipt.provenance FROM receipt_index_dev_write;
