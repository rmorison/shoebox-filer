-- Bank and card statements (statement-extraction plan, U2; the read
-- contract in docs/engineering/designs/statement-read-contract.md once U7
-- publishes it). A statement is one original PDF. Every processing run of it
-- writes one immutable revision, with its accounts and the lines of its clean
-- accounts. The statement row points at its current revision and copies its
-- status and type for the change feed. Grants are in 000024.

-- The feed counter: one row, the last value handed out. Every revision
-- number and every feed position (change_seq) comes from it, through
-- next_feed_value() below.
CREATE TABLE IF NOT EXISTS receipt.statement_feed_state (
    id          BOOLEAN NOT NULL DEFAULT TRUE,
    last_value  BIGINT  NOT NULL,

    CONSTRAINT pk_statement_feed_state PRIMARY KEY (id),
    CONSTRAINT ck_statement_feed_state_one_row CHECK (id),
    CONSTRAINT ck_statement_feed_state_last_value CHECK (last_value >= 0)
);

INSERT INTO receipt.statement_feed_state (id, last_value)
VALUES (TRUE, 0)
ON CONFLICT (id) DO NOTHING;

-- The feed lock: the counter's one row, locked until commit. Every writer
-- that draws a counter value holds it, so writers commit in counter order:
-- a reader that has seen a value has seen every committed value below it.
-- A row lock rather than an advisory one, so only a role that may update
-- the counter (through these functions) can take it; a reader can't stall
-- the writers.
--
-- A writer that will also update a statements row takes this lock first,
-- before the row lock, so a republish and a re-run of the same statement
-- take the two locks in the same order and can't deadlock.
--
-- SECURITY DEFINER, here and in next_feed_value(), so a writer needs only
-- EXECUTE and no privilege on statement_feed_state: no login can set the
-- counter by hand.
CREATE OR REPLACE FUNCTION receipt.lock_statement_feed()
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    PERFORM 1 FROM receipt.statement_feed_state WHERE id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'receipt.statement_feed_state has no row';
    END IF;
END;
$$;

REVOKE ALL ON FUNCTION receipt.lock_statement_feed() FROM PUBLIC;

-- The next counter value: the greater of the last value plus one and the
-- current time in microseconds since the epoch, under the feed lock (the
-- UPDATE takes it). The clock floor keeps a new value above anything handed
-- out before a database restore, as long as the server's clock doesn't run
-- backwards.
CREATE OR REPLACE FUNCTION receipt.next_feed_value()
RETURNS BIGINT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    next_value BIGINT;
BEGIN
    UPDATE receipt.statement_feed_state
    SET last_value = greatest(
        last_value + 1,
        (extract(epoch FROM clock_timestamp()) * 1000000)::BIGINT
    )
    WHERE id
    RETURNING last_value INTO next_value;
    IF next_value IS NULL THEN
        RAISE EXCEPTION 'receipt.statement_feed_state has no row';
    END IF;
    RETURN next_value;
END;
$$;

REVOKE ALL ON FUNCTION receipt.next_feed_value() FROM PUBLIC;

CREATE TABLE IF NOT EXISTS receipt.statements (
    id                   UUID        NOT NULL DEFAULT uuidv7(),
    original_file_id     UUID        NOT NULL,
    source_sha256        BYTEA       NOT NULL,
    current_revision_id  UUID        NOT NULL,
    document_type        TEXT        NOT NULL,
    status               TEXT        NOT NULL,
    change_seq           BIGINT      NOT NULL,
    publish_count        INTEGER     NOT NULL DEFAULT 0,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_statements PRIMARY KEY (id),
    CONSTRAINT uq_statements_original UNIQUE (original_file_id),
    CONSTRAINT uq_statements_change_seq UNIQUE (change_seq),
    CONSTRAINT fk_statements_original FOREIGN KEY (original_file_id)
        REFERENCES receipt.stored_files (id),
    CONSTRAINT ck_statements_source_sha256
        CHECK (octet_length(source_sha256) = 32),
    CONSTRAINT ck_statements_publish_count CHECK (publish_count >= 0)
);

CREATE TABLE IF NOT EXISTS receipt.statement_revisions (
    id                     UUID        NOT NULL DEFAULT uuidv7(),
    statement_id           UUID        NOT NULL,
    revision               BIGINT      NOT NULL,
    origin                 TEXT        NOT NULL,
    document_type          TEXT        NOT NULL,
    status                 TEXT        NOT NULL,
    hold_reason            TEXT,
    rejected_bound         TEXT,
    masked_runs            INTEGER     NOT NULL DEFAULT 0,
    period_start           DATE,
    period_end             DATE,
    printed_account_count  INTEGER,
    pages                  INTEGER,
    llm_model              TEXT,
    prompt_version         TEXT,
    first_pass_version     TEXT,
    input_tokens           INTEGER     NOT NULL DEFAULT 0,
    output_tokens          INTEGER     NOT NULL DEFAULT 0,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_statement_revisions PRIMARY KEY (id),
    CONSTRAINT uq_statement_revisions_revision UNIQUE (revision),
    -- Deferred: a new statement's first revision is written before the
    -- statement row it belongs to (see Insert order in the plan).
    CONSTRAINT fk_statement_revisions_statement FOREIGN KEY (statement_id)
        REFERENCES receipt.statements (id) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT ck_statement_revisions_origin
        CHECK (origin IN ('ingest', 'rerun')),
    CONSTRAINT ck_statement_revisions_document_type
        CHECK (document_type IN (
            'deposit', 'card', 'loan_statement',
            'loan_bill', 'tax_or_escrow_notice', 'not_statement',
            'scan', 'unreadable', 'over_bounds'
        )),
    CONSTRAINT ck_statement_revisions_status
        CHECK (status IN ('complete', 'incomplete', 'held', 'rejected')),
    -- A statement type is extracted; every other type is a rejection.
    CONSTRAINT ck_statement_revisions_rejected
        CHECK ((status = 'rejected')
               = (document_type NOT IN ('deposit', 'card', 'loan_statement'))),
    CONSTRAINT ck_statement_revisions_hold_reason
        CHECK (hold_reason IN (
            'no_accounts', 'dropped_account', 'account_count_mismatch',
            'no_usable_account'
        )),
    CONSTRAINT ck_statement_revisions_held_has_reason
        CHECK ((status = 'held') = (hold_reason IS NOT NULL)),
    CONSTRAINT ck_statement_revisions_rejected_bound
        CHECK (rejected_bound IN (
            'pages', 'size', 'text', 'memory', 'crash', 'accounts'
        )),
    CONSTRAINT ck_statement_revisions_over_bounds_has_bound
        CHECK ((document_type = 'over_bounds') = (rejected_bound IS NOT NULL)),
    CONSTRAINT ck_statement_revisions_masked_runs CHECK (masked_runs >= 0),
    CONSTRAINT ck_statement_revisions_period
        CHECK (period_start IS NULL OR period_end IS NULL
               OR period_start <= period_end),
    CONSTRAINT ck_statement_revisions_printed_account_count
        CHECK (printed_account_count >= 0),
    CONSTRAINT ck_statement_revisions_pages CHECK (pages >= 1),
    CONSTRAINT ck_statement_revisions_tokens
        CHECK (input_tokens >= 0 AND output_tokens >= 0)
);

CREATE INDEX IF NOT EXISTS idx_statement_revisions_statement_id
    ON receipt.statement_revisions (statement_id);

-- Deferred too: the statement row and its first revision point at each
-- other, so whichever is written second makes both references true at commit.
ALTER TABLE receipt.statements
    ADD CONSTRAINT fk_statements_current_revision
        FOREIGN KEY (current_revision_id)
        REFERENCES receipt.statement_revisions (id)
        DEFERRABLE INITIALLY DEFERRED;

-- One row per source item that delivered the statement's bytes, as
-- provenance is for receipts. The same bytes dropped twice are one statement
-- with two rows here.
CREATE TABLE IF NOT EXISTS receipt.statement_sources (
    id            UUID        NOT NULL DEFAULT uuidv7(),
    statement_id  UUID        NOT NULL,
    source_name   TEXT        NOT NULL,
    source_id     TEXT        NOT NULL,
    file_name     TEXT,
    received_at   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_statement_sources PRIMARY KEY (id),
    CONSTRAINT uq_statement_sources_source_id UNIQUE (source_id),
    CONSTRAINT fk_statement_sources_statement FOREIGN KEY (statement_id)
        REFERENCES receipt.statements (id)
);

CREATE INDEX IF NOT EXISTS idx_statement_sources_statement_id
    ON receipt.statement_sources (statement_id);

CREATE TABLE IF NOT EXISTS receipt.statement_accounts (
    id                 UUID          NOT NULL DEFAULT uuidv7(),
    revision_id        UUID          NOT NULL,
    ordinal            INTEGER       NOT NULL,
    occurrence         INTEGER       NOT NULL,
    institution        TEXT,
    last4              TEXT,
    account_type       TEXT          NOT NULL,
    currency           TEXT,
    period_start       DATE,
    period_end         DATE,
    status             TEXT          NOT NULL,
    held_by_statement  BOOLEAN       NOT NULL DEFAULT FALSE,
    failure_class      TEXT,
    difference         NUMERIC(14,2),
    first_page         INTEGER,
    last_page          INTEGER,
    line_order         TEXT,
    date_grace_days    INTEGER       NOT NULL DEFAULT 0,
    beginning_balance  NUMERIC(14,2),
    ending_balance     NUMERIC(14,2),
    section_totals     JSONB         NOT NULL DEFAULT '[]',
    column_totals      JSONB         NOT NULL DEFAULT '[]',
    principal_before   NUMERIC(14,2),
    principal_after    NUMERIC(14,2),
    principal_paid     NUMERIC(14,2),
    interest           NUMERIC(14,2),
    escrow             NUMERIC(14,2),
    fees               NUMERIC(14,2),
    advances           NUMERIC(14,2),
    content_digest     BYTEA         NOT NULL,
    created_at         TIMESTAMPTZ   NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_statement_accounts PRIMARY KEY (id),
    CONSTRAINT uq_statement_accounts_ordinal UNIQUE (revision_id, ordinal),
    CONSTRAINT fk_statement_accounts_revision FOREIGN KEY (revision_id)
        REFERENCES receipt.statement_revisions (id),
    CONSTRAINT ck_statement_accounts_ordinal CHECK (ordinal >= 1),
    CONSTRAINT ck_statement_accounts_occurrence CHECK (occurrence >= 1),
    -- Never a full account number: four digits or nothing.
    CONSTRAINT ck_statement_accounts_last4 CHECK (last4 ~ '^[0-9]{4}$'),
    CONSTRAINT ck_statement_accounts_account_type
        CHECK (account_type IN ('deposit', 'card', 'loan')),
    CONSTRAINT ck_statement_accounts_currency CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT ck_statement_accounts_period
        CHECK (period_start IS NULL OR period_end IS NULL
               OR period_start <= period_end),
    -- A loan is figures only, or held; a deposit or card account is clean,
    -- or held.
    CONSTRAINT ck_statement_accounts_status
        CHECK ((account_type = 'loan' AND status IN ('figures_only', 'held'))
               OR (account_type <> 'loan' AND status IN ('clean', 'held'))),
    CONSTRAINT ck_statement_accounts_held_by_statement
        CHECK (NOT held_by_statement OR status = 'held'),
    CONSTRAINT ck_statement_accounts_failure_class
        CHECK (failure_class IN (
            'empty_account', 'no_beginning_balance', 'no_ending_balance',
            'too_many_lines', 'undated', 'does_not_foot',
            'date_out_of_period', 'balance_chain_break', 'totals_mismatch'
        )),
    -- A held account says why: its own class, or the statement's hold.
    CONSTRAINT ck_statement_accounts_held_has_reason
        CHECK ((status = 'held') = (failure_class IS NOT NULL OR held_by_statement)),
    CONSTRAINT ck_statement_accounts_pages
        CHECK (first_page >= 1 AND last_page >= first_page),
    CONSTRAINT ck_statement_accounts_line_order
        CHECK (line_order IN ('oldest_first', 'newest_first')),
    CONSTRAINT ck_statement_accounts_date_grace_days
        CHECK (date_grace_days >= 0),
    CONSTRAINT ck_statement_accounts_totals_are_arrays
        CHECK (jsonb_typeof(section_totals) = 'array'
               AND jsonb_typeof(column_totals) = 'array'),
    -- Loan figures belong to loans only.
    CONSTRAINT ck_statement_accounts_loan_figures
        CHECK (account_type = 'loan'
               OR num_nulls(principal_before, principal_after, principal_paid,
                            interest, escrow, fees, advances) = 7),
    CONSTRAINT ck_statement_accounts_content_digest
        CHECK (octet_length(content_digest) = 32),
    -- A backstop for the application's mask: no run of eight digits (a full
    -- account or card number) in the institution or a total's section name.
    -- ASCII digits only, by decision, as in the mask itself: fullwidth or
    -- Arabic-Indic digits are neither masked nor refused.
    CONSTRAINT ck_statement_accounts_masked
        CHECK (institution !~ '[0-9]{8}'
               AND NOT jsonb_path_exists(section_totals,
                   '$[*].section ? (@ like_regex "[0-9]{8}")')
               AND NOT jsonb_path_exists(column_totals,
                   '$[*].section ? (@ like_regex "[0-9]{8}")')),
    -- NUMERIC accepts NaN; no money figure on a statement is one.
    CONSTRAINT ck_statement_accounts_not_nan
        CHECK (difference <> 'NaN' AND beginning_balance <> 'NaN'
               AND ending_balance <> 'NaN' AND principal_before <> 'NaN'
               AND principal_after <> 'NaN' AND principal_paid <> 'NaN'
               AND interest <> 'NaN' AND escrow <> 'NaN' AND fees <> 'NaN'
               AND advances <> 'NaN')
);

-- match_key pairs an account with itself across revisions, so it must be
-- unique within one: an account with no printed last four counts as one
-- key (NULLS NOT DISTINCT), told apart by its occurrence.
CREATE UNIQUE INDEX IF NOT EXISTS uq_statement_accounts_match_key
    ON receipt.statement_accounts (revision_id, account_type, last4, occurrence)
    NULLS NOT DISTINCT;

CREATE TABLE IF NOT EXISTS receipt.statement_lines (
    id                UUID          NOT NULL DEFAULT uuidv7(),
    account_id        UUID          NOT NULL,
    ordinal           INTEGER       NOT NULL,
    posted_date       DATE          NOT NULL,
    transaction_date  DATE,
    description       TEXT          NOT NULL,
    amount            NUMERIC(14,2) NOT NULL,
    running_balance   NUMERIC(14,2),
    section           TEXT,
    page              INTEGER       NOT NULL,
    created_at        TIMESTAMPTZ   NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_statement_lines PRIMARY KEY (id),
    CONSTRAINT uq_statement_lines_ordinal UNIQUE (account_id, ordinal),
    CONSTRAINT fk_statement_lines_account FOREIGN KEY (account_id)
        REFERENCES receipt.statement_accounts (id),
    CONSTRAINT ck_statement_lines_ordinal CHECK (ordinal >= 1),
    CONSTRAINT ck_statement_lines_page CHECK (page >= 1),
    -- A backstop for the application's mask: no run of eight ASCII digits
    -- (other digit scripts are out of scope, as in the mask).
    CONSTRAINT ck_statement_lines_masked
        CHECK (description !~ '[0-9]{8}' AND section !~ '[0-9]{8}'),
    CONSTRAINT ck_statement_lines_not_nan
        CHECK (amount <> 'NaN' AND running_balance <> 'NaN')
);

-- The history tables, the sources and the counter have no updated_at (and
-- the counter no id or created_at), an exception to the standard columns:
-- none of their rows is ever updated, except the counter's one value.

-- Revisions, accounts and lines are history: the guard refuses UPDATE,
-- DELETE and TRUNCATE, for the owner too, which grants alone can't.
CREATE OR REPLACE FUNCTION receipt.forbid_statement_history_change()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION '% on %.% is not allowed: history is append-only',
        TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER trg_statement_revisions_append_only
    BEFORE UPDATE OR DELETE ON receipt.statement_revisions
    FOR EACH ROW EXECUTE FUNCTION receipt.forbid_statement_history_change();
CREATE OR REPLACE TRIGGER trg_statement_revisions_no_truncate
    BEFORE TRUNCATE ON receipt.statement_revisions
    FOR EACH STATEMENT EXECUTE FUNCTION receipt.forbid_statement_history_change();
CREATE OR REPLACE TRIGGER trg_statement_accounts_append_only
    BEFORE UPDATE OR DELETE ON receipt.statement_accounts
    FOR EACH ROW EXECUTE FUNCTION receipt.forbid_statement_history_change();
CREATE OR REPLACE TRIGGER trg_statement_accounts_no_truncate
    BEFORE TRUNCATE ON receipt.statement_accounts
    FOR EACH STATEMENT EXECUTE FUNCTION receipt.forbid_statement_history_change();
CREATE OR REPLACE TRIGGER trg_statement_lines_append_only
    BEFORE UPDATE OR DELETE ON receipt.statement_lines
    FOR EACH ROW EXECUTE FUNCTION receipt.forbid_statement_history_change();
CREATE OR REPLACE TRIGGER trg_statement_lines_no_truncate
    BEFORE TRUNCATE ON receipt.statement_lines
    FOR EACH STATEMENT EXECUTE FUNCTION receipt.forbid_statement_history_change();

-- A revision's number comes from the counter, whatever the insert says.
-- Its created_at is the transaction's start, so its accounts can tell that
-- they are written in the same transaction (see below).
CREATE OR REPLACE FUNCTION receipt.set_statement_revision_number()
RETURNS TRIGGER AS $$
BEGIN
    NEW.revision := receipt.next_feed_value();
    NEW.created_at := transaction_timestamp();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER trg_statement_revisions_number
    BEFORE INSERT ON receipt.statement_revisions
    FOR EACH ROW EXECUTE FUNCTION receipt.set_statement_revision_number();

-- A statement's place in the feed comes from the counter, whatever the
-- write says: on insert, when its current revision changes, and when a
-- republish increments publish_count. Any other update keeps it. The
-- statement's identity and original never change, and publish_count only
-- goes up.
CREATE OR REPLACE FUNCTION receipt.set_statement_change_seq()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.publish_count := 0;
        NEW.change_seq := receipt.next_feed_value();
        RETURN NEW;
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.original_file_id IS DISTINCT FROM OLD.original_file_id
       OR NEW.source_sha256 IS DISTINCT FROM OLD.source_sha256 THEN
        RAISE EXCEPTION 'statement %: its id and original never change', OLD.id;
    END IF;
    IF NEW.publish_count NOT IN (OLD.publish_count, OLD.publish_count + 1) THEN
        RAISE EXCEPTION 'statement %: publish_count only goes up by one', OLD.id;
    END IF;
    IF NEW.current_revision_id IS DISTINCT FROM OLD.current_revision_id
       OR NEW.publish_count IS DISTINCT FROM OLD.publish_count THEN
        NEW.change_seq := receipt.next_feed_value();
    ELSE
        NEW.change_seq := OLD.change_seq;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER trg_statements_change_seq
    BEFORE INSERT OR UPDATE ON receipt.statements
    FOR EACH ROW EXECUTE FUNCTION receipt.set_statement_change_seq();

CREATE OR REPLACE TRIGGER trg_statements_updated_at
    BEFORE UPDATE ON receipt.statements
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- At commit, a statement's current revision is one of its own, and its
-- copied status and type match that revision's.
CREATE OR REPLACE FUNCTION receipt.check_statement_current_revision()
RETURNS TRIGGER AS $$
DECLARE
    rev RECORD;
BEGIN
    SELECT statement_id, revision, status, document_type INTO rev
    FROM receipt.statement_revisions
    WHERE id = NEW.current_revision_id;
    IF NOT FOUND THEN
        RETURN NULL;  -- the deferred foreign key reports it
    END IF;
    IF rev.statement_id <> NEW.id THEN
        RAISE EXCEPTION 'statement %: current revision % belongs to statement %',
            NEW.id, NEW.current_revision_id, rev.statement_id;
    END IF;
    IF rev.status <> NEW.status OR rev.document_type <> NEW.document_type THEN
        RAISE EXCEPTION
            'statement %: status % and type % don''t match its current revision (% and %)',
            NEW.id, NEW.status, NEW.document_type, rev.status, rev.document_type;
    END IF;
    -- The current revision only moves forward: it is the statement's newest.
    -- (A restore brings back earlier revisions with the rows themselves,
    -- not through an update.)
    IF rev.revision < (SELECT max(revision) FROM receipt.statement_revisions
                       WHERE statement_id = NEW.id) THEN
        RAISE EXCEPTION 'statement %: current revision % is not its newest',
            NEW.id, NEW.current_revision_id;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

-- At commit, a new revision is its statement's current revision: every run
-- is published, and none is left behind that would make the statement's
-- current revision look older than its newest. So one transaction writes at
-- most one revision per statement: runs can't be batched.
CREATE OR REPLACE FUNCTION receipt.check_statement_revision_is_current()
RETURNS TRIGGER AS $$
DECLARE
    current_id UUID;
BEGIN
    SELECT current_revision_id INTO current_id
    FROM receipt.statements WHERE id = NEW.statement_id;
    IF NOT FOUND THEN
        RETURN NULL;  -- the deferred foreign key reports it
    END IF;
    IF current_id <> NEW.id THEN
        RAISE EXCEPTION 'revision % of statement % was written but not made current',
            NEW.id, NEW.statement_id;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER trg_statement_revisions_current
    AFTER INSERT ON receipt.statement_revisions
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION receipt.check_statement_revision_is_current();

-- At commit, a held or rejected revision passes no usable account, and only
-- a held revision has accounts held by the statement. The ledger imports a
-- clean account's lines whatever the statement's status, so these are the
-- rules a wrong status could break silently. Whether a usable revision is
-- complete or incomplete is the gates' decision (U4), not checked here.
-- A held revision with no accounts at all is legitimate: a statement that
-- lists no accounts is held with hold_reason no_accounts (tested by
-- test_a_revision_status_that_agrees_with_its_accounts_commits[no-accounts]).
CREATE OR REPLACE FUNCTION receipt.check_statement_revision_accounts()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.status IN ('held', 'rejected') AND EXISTS (
        SELECT 1 FROM receipt.statement_accounts
        WHERE revision_id = NEW.id AND status IN ('clean', 'figures_only')
    ) THEN
        RAISE EXCEPTION 'revision % is % but has a usable account',
            NEW.id, NEW.status;
    END IF;
    IF NEW.status <> 'held' AND EXISTS (
        SELECT 1 FROM receipt.statement_accounts
        WHERE revision_id = NEW.id AND held_by_statement
    ) THEN
        RAISE EXCEPTION 'revision % is % but has an account held by the statement',
            NEW.id, NEW.status;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER trg_statement_revisions_accounts
    AFTER INSERT ON receipt.statement_revisions
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION receipt.check_statement_revision_accounts();

CREATE CONSTRAINT TRIGGER trg_statements_current_revision
    AFTER INSERT OR UPDATE ON receipt.statements
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION receipt.check_statement_current_revision();

-- A revision is complete when its transaction commits: its accounts, and
-- their lines, are written in the same transaction as the revision, so a
-- later write can't change what the feed has already published. A row
-- written in this transaction has created_at equal to the transaction's
-- start, which the triggers force (savepoints don't change it). Two
-- transactions that start in the same microsecond share that value; one of
-- them would also need the other's revision id, so the check accepts that
-- remote case rather than store a transaction id on every row. A rejected
-- revision has no accounts, and only a clean account has lines.
CREATE OR REPLACE FUNCTION receipt.check_statement_account_revision()
RETURNS TRIGGER AS $$
DECLARE
    rev RECORD;
BEGIN
    SELECT status, created_at INTO rev
    FROM receipt.statement_revisions WHERE id = NEW.revision_id;
    -- A revision written in this transaction is always visible to it. One
    -- that isn't is missing, or another transaction's still uncommitted.
    IF NOT FOUND THEN
        RAISE EXCEPTION 'revision % is not one this transaction wrote',
            NEW.revision_id;
    END IF;
    IF rev.created_at <> transaction_timestamp() THEN
        RAISE EXCEPTION 'revision % is committed and takes no more accounts',
            NEW.revision_id;
    END IF;
    IF rev.status = 'rejected' THEN
        RAISE EXCEPTION 'revision % is a rejection and has no accounts',
            NEW.revision_id;
    END IF;
    NEW.created_at := transaction_timestamp();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER trg_statement_accounts_revision
    BEFORE INSERT ON receipt.statement_accounts
    FOR EACH ROW EXECUTE FUNCTION receipt.check_statement_account_revision();

CREATE OR REPLACE FUNCTION receipt.check_statement_line_account()
RETURNS TRIGGER AS $$
DECLARE
    acct RECORD;
BEGIN
    SELECT status, created_at INTO acct
    FROM receipt.statement_accounts WHERE id = NEW.account_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'account % is not one this transaction wrote',
            NEW.account_id;
    END IF;
    IF acct.created_at <> transaction_timestamp() THEN
        RAISE EXCEPTION 'account % is committed and takes no more lines',
            NEW.account_id;
    END IF;
    IF acct.status <> 'clean' THEN
        RAISE EXCEPTION 'account % is not clean and carries no lines',
            NEW.account_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER trg_statement_lines_account
    BEFORE INSERT ON receipt.statement_lines
    FOR EACH ROW EXECUTE FUNCTION receipt.check_statement_line_account();

-- _all: grants for these tables and the counter are in 000024.
