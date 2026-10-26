"""Integration tests for the paid amount and the effective amount.

Ingest runs through GreenMail and the devtest Postgres with a mocked LLM, so
these tests prove that the paid amount and field confidence are stored and
searched, not that a model extracts them correctly. Vendors and amounts are
placeholders.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import psycopg
import pytest

from receipt_index.adapters.imap import ImapAdapter
from receipt_index.config import ImapSourceConfig
from receipt_index.models import FieldConfidence, ReceiptMetadata
from receipt_index.pipeline import run_ingest
from receipt_index.repository import count_receipts, insert_receipt, search_receipts
from receipt_index.store import LocalEvidenceStore

from .conftest import GREENMAIL_IMAP_PORT, seed_email

if TYPE_CHECKING:
    from pathlib import Path

    from receipt_index.models import Receipt

# Fixture receipts where the paid amount and the total differ.
_GIFT_CARD_SUBJECT = "Your parking receipt"
_GIFT_CARD_BODY = """\
Acme Parking (U.S.), LLC
Date: 2025-06-15
Monthly parking                 $50.00
Order total                     $50.00
Gift card applied              -$20.00
Charged to card ending 0000     $30.00
"""
_UNPAID_SUBJECT = "Invoice INV-0001 from Example Services"
_UNPAID_BODY = """\
Example Services
Invoice INV-0001
Invoice date: 2025-06-20
Consulting                     $120.00
Invoice total                  $120.00
Amount paid                      $0.00
Balance due                    $120.00
Status: unpaid
"""

_EXTRACTIONS = {
    _GIFT_CARD_SUBJECT: ReceiptMetadata(
        vendor="Acme Parking (U.S.), LLC",
        amount=Decimal("50.00"),
        paid_amount=Decimal("30.00"),
        date=date(2025, 6, 15),
        description="Monthly parking; 20.00 paid with a gift card",
        confidence=0.95,
        field_confidence=FieldConfidence(
            vendor=0.95, amount=0.95, paid_amount=0.9, date=0.95
        ),
    ),
    _UNPAID_SUBJECT: ReceiptMetadata(
        vendor="Example Services",
        amount=Decimal("120.00"),
        paid_amount=Decimal("0"),
        date=date(2025, 6, 20),
        description="Consulting invoice, unpaid",
        confidence=0.9,
        field_confidence=FieldConfidence(
            vendor=0.95, amount=0.95, paid_amount=0.85, date=0.95
        ),
    ),
}


def _agent_for_fixtures() -> MagicMock:
    """A mock agent that answers from the subject line in the prompt."""

    def run_sync(prompt: str) -> MagicMock:
        (meta,) = [m for s, m in _EXTRACTIONS.items() if f"Subject: {s}" in prompt]
        usage = MagicMock()
        usage.input_tokens = 100
        usage.output_tokens = 60
        usage.cache_read_tokens = 0
        usage.requests = 1
        result = MagicMock()
        result.output = meta
        result.usage.return_value = usage
        return result

    agent = MagicMock()
    agent.run_sync.side_effect = run_sync
    return agent


def _imap_config(user: str) -> ImapSourceConfig:
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
class TestIngestStoresPaidAmount:
    def test_fixture_receipts_store_paid_amount_and_confidence(
        self,
        greenmail_available: bool,
        pg_conn: psycopg.Connection[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        user = f"paid-{uuid.uuid4().hex[:8]}@localhost"
        seed_email(
            to_addr=user,
            from_addr="billing@example.com",
            subject=_GIFT_CARD_SUBJECT,
            text_body=_GIFT_CARD_BODY,
        )
        seed_email(
            to_addr=user,
            from_addr="billing@example.com",
            subject=_UNPAID_SUBJECT,
            text_body=_UNPAID_BODY,
        )

        result = run_ingest(
            conn=pg_conn,
            adapter=ImapAdapter(_imap_config(user)),
            store=LocalEvidenceStore(tmp_path),
            source_name="greenmail-test",
            source_type="imap",
            agent=_agent_for_fixtures(),
            llm_model="test-model",
        )
        assert (result.processed, result.failed) == (2, 0)

        rows = pg_conn.execute(
            "SELECT r.vendor, r.amount, r.paid_amount, r.field_confidence,"
            " l.amount AS log_amount, l.paid_amount AS log_paid_amount"
            " FROM receipt.receipts r"
            " JOIN receipt.ingest_log l ON l.receipt_id = r.id"
            " ORDER BY r.receipt_date"
        ).fetchall()
        assert [
            (r["vendor"], r["amount"], r["paid_amount"], r["log_paid_amount"])
            for r in rows
        ] == [
            (
                "Acme Parking (U.S.), LLC",
                Decimal("50.00"),
                Decimal("30.00"),
                Decimal("30.00"),
            ),
            ("Example Services", Decimal("120.00"), Decimal("0.00"), Decimal("0.00")),
        ]
        assert rows[0]["field_confidence"] == {
            "vendor": 0.95,
            "amount": 0.95,
            "paid_amount": 0.9,
            "date": 0.95,
        }
        assert rows[1]["log_amount"] == Decimal("120.00")

        unpaid = search_receipts(pg_conn, vendor="Example Services")
        assert unpaid[0].effective_amount == Decimal("0.00")


def _insert(
    conn: psycopg.Connection[dict[str, Any]],
    vendor: str,
    amount: str,
    paid_amount: str | None,
) -> Receipt:
    return insert_receipt(
        conn,
        source_id=f"<{uuid.uuid4().hex}@example.com>",
        source_type="imap",
        source_name="test-email",
        vendor=vendor,
        amount=Decimal(amount),
        paid_amount=None if paid_amount is None else Decimal(paid_amount),
        currency="USD",
        receipt_date=date(2025, 6, 15),
        description=None,
        confidence=0.9,
        pdf_path="2025/06/x.pdf",
        email_subject=None,
        email_sender=None,
        email_date=None,
    )


@pytest.fixture()
def three_receipts(pg_conn: psycopg.Connection[dict[str, Any]]) -> None:
    _insert(pg_conn, "Gift Card Vendor", "50.00", "30.00")
    _insert(pg_conn, "Unpaid Vendor", "120.00", "0")
    _insert(pg_conn, "Unknown Vendor", "75.00", None)


def _vendors(rows: list[Receipt]) -> list[str]:
    return sorted(r.vendor for r in rows)


@pytest.mark.integration
@pytest.mark.usefixtures("three_receipts")
class TestSearchOnEffectiveAmount:
    def test_exact_amount_matches_paid_not_total(
        self, pg_conn: psycopg.Connection[dict[str, Any]]
    ) -> None:
        assert _vendors(search_receipts(pg_conn, amount=Decimal("30"))) == [
            "Gift Card Vendor"
        ]
        assert search_receipts(pg_conn, amount=Decimal("50")) == []

    def test_unpaid_invoice_matches_zero(
        self, pg_conn: psycopg.Connection[dict[str, Any]]
    ) -> None:
        assert _vendors(search_receipts(pg_conn, amount=Decimal("0"))) == [
            "Unpaid Vendor"
        ]
        assert search_receipts(pg_conn, amount=Decimal("120")) == []

    def test_unknown_paid_amount_falls_back_to_total(
        self, pg_conn: psycopg.Connection[dict[str, Any]]
    ) -> None:
        (row,) = search_receipts(pg_conn, amount=Decimal("75"))
        assert row.vendor == "Unknown Vendor"
        assert row.paid_amount is None
        assert row.effective_amount == Decimal("75.00")

    def test_range_uses_effective_amount(
        self, pg_conn: psycopg.Connection[dict[str, Any]]
    ) -> None:
        filters: dict[str, Any] = {
            "amount_min": Decimal("10"),
            "amount_max": Decimal("60"),
        }
        assert _vendors(search_receipts(pg_conn, **filters)) == ["Gift Card Vendor"]
        assert count_receipts(pg_conn, **filters) == 1
        assert _vendors(search_receipts(pg_conn, amount_min=Decimal("70"))) == [
            "Unknown Vendor"
        ]


@pytest.mark.integration
class TestPaidAmountConstraint:
    def test_negative_paid_amount_rejected(
        self, pg_conn: psycopg.Connection[dict[str, Any]]
    ) -> None:
        with pytest.raises(psycopg.errors.CheckViolation) as exc:
            _insert(pg_conn, "Negative Vendor", "10.00", "-1.00")
        pg_conn.rollback()
        assert "ck_receipts_paid_amount_non_negative" in str(exc.value)

    def test_existing_style_row_defaults(
        self, pg_conn: psycopg.Connection[dict[str, Any]]
    ) -> None:
        # A row written without the new columns, as before the migration.
        row = pg_conn.execute(
            "INSERT INTO receipt.receipts (source_id, vendor, amount, receipt_date,"
            " confidence, pdf_path) VALUES ('<old@example.com>', 'Old Vendor',"
            " 12.34, '2024-01-01', 0.9, 'x.pdf')"
            " RETURNING paid_amount, field_confidence"
        ).fetchone()
        pg_conn.commit()
        assert row == {"paid_amount": None, "field_confidence": {}}
