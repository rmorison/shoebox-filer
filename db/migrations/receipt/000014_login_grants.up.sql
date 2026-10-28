-- Grants for the separate production logins (Database Standards: _all,
-- _write, _read). The application connects as a member of
-- receipt_index_dev_write; backups, forensics and the MCP server as a member
-- of receipt_index_dev_read. See docs/user/server-deployment.md, "Separate
-- database logins".

-- _read: the nightly pg_dump runs as the read login, and pg_dump reads every
-- table, golang-migrate's two version tables among them. Those tables are
-- created by golang-migrate, not by a migration file, so grant only where
-- they exist under the names the Makefile gives them.
DO $$
DECLARE
    version_table text;
BEGIN
    FOREACH version_table IN ARRAY ARRAY[
        'public.schema_migrations_public',
        'public.schema_migrations_receipt'
    ] LOOP
        IF to_regclass(version_table) IS NOT NULL THEN
            EXECUTE format('GRANT SELECT ON %s TO receipt_index_dev_read',
                           version_table);
        ELSE
            RAISE NOTICE '% not found: no grant change on it', version_table;
        END IF;
    END LOOP;
END $$;

-- _write: ingest_log is append-only (000012), and no code path updates it.
-- 000007 granted UPDATE on its token columns; take that back.
REVOKE UPDATE (llm_input_tokens, llm_output_tokens, llm_cache_read_tokens,
               llm_requests, llm_model)
    ON receipt.ingest_log FROM receipt_index_dev_write;
