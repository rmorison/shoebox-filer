GRANT UPDATE (llm_input_tokens, llm_output_tokens, llm_cache_read_tokens,
              llm_requests, llm_model)
    ON receipt.ingest_log TO receipt_index_dev_write;

DO $$
DECLARE
    version_table text;
BEGIN
    FOREACH version_table IN ARRAY ARRAY[
        'public.schema_migrations_public',
        'public.schema_migrations_receipt'
    ] LOOP
        IF to_regclass(version_table) IS NOT NULL THEN
            EXECUTE format('REVOKE SELECT ON %s FROM receipt_index_dev_read',
                           version_table);
        END IF;
    END LOOP;
END $$;
