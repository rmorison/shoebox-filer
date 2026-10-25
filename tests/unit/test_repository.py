"""Tests for receipt_index.repository."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

from receipt_index.repository import (
    get_processed_source_ids,
    get_receipt_by_id,
    insert_receipt,
    search_receipts,
)

_RECEIPT_ROW: dict[str, Any] = {
    "id": UUID("019572a0-0000-7000-8000-000000000001"),
    "source_id": "<msg-1@example.com>",
    "source_type": "imap",
    "source_name": "test-email",
    "vendor": "Amazon",
    "amount": Decimal("42.99"),
    "currency": "USD",
    "receipt_date": date(2025, 6, 15),
    "description": "Python Cookbook",
    "confidence": 0.95,
    "pdf_path": "2025/06/2025-06-15__amazon__42.99.pdf",
    "email_subject": "Your Amazon.com order",
    "email_sender": "no-reply@amazon.com",
    "email_date": datetime(2025, 6, 15, 10, 30, 0, tzinfo=UTC),
    "file_name": None,
    "created_at": datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC),
    "updated_at": datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC),
}


def _mock_conn(rows: list[dict[str, Any]] | None = None) -> MagicMock:
    """Create a mock psycopg Connection with cursor returning given rows."""
    conn = MagicMock()
    cursor = MagicMock()
    conn.execute.return_value = cursor

    if rows is not None:
        cursor.fetchall.return_value = rows
        cursor.fetchone.return_value = rows[0] if rows else None
    else:
        cursor.fetchall.return_value = []
        cursor.fetchone.return_value = None

    return conn


class TestGetProcessedSourceIds:
    """Tests for get_processed_source_ids."""

    def test_returns_source_ids(self) -> None:
        conn = _mock_conn(
            [{"source_id": "<msg-1@ex.com>"}, {"source_id": "<msg-2@ex.com>"}]
        )
        result = get_processed_source_ids(conn)
        assert result == {"<msg-1@ex.com>", "<msg-2@ex.com>"}

    def test_empty_table(self) -> None:
        conn = _mock_conn([])
        result = get_processed_source_ids(conn)
        assert result == set()

    def test_executes_correct_sql(self) -> None:
        conn = _mock_conn([])
        get_processed_source_ids(conn)
        conn.execute.assert_called_once_with(
            "SELECT source_id FROM receipt.receipts "
            "UNION "
            "SELECT source_id FROM receipt.ingest_log"
        )


class TestInsertReceipt:
    """Tests for insert_receipt."""

    def test_returns_validated_receipt(self) -> None:
        conn = _mock_conn([_RECEIPT_ROW])
        result = insert_receipt(
            conn,
            source_id="<msg-1@example.com>",
            source_type="imap",
            source_name="test-email",
            vendor="Amazon",
            amount=Decimal("42.99"),
            currency="USD",
            receipt_date=date(2025, 6, 15),
            description="Python Cookbook",
            confidence=0.95,
            pdf_path="2025/06/2025-06-15__amazon__42.99.pdf",
            email_subject="Your Amazon.com order",
            email_sender="no-reply@amazon.com",
            email_date=datetime(2025, 6, 15, 10, 30, 0, tzinfo=UTC),
        )
        assert result.vendor == "Amazon"
        assert result.amount == Decimal("42.99")
        assert result.id == UUID("019572a0-0000-7000-8000-000000000001")

    def test_commits_after_insert(self) -> None:
        conn = _mock_conn([_RECEIPT_ROW])
        insert_receipt(
            conn,
            source_id="<msg-1@example.com>",
            source_type="imap",
            source_name="test-email",
            vendor="Amazon",
            amount=Decimal("42.99"),
            currency="USD",
            receipt_date=date(2025, 6, 15),
            description=None,
            confidence=0.9,
            pdf_path="path.pdf",
            email_subject=None,
            email_sender=None,
            email_date=None,
        )
        conn.commit.assert_called_once()

    def test_uses_parameterized_query(self) -> None:
        conn = _mock_conn([_RECEIPT_ROW])
        insert_receipt(
            conn,
            source_id="<msg-1@example.com>",
            source_type="imap",
            source_name="test-email",
            vendor="Amazon",
            amount=Decimal("42.99"),
            currency="USD",
            receipt_date=date(2025, 6, 15),
            description=None,
            confidence=0.9,
            pdf_path="path.pdf",
            email_subject=None,
            email_sender=None,
            email_date=None,
        )
        args = conn.execute.call_args
        params = args[0][1]
        assert params["source_id"] == "<msg-1@example.com>"
        assert params["vendor"] == "Amazon"
        assert params["amount"] == Decimal("42.99")


class TestSearchReceipts:
    """Tests for search_receipts."""

    def test_no_filters(self) -> None:
        conn = _mock_conn([_RECEIPT_ROW])
        results = search_receipts(conn)
        sql = conn.execute.call_args[0][0]
        assert "WHERE TRUE" in sql
        assert "ORDER BY receipt_date DESC, vendor ASC" in sql
        assert len(results) == 1

    def test_vendor_filter(self) -> None:
        conn = _mock_conn([_RECEIPT_ROW])
        search_receipts(conn, vendor="amazon")
        args = conn.execute.call_args
        sql = args[0][0]
        params = args[0][1]
        assert "vendor ILIKE %(vendor)s" in sql
        assert params["vendor"] == "%amazon%"

    def test_exact_amount_filter(self) -> None:
        conn = _mock_conn([])
        search_receipts(conn, amount=Decimal("42.99"))
        args = conn.execute.call_args
        sql = args[0][0]
        params = args[0][1]
        assert "amount = %(amount)s" in sql
        assert params["amount"] == Decimal("42.99")

    def test_amount_range_filter(self) -> None:
        conn = _mock_conn([])
        search_receipts(conn, amount_min=Decimal("10"), amount_max=Decimal("100"))
        args = conn.execute.call_args
        sql = args[0][0]
        params = args[0][1]
        assert "amount >= %(amount_min)s" in sql
        assert "amount <= %(amount_max)s" in sql
        assert params["amount_min"] == Decimal("10")
        assert params["amount_max"] == Decimal("100")

    def test_date_range_filter(self) -> None:
        conn = _mock_conn([])
        search_receipts(
            conn,
            date_from=date(2025, 1, 1),
            date_to=date(2025, 12, 31),
        )
        args = conn.execute.call_args
        sql = args[0][0]
        params = args[0][1]
        assert "receipt_date >= %(date_from)s" in sql
        assert "receipt_date <= %(date_to)s" in sql
        assert params["date_from"] == date(2025, 1, 1)
        assert params["date_to"] == date(2025, 12, 31)

    def test_combined_filters(self) -> None:
        conn = _mock_conn([])
        search_receipts(
            conn,
            vendor="amazon",
            amount_min=Decimal("10"),
            date_from=date(2025, 1, 1),
        )
        args = conn.execute.call_args
        sql = args[0][0]
        assert "vendor ILIKE" in sql
        assert "amount >=" in sql
        assert "receipt_date >=" in sql
        assert " AND " in sql

    def test_returns_validated_receipts(self) -> None:
        conn = _mock_conn([_RECEIPT_ROW, _RECEIPT_ROW])
        results = search_receipts(conn)
        assert len(results) == 2
        assert all(isinstance(r, type(results[0])) for r in results)


class TestGetReceiptById:
    """Tests for get_receipt_by_id."""

    def test_found(self) -> None:
        conn = _mock_conn([_RECEIPT_ROW])
        uid = UUID("019572a0-0000-7000-8000-000000000001")
        result = get_receipt_by_id(conn, uid)
        assert result is not None
        assert result.id == uid

    def test_not_found(self) -> None:
        conn = _mock_conn()
        uid = UUID("019572a0-0000-7000-8000-000000000099")
        result = get_receipt_by_id(conn, uid)
        assert result is None

    def test_uses_parameterized_query(self) -> None:
        conn = _mock_conn()
        uid = UUID("019572a0-0000-7000-8000-000000000001")
        get_receipt_by_id(conn, uid)
        args = conn.execute.call_args
        assert "%(id)s" in args[0][0]
        assert args[0][1]["id"] == uid
