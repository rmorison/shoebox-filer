ALTER TABLE receipt.ingest_log
    DROP COLUMN IF EXISTS llm_input_tokens,
    DROP COLUMN IF EXISTS llm_output_tokens,
    DROP COLUMN IF EXISTS llm_cache_read_tokens,
    DROP COLUMN IF EXISTS llm_requests,
    DROP COLUMN IF EXISTS llm_model;
