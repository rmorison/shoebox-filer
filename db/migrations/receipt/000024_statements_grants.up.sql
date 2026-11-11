-- Grants for the statement tables and the feed counter (000023;
-- statement-extraction plan, U2).

-- _all: full DDL access (runs migrations)
GRANT ALL PRIVILEGES ON receipt.statement_feed_state, receipt.statements,
    receipt.statement_revisions, receipt.statement_sources,
    receipt.statement_accounts, receipt.statement_lines
    TO receipt_index_dev_all;
GRANT EXECUTE ON FUNCTION receipt.next_feed_value() TO receipt_index_dev_all;

-- _write: append revisions, accounts, lines and sources; create a statement
-- and move its current revision or republish it. No UPDATE on history, no
-- DELETE anywhere, and no privilege on the counter's table: the triggers
-- draw revision numbers and change_seq through next_feed_value(), which runs
-- as its owner.
GRANT SELECT, INSERT ON receipt.statement_revisions, receipt.statement_accounts,
    receipt.statement_lines, receipt.statement_sources
    TO receipt_index_dev_write;
GRANT SELECT, INSERT ON receipt.statements TO receipt_index_dev_write;
GRANT UPDATE (current_revision_id, status, document_type, change_seq,
              publish_count, updated_at)
    ON receipt.statements TO receipt_index_dev_write;
GRANT EXECUTE ON FUNCTION receipt.next_feed_value() TO receipt_index_dev_write;

-- _read: SELECT only (the nightly backup reads every table, the MCP server
-- reads the statements).
GRANT SELECT ON receipt.statement_feed_state, receipt.statements,
    receipt.statement_revisions, receipt.statement_sources,
    receipt.statement_accounts, receipt.statement_lines
    TO receipt_index_dev_read;
