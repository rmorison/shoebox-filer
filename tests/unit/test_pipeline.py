"""Tests for receipt_index.pipeline."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID

from receipt_index.models import ExtractionResult, RawReceipt, Receipt, ReceiptMetadata
from receipt_index.pipeline import IngestResult, run_ingest

_RECEIPT_ROW: dict[str, Any] = {
    "id": UUID("019572a0-0000-7000-8000-000000000001"),
    "source_id": "<msg-1@example.com>",
    "source_type": "imap",
    "source_name": "personal-email",
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

_SAMPLE_RECEIPT = Receipt.model_validate(_RECEIPT_ROW)

_SAMPLE_METADATA = ExtractionResult(
    metadata=ReceiptMetadata(
        vendor="Amazon",
        amount=Decimal("42.99"),
        currency="USD",
        date=date(2025, 6, 15),
        description="Python Cookbook",
        confidence=0.95,
    ),
    input_tokens=150,
    output_tokens=50,
    cache_read_tokens=0,
    requests=1,
)


def _make_raw(
    source_id: str = "<msg-1@example.com>",
    source_name: str = "personal-email",
    source_type: str = "imap",
) -> RawReceipt:
    return RawReceipt(
        source_id=source_id,
        source_name=source_name,
        source_type=source_type,
        date=datetime(2025, 6, 15, 10, 30, 0, tzinfo=UTC),
        subject="Your Amazon.com order",
        sender="no-reply@amazon.com",
        text_body="Order Total: $42.99",
    )


def _mock_conn() -> MagicMock:
    conn = MagicMock()
    cursor = MagicMock()
    conn.execute.return_value = cursor
    cursor.fetchall.return_value = []
    cursor.fetchone.return_value = _RECEIPT_ROW
    return conn


def _mock_adapter(raws: list[RawReceipt]) -> MagicMock:
    adapter = MagicMock()
    adapter.fetch_unprocessed.return_value = iter(raws)
    return adapter


def _mock_store() -> MagicMock:
    store = MagicMock()
    store.save.return_value = "2025/06/2025-06-15__amazon__42.99.pdf"
    return store


class TestRunIngest:
    """Tests for run_ingest."""

    @patch("receipt_index.pipeline.insert_ingest_log")
    @patch("receipt_index.pipeline.insert_receipt", return_value=_SAMPLE_RECEIPT)
    @patch("receipt_index.pipeline.render_pdf", return_value=b"%PDF-fake")
    @patch("receipt_index.pipeline.extract_metadata", return_value=_SAMPLE_METADATA)
    @patch("receipt_index.pipeline.get_processed_source_ids", return_value=set())
    def test_processes_receipt(
        self,
        mock_get_ids: MagicMock,
        mock_extract: MagicMock,
        mock_render: MagicMock,
        mock_insert: MagicMock,
        mock_log: MagicMock,
    ) -> None:
        conn = _mock_conn()
        adapter = _mock_adapter([_make_raw()])
        store = _mock_store()

        result = run_ingest(
            conn=conn,
            adapter=adapter,
            store=store,
            source_name="test-imap",
            source_type="imap",
        )

        assert result.processed == 1
        assert result.skipped == 0
        assert result.failed == 0
        assert len(result.receipts) == 1
        mock_extract.assert_called_once()
        mock_render.assert_called_once()
        store.save.assert_called_once()
        mock_insert.assert_called_once()
        mock_log.assert_called_once()
        _, kwargs = mock_log.call_args
        assert kwargs["status"] == "success"

    @patch("receipt_index.pipeline.get_processed_source_ids", return_value=set())
    def test_dry_run_skips_processing(self, mock_get_ids: MagicMock) -> None:
        conn = _mock_conn()
        adapter = _mock_adapter([_make_raw(), _make_raw("<msg-2@example.com>")])
        store = _mock_store()

        result = run_ingest(
            conn=conn,
            adapter=adapter,
            store=store,
            source_name="test-imap",
            source_type="imap",
            dry_run=True,
        )

        assert result.processed == 0
        assert result.skipped == 2
        assert result.failed == 0

    @patch("receipt_index.pipeline.insert_ingest_log")
    @patch("receipt_index.pipeline.insert_receipt", return_value=_SAMPLE_RECEIPT)
    @patch("receipt_index.pipeline.render_pdf", return_value=b"%PDF-fake")
    @patch("receipt_index.pipeline.extract_metadata", return_value=_SAMPLE_METADATA)
    @patch("receipt_index.pipeline.get_processed_source_ids", return_value=set())
    def test_limit_caps_iteration(
        self,
        mock_get_ids: MagicMock,
        mock_extract: MagicMock,
        mock_render: MagicMock,
        mock_insert: MagicMock,
        mock_log: MagicMock,
    ) -> None:
        conn = _mock_conn()
        raws = [_make_raw(f"<msg-{i}@example.com>") for i in range(5)]
        adapter = _mock_adapter(raws)
        store = _mock_store()

        result = run_ingest(
            conn=conn,
            adapter=adapter,
            store=store,
            source_name="test-imap",
            source_type="imap",
            limit=2,
        )

        assert result.processed == 2

    @patch("receipt_index.pipeline.insert_ingest_log")
    @patch(
        "receipt_index.pipeline.extract_metadata",
        side_effect=RuntimeError("LLM error"),
    )
    @patch("receipt_index.pipeline.get_processed_source_ids", return_value=set())
    def test_per_receipt_failure_continues(
        self,
        mock_get_ids: MagicMock,
        mock_extract: MagicMock,
        mock_log: MagicMock,
    ) -> None:
        conn = _mock_conn()
        adapter = _mock_adapter([_make_raw(), _make_raw("<msg-2@example.com>")])
        store = _mock_store()

        result = run_ingest(
            conn=conn,
            adapter=adapter,
            store=store,
            source_name="test-imap",
            source_type="imap",
        )

        assert result.failed == 2
        assert result.processed == 0
        assert mock_log.call_count == 2
        for call in mock_log.call_args_list:
            assert call.kwargs["status"] == "failed"
            assert "LLM error" in call.kwargs["error_message"]

    @patch("receipt_index.pipeline.insert_ingest_log")
    @patch("receipt_index.pipeline.insert_receipt", return_value=_SAMPLE_RECEIPT)
    @patch("receipt_index.pipeline.render_pdf", return_value=b"%PDF-fake")
    @patch("receipt_index.pipeline.extract_metadata", return_value=_SAMPLE_METADATA)
    @patch("receipt_index.pipeline.get_processed_source_ids", return_value=set())
    def test_passes_agent_to_extract(
        self,
        mock_get_ids: MagicMock,
        mock_extract: MagicMock,
        mock_render: MagicMock,
        mock_insert: MagicMock,
        mock_log: MagicMock,
    ) -> None:
        conn = _mock_conn()
        adapter = _mock_adapter([_make_raw()])
        store = _mock_store()
        mock_agent = MagicMock()

        run_ingest(
            conn=conn,
            adapter=adapter,
            store=store,
            source_name="test-imap",
            source_type="imap",
            agent=mock_agent,
        )

        mock_extract.assert_called_once()
        _, kwargs = mock_extract.call_args
        assert kwargs["agent"] is mock_agent


class TestIngestResult:
    """Tests for IngestResult dataclass."""

    def test_defaults(self) -> None:
        result = IngestResult()
        assert result.processed == 0
        assert result.skipped == 0
        assert result.failed == 0
        assert result.receipts == []
