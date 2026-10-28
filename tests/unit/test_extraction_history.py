"""Extraction changes for re-extraction (receipt data-model design, §2, §3).

The prompt version, ``receipt_document`` and the attachment list it relies
on, and extraction from a stored rendition.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import uuid4

from pydantic_ai import BinaryContent

from receipt_index.extraction import (
    _SYSTEM_PROMPT,
    DOCUMENT_SYSTEM_PROMPT,
    EXTRACTION_PROMPT_VERSION,
    _build_prompt,
    extract_from_rendition,
)
from receipt_index.models import (
    Attachment,
    FieldConfidence,
    RawReceipt,
    Receipt,
    ReceiptMetadata,
)

_FC = FieldConfidence(vendor=0.9, amount=0.9, paid_amount=0.9, date=0.9)


def _metadata(**overrides: object) -> ReceiptMetadata:
    values: dict[str, object] = {
        "vendor": "Acme Parking (U.S.), LLC",
        "amount": Decimal("12.00"),
        "date": date(2026, 1, 15),
        "confidence": 0.9,
        "field_confidence": _FC,
    }
    values.update(overrides)
    return ReceiptMetadata.model_validate(values)


def _agent() -> MagicMock:
    usage = MagicMock()
    usage.input_tokens = 1200
    usage.output_tokens = 180
    usage.cache_read_tokens = 0
    usage.requests = 1
    result = MagicMock()
    result.output = _metadata()
    result.usage = usage
    agent = MagicMock()
    agent.run_sync.return_value = result
    return agent


def _raw(attachments: list[Attachment]) -> RawReceipt:
    return RawReceipt(
        source_id="<a@example.com>",
        source_name="personal-email",
        source_type="imap",
        date=datetime(2026, 1, 15, tzinfo=UTC),
        subject="Your parking receipt",
        sender="receipts@example.com",
        text_body="Thanks for parking.",
        attachments=attachments,
    )


class TestPromptVersion:
    def test_current_version(self) -> None:
        # 0 is "no history"; 1 was the paid-amount prompt, never recorded;
        # 3 added receipt_link; 4 sends scanned PDF attachments as documents.
        assert EXTRACTION_PROMPT_VERSION == 4

    def test_email_prompt_explains_scanned_pdfs(self) -> None:
        flat = " ".join(_SYSTEM_PROMPT.split())
        assert "--- PDF Attachment (scanned): <file name> ---" in flat
        assert "give that file name as receipt_document" in flat


class TestReceiptDocument:
    def test_optional_and_defaults_to_none(self) -> None:
        assert _metadata().receipt_document is None
        assert _metadata(receipt_document="body").receipt_document == "body"

    def test_email_prompt_asks_for_it(self) -> None:
        flat = " ".join(_SYSTEM_PROMPT.split())
        assert "receipt_document:" in flat
        assert '"body"' in flat
        assert "exactly as listed" in flat

    def test_document_prompt_does_not(self) -> None:
        assert "receipt_document" not in DOCUMENT_SYSTEM_PROMPT


class TestAttachmentList:
    def test_lists_each_attachment_name_and_type(self) -> None:
        prompt = _build_prompt(
            _raw(
                [
                    Attachment("invoice-2026.pdf", "application/pdf", b"%PDF"),
                    Attachment("logo.png", "image/png", b"png"),
                ]
            )
        )
        assert "--- Attachments ---" in prompt
        assert "invoice-2026.pdf (application/pdf)" in prompt
        assert "logo.png (image/png)" in prompt

    def test_no_block_without_attachments(self) -> None:
        assert "--- Attachments ---" not in _build_prompt(_raw([]))

    def test_pdf_text_section_names_its_file(self) -> None:
        raw = _raw([Attachment("invoice-2026.pdf", "application/pdf", b"%PDF")])
        prompt = _build_prompt(raw, pdf_text="Total 12.00", pdf_name="invoice-2026.pdf")
        assert "--- PDF Attachment Content: invoice-2026.pdf ---" in prompt


class TestExtractFromRendition:
    @patch("receipt_index.pdf_reader.extract_text", return_value="Total 12.00 " * 5)
    def test_text_path_prepends_subject_and_sender(self, _text: MagicMock) -> None:
        agent = _agent()
        result = extract_from_rendition(
            b"%PDF", subject="Your parking receipt", sender="r@example.com", agent=agent
        )
        (prompt,), _ = agent.run_sync.call_args
        assert isinstance(prompt, str)
        assert prompt.index("Subject: Your parking receipt") < prompt.index("Total")
        assert "From: r@example.com" in prompt
        assert result.input_tokens == 1200

    @patch("receipt_index.pdf_reader.extract_text", return_value="  ")
    def test_thin_text_falls_back_to_vision(self, _text: MagicMock) -> None:
        agent = _agent()
        extract_from_rendition(
            b"%PDF-scan", subject="Scan", sender="r@example.com", agent=agent
        )
        (message,), _ = agent.run_sync.call_args
        assert isinstance(message, list)
        assert "Subject: Scan" in message[0]
        assert isinstance(message[1], BinaryContent)
        assert message[1].media_type == "application/pdf"
        assert message[1].data == b"%PDF-scan"


class TestReceiptManualFields:
    def test_defaults_to_empty(self) -> None:
        now = datetime(2026, 1, 15, tzinfo=UTC)
        receipt = Receipt(
            id=uuid4(),
            source_id="<a@example.com>",
            source_type="imap",
            source_name="personal-email",
            vendor="Acme Parking (U.S.), LLC",
            amount=Decimal("12.00"),
            currency="USD",
            receipt_date=date(2026, 1, 15),
            description=None,
            confidence=0.9,
            pdf_path="2026/01/x.pdf",
            email_subject=None,
            email_sender=None,
            email_date=None,
            created_at=now,
            updated_at=now,
        )
        assert receipt.manual_fields == []
        assert receipt.model_dump(mode="json")["manual_fields"] == []
