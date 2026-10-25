ALTER TABLE receipt.ingest_log
    ADD COLUMN llm_input_tokens INTEGER,
    ADD COLUMN llm_output_tokens INTEGER,
    ADD COLUMN llm_cache_read_tokens INTEGER,
    ADD COLUMN llm_requests INTEGER,
    ADD COLUMN llm_model TEXT;

-- Grant access to new columns
GRANT INSERT (llm_input_tokens, llm_output_tokens, llm_cache_read_tokens, llm_requests, llm_model)
    ON receipt.ingest_log TO receipt_index_dev_write;
GRANT UPDATE (llm_input_tokens, llm_output_tokens, llm_cache_read_tokens, llm_requests, llm_model)
    ON receipt.ingest_log TO receipt_index_dev_write;
GRANT SELECT (llm_input_tokens, llm_output_tokens, llm_cache_read_tokens, llm_requests, llm_model)
    ON receipt.ingest_log TO receipt_index_dev_read;
