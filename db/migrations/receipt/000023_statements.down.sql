-- Lossy: dropping these tables drops every statement, revision, account and
-- line. The stored originals stay in receipt.stored_files and the store,
-- since other records can share them (see the plan's Rollback). Down
-- 000024 first, which takes back the grants.

-- Dropping a table drops its triggers. statements and statement_revisions
-- reference each other, so drop the cross reference first.
ALTER TABLE IF EXISTS receipt.statements
    DROP CONSTRAINT IF EXISTS fk_statements_current_revision;
DROP TABLE IF EXISTS receipt.statement_lines;
DROP TABLE IF EXISTS receipt.statement_accounts;
DROP TABLE IF EXISTS receipt.statement_sources;
DROP TABLE IF EXISTS receipt.statement_revisions;
DROP TABLE IF EXISTS receipt.statements;
DROP TABLE IF EXISTS receipt.statement_feed_state;

DROP FUNCTION IF EXISTS receipt.check_statement_line_account();
DROP FUNCTION IF EXISTS receipt.check_statement_account_revision();
DROP FUNCTION IF EXISTS receipt.check_statement_revision_accounts();
DROP FUNCTION IF EXISTS receipt.check_statement_revision_is_current();
DROP FUNCTION IF EXISTS receipt.check_statement_current_revision();
DROP FUNCTION IF EXISTS receipt.set_statement_change_seq();
DROP FUNCTION IF EXISTS receipt.set_statement_revision_number();
DROP FUNCTION IF EXISTS receipt.forbid_statement_history_change();
DROP FUNCTION IF EXISTS receipt.next_feed_value();
DROP FUNCTION IF EXISTS receipt.lock_statement_feed();
