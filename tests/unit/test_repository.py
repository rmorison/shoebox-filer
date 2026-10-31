"""Tests for receipt_index.repository."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

from psycopg.types.json import Jsonb

from receipt_index.repository import (
    count_ingest_failures,
    count_receipts,
    get_ingest_failures,
    get_processed_source_ids,
    get_receipt_by_id,
    insert_ingest_log,
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
            "SELECT source_id FROM receipt.provenance "
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
        assert "WHERE merged_into_id IS NULL ORDER" in sql
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
        assert "COALESCE(paid_amount, amount) = %(amount)s" in sql
        assert params["amount"] == Decimal("42.99")

    def test_amount_range_filter(self) -> None:
        conn = _mock_conn([])
        search_receipts(conn, amount_min=Decimal("10"), amount_max=Decimal("100"))
        args = conn.execute.call_args
        sql = args[0][0]
        params = args[0][1]
        assert "COALESCE(paid_amount, amount) >= %(amount_min)s" in sql
        assert "COALESCE(paid_amount, amount) <= %(amount_max)s" in sql
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
        assert "COALESCE(paid_amount, amount) >=" in sql
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


class TestSearchPaging:
    """Tests for limit/offset paging and counting on search."""

    def test_unpaged_by_default(self) -> None:
        conn = _mock_conn([])
        search_receipts(conn)
        sql, params = conn.execute.call_args.args
        assert "LIMIT" not in sql
        assert "OFFSET" not in sql
        assert "limit" not in params

    def test_order_has_id_tiebreak(self) -> None:
        conn = _mock_conn([])
        search_receipts(conn)
        sql = conn.execute.call_args.args[0]
        assert sql.rstrip().endswith("ORDER BY receipt_date DESC, vendor ASC, id ASC")

    def test_limit_and_offset(self) -> None:
        conn = _mock_conn([_RECEIPT_ROW])
        results = search_receipts(conn, vendor="acme", limit=10, offset=20)
        sql, params = conn.execute.call_args.args
        assert sql.endswith("id ASC LIMIT %(limit)s OFFSET %(offset)s")
        assert params["limit"] == 10
        assert params["offset"] == 20
        assert params["vendor"] == "%acme%"
        assert len(results) == 1

    def test_count_uses_same_filters(self) -> None:
        conn = _mock_conn([{"n": 7}])
        n = count_receipts(
            conn,
            vendor="acme",
            amount_min=Decimal("1.00"),
            date_to=date(2025, 6, 30),
            source_name="test-email",
        )
        sql, params = conn.execute.call_args.args
        assert n == 7
        assert sql.startswith("SELECT COUNT(*) AS n FROM receipt.receipts WHERE ")
        assert "vendor ILIKE %(vendor)s" in sql
        assert "COALESCE(paid_amount, amount) >= %(amount_min)s" in sql
        assert "receipt_date <= %(date_to)s" in sql
        assert "source_name = %(source_name)s" in sql
        assert params == {
            "vendor": "%acme%",
            "amount_min": Decimal("1.00"),
            "date_to": date(2025, 6, 30),
            "source_name": "test-email",
        }

    def test_count_without_filters(self) -> None:
        conn = _mock_conn([{"n": 0}])
        assert count_receipts(conn) == 0
        assert conn.execute.call_args.args[0].endswith("WHERE merged_into_id IS NULL")


class TestFailuresPaging:
    """Tests for limit/offset paging and counting on ingest failures."""

    def test_unpaged_by_default(self) -> None:
        conn = _mock_conn([])
        get_ingest_failures(conn)
        sql = conn.execute.call_args.args[0]
        assert "LIMIT" not in sql
        assert "ORDER BY created_at DESC, id ASC" in sql

    def test_limit_and_offset(self) -> None:
        conn = _mock_conn([])
        get_ingest_failures(conn, limit=5, offset=10)
        sql, params = conn.execute.call_args.args
        assert sql.endswith("LIMIT %(limit)s OFFSET %(offset)s")
        assert params == {"limit": 5, "offset": 10}

    def test_count(self) -> None:
        conn = _mock_conn([{"n": 3}])
        assert count_ingest_failures(conn) == 3
        assert "status = 'failed'" in conn.execute.call_args.args[0]


_INSERT_ARGS: dict[str, Any] = {
    "source_id": "<msg-2@example.com>",
    "source_type": "imap",
    "source_name": "test-email",
    "vendor": "Acme Parking (U.S.), LLC",
    "amount": Decimal("50.00"),
    "currency": "USD",
    "receipt_date": date(2025, 6, 15),
    "description": None,
    "confidence": 0.9,
    "pdf_path": "path.pdf",
    "email_subject": None,
    "email_sender": None,
    "email_date": None,
}


class TestPaidAmountWrites:
    """insert_receipt and insert_ingest_log write the paid amount."""

    def test_insert_receipt_writes_paid_amount_and_field_confidence(self) -> None:
        conn = _mock_conn([_RECEIPT_ROW])
        insert_receipt(
            conn,
            **_INSERT_ARGS,
            paid_amount=Decimal("30.00"),
            field_confidence={"vendor": 0.9, "paid_amount": 0.6},
        )
        sql, params = conn.execute.call_args.args
        assert "%(paid_amount)s" in sql
        assert "%(field_confidence)s" in sql
        assert params["paid_amount"] == Decimal("30.00")
        assert isinstance(params["field_confidence"], Jsonb)
        assert params["field_confidence"].obj == {"vendor": 0.9, "paid_amount": 0.6}

    def test_insert_receipt_defaults_to_unknown_paid_amount(self) -> None:
        conn = _mock_conn([_RECEIPT_ROW])
        insert_receipt(conn, **_INSERT_ARGS)
        params = conn.execute.call_args.args[1]
        assert params["paid_amount"] is None
        assert params["field_confidence"].obj == {}

    def test_insert_ingest_log_writes_paid_amount(self) -> None:
        row = {
            "id": UUID("019572a0-0000-7000-8000-000000000002"),
            "source_id": "<msg-2@example.com>",
            "source_type": "imap",
            "status": "success",
            "receipt_id": None,
            "vendor": None,
            "amount": Decimal("50.00"),
            "paid_amount": Decimal("0"),
            "email_subject": None,
            "email_sender": None,
            "email_date": None,
            "error_message": None,
            "created_at": datetime(2025, 6, 15, tzinfo=UTC),
        }
        conn = _mock_conn([row])
        entry = insert_ingest_log(
            conn,
            source_id="<msg-2@example.com>",
            source_type="imap",
            status="success",
            amount=Decimal("50.00"),
            paid_amount=Decimal("0"),
        )
        sql, params = conn.execute.call_args.args
        assert "%(paid_amount)s" in sql
        assert params["paid_amount"] == Decimal("0")
        assert entry.paid_amount == Decimal("0")


class TestInsertIngestLogSourceName:
    """Every ingest_log row records which configured source it came from."""

    def test_source_name_is_written(self) -> None:
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = {
            "id": UUID("019572a0-0000-7000-8000-000000000009"),
            "source_id": "<a@example.com>",
            "source_type": "imap",
            "status": "started",
            "receipt_id": None,
            "vendor": None,
            "amount": None,
            "email_subject": None,
            "email_sender": None,
            "email_date": None,
            "error_message": None,
            "source_name": "personal-email",
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        }

        entry = insert_ingest_log(
            conn,
            source_id="<a@example.com>",
            source_type="imap",
            source_name="personal-email",
            status="started",
        )

        sql, params = conn.execute.call_args.args
        assert "source_name" in sql
        assert params["source_name"] == "personal-email"
        assert entry.status == "started"
        assert entry.source_name == "personal-email"
        conn.commit.assert_called_once()


class TestInsertExtraction:
    def test_inserts_without_committing(self) -> None:
        from receipt_index.repository import insert_extraction

        conn = _mock_conn()
        receipt_id = UUID("019572a0-0000-7000-8000-000000000001")
        insert_extraction(
            conn,
            receipt_id=receipt_id,
            origin="reextract",
            input_kind="rendition",
            prompt_version=2,
            llm_model="claude-test",
            output={"vendor": "Acme Parking (U.S.), LLC"},
            applied_fields=["paid_amount"],
            replaced={"paid_amount": None},
            llm_input_tokens=1200,
            llm_output_tokens=180,
            llm_cache_read_tokens=0,
            llm_requests=1,
        )
        sql, params = conn.execute.call_args[0]
        assert "INSERT INTO receipt.extractions" in sql
        assert params["receipt_id"] == receipt_id
        assert params["applied_fields"] == ["paid_amount"]
        assert params["output"].obj == {"vendor": "Acme Parking (U.S.), LLC"}
        assert params["replaced"].obj == {"paid_amount": None}
        assert params["input_file_id"] is None
        conn.commit.assert_not_called()

    def test_insert_receipt_can_leave_the_commit(self) -> None:
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
            confidence=0.95,
            pdf_path="2025/06/x.pdf",
            email_subject=None,
            email_sender=None,
            email_date=None,
            commit=False,
        )
        conn.commit.assert_not_called()
