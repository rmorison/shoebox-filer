REVOKE INSERT (provenance_id, position, file_id, role, file_name)
    ON receipt.provenance_files FROM receipt_index_dev_write;
REVOKE INSERT (source_sha256, original_file_id, receipt_document_file_id,
               document_certainty)
    ON receipt.provenance FROM receipt_index_dev_write;
