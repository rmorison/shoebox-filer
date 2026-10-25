"""Shared fixtures for integration tests."""

from __future__ import annotations

import os
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import TYPE_CHECKING, Any

import psycopg
import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator


# ---------------------------------------------------------------------------
# Postgres fixtures
# ---------------------------------------------------------------------------

# TODO: Tests run as superuser (main). Ideally bootstrap schema as main,
# then run pipeline tests as receipt_index_dev_write to catch missing GRANTs.
_DB_DSN = (
    "postgresql://main:localpass@localhost:"  # pragma: allowlist secret
    f"{os.environ.get('POSTGRES_HOST_PORT', '15432')}/receipt_index_dev"
)


def _run_migrations(conn: psycopg.Connection[dict[str, Any]]) -> None:
    """Apply migrations inline so tests don't need golang-migrate.

    Mirrors: public/000001, receipt/000001-000007.
    Keep in sync with db/migrations/ when schema changes.
    """
    # public/000001 — set_updated_at trigger function
    conn.execute(
        """\
        CREATE OR REPLACE FUNCTION public.set_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )

    # receipt/000001 — receipts table, indexes, trigger
    conn.execute("CREATE SCHEMA IF NOT EXISTS receipt")
    conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    conn.execute("DROP TABLE IF EXISTS receipt.receipts CASCADE")
    conn.execute(
        """\
        CREATE TABLE receipt.receipts (
            id              UUID NOT NULL DEFAULT uuidv7(),
            source_id       TEXT NOT NULL,
            source_type     TEXT NOT NULL DEFAULT 'imap',
            vendor          TEXT NOT NULL,
            amount          NUMERIC(12,2) NOT NULL,
            currency        TEXT NOT NULL DEFAULT 'USD',
            receipt_date    DATE NOT NULL,
            description     TEXT,
            confidence      REAL NOT NULL,
            pdf_path        TEXT NOT NULL,
            source_name     TEXT NOT NULL DEFAULT 'legacy-imap',
            email_subject   TEXT,
            email_sender    TEXT,
            email_date      TIMESTAMPTZ,
            file_name       TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            CONSTRAINT pk_receipts PRIMARY KEY (id),
            CONSTRAINT uq_receipts_source_id UNIQUE (source_id),
            CONSTRAINT ck_receipts_amount_non_negative CHECK (amount >= 0),
            CONSTRAINT ck_receipts_confidence_range
                CHECK (confidence >= 0 AND confidence <= 1)
        )
        """
    )
    conn.execute(
        "CREATE INDEX idx_receipts_vendor"
        " ON receipt.receipts USING gin (vendor gin_trgm_ops)"
    )
    conn.execute("CREATE INDEX idx_receipts_amount ON receipt.receipts (amount)")
    conn.execute(
        "CREATE INDEX idx_receipts_receipt_date ON receipt.receipts (receipt_date)"
    )
    conn.execute(
        """\
        CREATE OR REPLACE TRIGGER trg_receipts_set_updated_at
            BEFORE UPDATE ON receipt.receipts
            FOR EACH ROW
            EXECUTE FUNCTION public.set_updated_at()
        """
    )

    # receipt/000003 — ingest_log table and indexes
    conn.execute("DROP TABLE IF EXISTS receipt.ingest_log CASCADE")
    conn.execute(
        """\
        CREATE TABLE receipt.ingest_log (
            id              UUID NOT NULL DEFAULT uuidv7(),
            source_id       TEXT NOT NULL,
            source_type     TEXT NOT NULL DEFAULT 'imap',
            status          TEXT NOT NULL,
            receipt_id      UUID,
            vendor          TEXT,
            amount          NUMERIC(12,2),
            email_subject   TEXT,
            email_sender    TEXT,
            email_date      TIMESTAMPTZ,
            error_message   TEXT,
            llm_input_tokens   INTEGER,
            llm_output_tokens  INTEGER,
            llm_cache_read_tokens INTEGER,
            llm_requests       INTEGER,
            llm_model          TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            CONSTRAINT pk_ingest_log PRIMARY KEY (id),
            CONSTRAINT ck_ingest_log_status
                CHECK (status IN ('success', 'failed', 'skipped')),
            CONSTRAINT fk_ingest_log_receipt FOREIGN KEY (receipt_id)
                REFERENCES receipt.receipts (id) ON DELETE SET NULL
        )
        """
    )
    conn.execute("CREATE INDEX idx_ingest_log_status ON receipt.ingest_log (status)")
    conn.execute(
        "CREATE INDEX idx_ingest_log_source_id ON receipt.ingest_log (source_id)"
    )
    conn.execute(
        "CREATE INDEX idx_ingest_log_created_at ON receipt.ingest_log (created_at)"
    )
    conn.commit()


@pytest.fixture(scope="session")
def pg_conn() -> Iterator[psycopg.Connection[dict[str, Any]]]:
    """Session-scoped Postgres connection with schema bootstrapped."""
    try:
        conn = psycopg.connect(_DB_DSN, row_factory=psycopg.rows.dict_row)
    except psycopg.OperationalError:
        pytest.skip("Postgres not available at localhost:15432")

    _run_migrations(conn)
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def _truncate_receipts(pg_conn: psycopg.Connection[dict[str, Any]]) -> None:
    """Clear the receipts and ingest_log tables before each test."""
    pg_conn.rollback()  # clear any dangling transaction from a prior test failure
    pg_conn.execute("TRUNCATE receipt.ingest_log, receipt.receipts CASCADE")
    pg_conn.commit()


# ---------------------------------------------------------------------------
# GreenMail / IMAP fixtures
# ---------------------------------------------------------------------------

_GREENMAIL_SMTP_PORT = int(os.environ.get("GREENMAIL_SMTP_PORT", "3025"))
GREENMAIL_IMAP_PORT = int(os.environ.get("GREENMAIL_IMAP_PORT", "3143"))


def _smtp_available() -> bool:
    """Check if GreenMail SMTP is reachable."""
    import socket

    try:
        s = socket.create_connection(("localhost", _GREENMAIL_SMTP_PORT), timeout=2)
        s.close()
        return True
    except OSError:
        return False


def seed_email(
    *,
    to_addr: str = "test@localhost",
    from_addr: str = "sender@example.com",
    subject: str = "Test Receipt",
    text_body: str | None = None,
    html_body: str | None = None,
    pdf_attachment: tuple[str, bytes] | None = None,
) -> None:
    """Send a test email to GreenMail via SMTP.

    GreenMail auto-creates mailboxes on first delivery when
    auth is disabled.
    """
    if html_body or pdf_attachment:
        msg = MIMEMultipart("mixed")
        if text_body:
            msg.attach(MIMEText(text_body, "plain"))
        if html_body:
            msg.attach(MIMEText(html_body, "html"))
        if pdf_attachment:
            filename, data = pdf_attachment
            att = MIMEApplication(data, "pdf")
            att.add_header("Content-Disposition", "attachment", filename=filename)
            msg.attach(att)
    else:
        msg = MIMEText(text_body or "", "plain")  # type: ignore[assignment]

    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr

    with smtplib.SMTP("localhost", _GREENMAIL_SMTP_PORT) as smtp:
        smtp.send_message(msg)


@pytest.fixture(scope="session")
def greenmail_available() -> bool:
    """Check GreenMail is running; skip tests if not."""
    available = _smtp_available()
    if not available:
        pytest.skip(f"GreenMail not available at localhost:{_GREENMAIL_SMTP_PORT}")
    return available
