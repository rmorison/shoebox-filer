-- Take back 000024's grants. 000023's down drops the tables and functions.
REVOKE ALL PRIVILEGES ON receipt.statement_feed_state, receipt.statements,
    receipt.statement_revisions, receipt.statement_sources,
    receipt.statement_accounts, receipt.statement_lines
    FROM receipt_index_dev_all, receipt_index_dev_write, receipt_index_dev_read;
REVOKE EXECUTE ON FUNCTION receipt.lock_statement_feed(), receipt.next_feed_value()
    FROM receipt_index_dev_all, receipt_index_dev_write;
