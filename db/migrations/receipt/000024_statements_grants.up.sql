-- Grants for the statement tables and the feed counter (000023;
-- statement-extraction plan, U2).

-- _all: full DDL access (runs migrations)
GRANT ALL PRIVILEGES ON receipt.statement_feed_state, receipt.statements,
    receipt.statement_revisions, receipt.statement_sources,
    receipt.statement_accounts, receipt.statement_lines
    TO receipt_index_dev_all;
GRANT EXECUTE ON FUNCTION receipt.lock_statement_feed(), receipt.next_feed_value()
    TO receipt_index_dev_all;

-- _write: append revisions, accounts, lines and sources; create a statement
-- and move its current revision or republish it. Business columns only:
-- ids, timestamps, revision numbers and change_seq come from defaults and
-- triggers (a statement's id is the exception: the writer draws it first,
-- since the statement's first revision is written before it). No UPDATE on
-- history, no DELETE anywhere, and no privilege on the counter's table: the
-- writer takes the feed lock and the triggers draw values through
-- SECURITY DEFINER functions.
GRANT SELECT ON receipt.statements, receipt.statement_revisions,
    receipt.statement_sources, receipt.statement_accounts,
    receipt.statement_lines
    TO receipt_index_dev_write;
GRANT INSERT (id, original_file_id, source_sha256, current_revision_id,
              document_type, status)
    ON receipt.statements TO receipt_index_dev_write;
GRANT UPDATE (current_revision_id, status, document_type, publish_count)
    ON receipt.statements TO receipt_index_dev_write;
GRANT INSERT (statement_id, origin, document_type, status, hold_reason,
              rejected_bound, masked_runs, period_start, period_end,
              printed_account_count, pages, llm_model, prompt_version,
              first_pass_version, input_tokens, output_tokens)
    ON receipt.statement_revisions TO receipt_index_dev_write;
GRANT INSERT (statement_id, source_name, source_id, file_name, received_at)
    ON receipt.statement_sources TO receipt_index_dev_write;
GRANT INSERT (revision_id, ordinal, occurrence, institution, last4,
              account_type, currency, period_start, period_end, status,
              held_by_statement, failure_class, difference, first_page,
              last_page, line_order, date_grace_days, beginning_balance,
              ending_balance, section_totals, column_totals, principal_before,
              principal_after, principal_paid, interest, escrow, fees,
              advances, content_digest)
    ON receipt.statement_accounts TO receipt_index_dev_write;
GRANT INSERT (account_id, ordinal, posted_date, transaction_date, description,
              amount, running_balance, section, page)
    ON receipt.statement_lines TO receipt_index_dev_write;
GRANT EXECUTE ON FUNCTION receipt.lock_statement_feed(), receipt.next_feed_value()
    TO receipt_index_dev_write;

-- _read: SELECT only (the nightly backup reads every table, the MCP server
-- reads the statements). No EXECUTE on the counter's functions.
GRANT SELECT ON receipt.statement_feed_state, receipt.statements,
    receipt.statement_revisions, receipt.statement_sources,
    receipt.statement_accounts, receipt.statement_lines
    TO receipt_index_dev_read;
