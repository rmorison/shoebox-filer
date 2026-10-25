"""End-to-end integration tests for the receipt ingestion pipeline.

Requires Docker services: Postgres (port 15432) and GreenMail (ports 3025/3143).
Start with: docker compose up -d
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from receipt_index.adapters.imap import ImapAdapter
from receipt_index.config import ImapSourceConfig
from receipt_index.models import ReceiptMetadata
from receipt_index.pipeline import run_ingest
from receipt_index.repository import get_processed_source_ids, search_receipts
from receipt_index.store import LocalFileStore

from .conftest import GREENMAIL_IMAP_PORT, seed_email

if TYPE_CHECKING:
    from pathlib import Path

    import psycopg


def _make_mock_agent(
    vendor: str = "TestVendor",
    amount: Decimal = Decimal("42.99"),
    receipt_date: date | None = None,
    confidence: float = 0.95,
) -> MagicMock:
    """Create a mock extraction agent that returns fixed metadata."""
    meta = ReceiptMetadata(
        vendor=vendor,
        amount=amount,
        date=receipt_date or date(2025, 6, 15),
        confidence=confidence,
    )
    mock_usage = MagicMock()
    mock_usage.input_tokens = 100
    mock_usage.output_tokens = 50
    mock_usage.cache_read_tokens = 0
    mock_usage.requests = 1

    mock_result = MagicMock()
    mock_result.output = meta
    mock_result.usage.return_value = mock_usage

    agent = MagicMock()
    agent.run_sync.return_value = mock_result
    return agent


def _unique_user(prefix: str = "test") -> str:
    """Generate a unique GreenMail user address to avoid cross-run collisions."""
    short_id = uuid.uuid4().hex[:8]
    return f"{prefix}-{short_id}@localhost"


def _greenmail_imap_config(user: str = "test@localhost") -> ImapSourceConfig:
    """IMAP config pointing at GreenMail."""
    return ImapSourceConfig(
        name="greenmail-test",
        host="localhost",
        username=user,
        password="any",  # pragma: allowlist secret
        port=GREENMAIL_IMAP_PORT,
        folder="INBOX",
        use_ssl=False,
    )


@pytest.mark.integration
class TestFullPipelineE2E:
    """End-to-end pipeline tests using GreenMail + Postgres."""

    def test_happy_path(
        self,
        greenmail_available: bool,
        pg_conn: psycopg.Connection[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        """Seed emails via SMTP, run pipeline, verify DB and file store."""
        user = _unique_user("happy")
        seed_email(
            to_addr=user,
            from_addr="shop@example.com",
            subject="Your Order #12345",
            html_body="<p>Total: $42.99</p>",
        )

        adapter = ImapAdapter(_greenmail_imap_config(user))
        store = LocalFileStore(tmp_path)
        agent = _make_mock_agent()

        result = run_ingest(
            conn=pg_conn,
            adapter=adapter,
            store=store,
            source_name="greenmail-test",
            source_type="imap",
            agent=agent,
        )

        assert result.processed == 1
        assert result.failed == 0
        assert len(result.receipts) == 1

        receipt = result.receipts[0]
        assert receipt.vendor == "TestVendor"
        assert receipt.amount == Decimal("42.99")
        assert receipt.email_subject == "Your Order #12345"

        # Verify file was saved
        assert store.exists(receipt.pdf_path)

        # Verify DB record
        rows = search_receipts(pg_conn, vendor="TestVendor")
        assert len(rows) == 1
        assert rows[0].source_id == receipt.source_id

    def test_idempotent_reingestion(
        self,
        greenmail_available: bool,
        pg_conn: psycopg.Connection[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        """Running the pipeline twice on the same emails should not duplicate."""
        user = _unique_user("idempotent")
        seed_email(
            to_addr=user,
            from_addr="shop@example.com",
            subject="Receipt A",
            text_body="Amount: $10.00",
        )

        config = _greenmail_imap_config(user)
        store = LocalFileStore(tmp_path)
        agent = _make_mock_agent(vendor="ShopA", amount=Decimal("10.00"))

        # First run
        r1 = run_ingest(
            conn=pg_conn,
            adapter=ImapAdapter(config),
            store=store,
            source_name="greenmail-test",
            source_type="imap",
            agent=agent,
        )
        assert r1.processed == 1

        # Second run — same email should be skipped
        r2 = run_ingest(
            conn=pg_conn,
            adapter=ImapAdapter(config),
            store=store,
            source_name="greenmail-test",
            source_type="imap",
            agent=agent,
        )
        assert r2.processed == 0
        assert r2.skipped == 0  # not dry_run, just no unprocessed messages

        # Still only one row in DB
        rows = search_receipts(pg_conn, vendor="ShopA")
        assert len(rows) == 1

    def test_dry_run_skips_processing(
        self,
        greenmail_available: bool,
        pg_conn: psycopg.Connection[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        """Dry run should not insert into DB or save files."""
        user = _unique_user("dryrun")
        seed_email(
            to_addr=user,
            from_addr="shop@example.com",
            subject="Dry Run Receipt",
            text_body="Amount: $5.00",
        )

        adapter = ImapAdapter(_greenmail_imap_config(user))
        store = LocalFileStore(tmp_path)

        result = run_ingest(
            conn=pg_conn,
            adapter=adapter,
            store=store,
            source_name="greenmail-test",
            source_type="imap",
            agent=_make_mock_agent(),
            dry_run=True,
        )

        assert result.skipped == 1
        assert result.processed == 0

        # DB should be empty
        ids = get_processed_source_ids(pg_conn)
        assert len(ids) == 0

    def test_multiple_emails(
        self,
        greenmail_available: bool,
        pg_conn: psycopg.Connection[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        """Pipeline processes multiple emails from the same mailbox."""
        user = _unique_user("multi")
        for i in range(3):
            seed_email(
                to_addr=user,
                from_addr=f"vendor{i}@example.com",
                subject=f"Receipt {i}",
                text_body=f"Amount: ${10 + i}.00",
            )

        adapter = ImapAdapter(_greenmail_imap_config(user))
        store = LocalFileStore(tmp_path)
        agent = _make_mock_agent()

        result = run_ingest(
            conn=pg_conn,
            adapter=adapter,
            store=store,
            source_name="greenmail-test",
            source_type="imap",
            agent=agent,
        )

        assert result.processed == 3
        assert result.failed == 0
        assert len(result.receipts) == 3

    def test_limit_caps_processing(
        self,
        greenmail_available: bool,
        pg_conn: psycopg.Connection[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        """The limit parameter should cap how many emails are processed."""
        user = _unique_user("limit")
        for i in range(5):
            seed_email(
                to_addr=user,
                from_addr=f"vendor{i}@example.com",
                subject=f"Limit Receipt {i}",
                text_body=f"Amount: ${20 + i}.00",
            )

        adapter = ImapAdapter(_greenmail_imap_config(user))
        store = LocalFileStore(tmp_path)
        agent = _make_mock_agent()

        result = run_ingest(
            conn=pg_conn,
            adapter=adapter,
            store=store,
            source_name="greenmail-test",
            source_type="imap",
            agent=agent,
            limit=2,
        )

        assert result.processed == 2

    @patch(
        "receipt_index.extraction._extract_pdf_text",
        return_value="Invoice Total: $199.99",
    )
    def test_email_with_pdf_attachment(
        self,
        _mock_pdf: MagicMock,
        greenmail_available: bool,
        pg_conn: psycopg.Connection[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        """Pipeline handles emails with PDF attachments."""
        user = _unique_user("pdfattach")
        fake_pdf = b"%PDF-1.4 fake pdf content for testing"
        seed_email(
            to_addr=user,
            from_addr="billing@example.com",
            subject="Invoice #9876",
            text_body="See attached invoice.",
            pdf_attachment=("invoice.pdf", fake_pdf),
        )

        adapter = ImapAdapter(_greenmail_imap_config(user))
        store = LocalFileStore(tmp_path)
        agent = _make_mock_agent(vendor="BillingCo", amount=Decimal("199.99"))

        result = run_ingest(
            conn=pg_conn,
            adapter=adapter,
            store=store,
            source_name="greenmail-test",
            source_type="imap",
            agent=agent,
        )

        assert result.processed == 1
        receipt = result.receipts[0]
        assert receipt.vendor == "BillingCo"
        assert store.exists(receipt.pdf_path)

    def test_search_filters_on_seeded_data(
        self,
        greenmail_available: bool,
        pg_conn: psycopg.Connection[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        """Search filters work against ingested data."""
        user = _unique_user("search")
        seed_email(
            to_addr=user,
            from_addr="acme@example.com",
            subject="ACME Order",
            text_body="Total: $100.00",
        )
        seed_email(
            to_addr=user,
            from_addr="globex@example.com",
            subject="Globex Invoice",
            text_body="Total: $200.00",
        )

        adapter = ImapAdapter(_greenmail_imap_config(user))
        store = LocalFileStore(tmp_path)

        # Mock agent returns different vendor for each call
        meta1 = ReceiptMetadata(
            vendor="ACME Corp",
            amount=Decimal("100.00"),
            date=date(2025, 3, 1),
            confidence=0.9,
        )
        meta2 = ReceiptMetadata(
            vendor="Globex Inc",
            amount=Decimal("200.00"),
            date=date(2025, 3, 2),
            confidence=0.85,
        )

        def _mock_result(meta: ReceiptMetadata) -> MagicMock:
            usage = MagicMock()
            usage.input_tokens = 100
            usage.output_tokens = 50
            usage.cache_read_tokens = 0
            usage.requests = 1
            r = MagicMock()
            r.output = meta
            r.usage.return_value = usage
            return r

        agent = MagicMock()
        agent.run_sync.side_effect = [_mock_result(meta1), _mock_result(meta2)]

        run_ingest(
            conn=pg_conn,
            adapter=adapter,
            store=store,
            source_name="greenmail-test",
            source_type="imap",
            agent=agent,
        )

        # Search by vendor
        acme = search_receipts(pg_conn, vendor="ACME")
        assert len(acme) == 1
        assert acme[0].vendor == "ACME Corp"

        # Search by amount range
        over_150 = search_receipts(pg_conn, amount_min=Decimal("150.00"))
        assert len(over_150) == 1
        assert over_150[0].vendor == "Globex Inc"

        # Search by date range
        march = search_receipts(
            pg_conn,
            date_from=date(2025, 3, 1),
            date_to=date(2025, 3, 31),
        )
        assert len(march) == 2
