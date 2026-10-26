"""Tests for receipt_index.extraction."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from pydantic_ai import BinaryContent

from receipt_index.extraction import (
    _SYSTEM_PROMPT,
    DOCUMENT_SYSTEM_PROMPT,
    _build_prompt,
    _extract_from_document,
    _extract_pdf_text,
    _has_sufficient_text,
    _strip_html_tags,
    extract_metadata,
)
from receipt_index.models import (
    Attachment,
    FieldConfidence,
    RawReceipt,
    ReceiptMetadata,
)

_FIELD_CONFIDENCE = FieldConfidence(vendor=0.9, amount=0.9, paid_amount=0.9, date=0.9)


def _mock_agent_result(metadata: ReceiptMetadata) -> MagicMock:
    """Create a mock agent run result with usage info."""
    mock_usage = MagicMock()
    mock_usage.input_tokens = 100
    mock_usage.output_tokens = 50
    mock_usage.cache_read_tokens = 0
    mock_usage.requests = 1

    mock_result = MagicMock()
    mock_result.output = metadata
    mock_result.usage.return_value = mock_usage
    return mock_result


class TestBuildPrompt:
    """Tests for _build_prompt."""

    def test_includes_subject_sender_date(self, sample_raw_receipt: RawReceipt) -> None:
        prompt = _build_prompt(sample_raw_receipt)
        assert "Subject: Your Amazon.com order" in prompt
        assert "From: no-reply@amazon.com" in prompt
        assert "2025-06-15" in prompt

    def test_uses_text_body(self, sample_raw_receipt: RawReceipt) -> None:
        prompt = _build_prompt(sample_raw_receipt)
        assert "Order Total: $42.99" in prompt
        assert "Item: Python Cookbook" in prompt

    def test_strips_html_when_no_text_body(self) -> None:
        raw = RawReceipt(
            source_id="test",
            source_name="test-source",
            source_type="imap",
            subject="HTML Receipt",
            sender="shop@example.com",
            date=datetime(2025, 1, 1, tzinfo=UTC),
            html_body="<p>Your total is <strong>$99.00</strong></p>",
        )
        prompt = _build_prompt(raw)
        assert "Your total is $99.00" in prompt
        assert "<p>" not in prompt
        assert "<strong>" not in prompt

    def test_prefers_text_over_html(self) -> None:
        raw = RawReceipt(
            source_id="test",
            source_name="test-source",
            source_type="imap",
            subject="Both",
            sender="shop@example.com",
            date=datetime(2025, 1, 1, tzinfo=UTC),
            text_body="Text version",
            html_body="<p>HTML version</p>",
        )
        prompt = _build_prompt(raw)
        assert "Text version" in prompt
        assert "HTML version" not in prompt

    def test_handles_no_body(self) -> None:
        raw = RawReceipt(
            source_id="test",
            source_name="test-source",
            source_type="imap",
            subject="Empty",
            sender="x@example.com",
            date=datetime(2025, 1, 1, tzinfo=UTC),
        )
        prompt = _build_prompt(raw)
        assert "(no body content)" in prompt

    def test_includes_pdf_text_when_provided(
        self, sample_raw_receipt: RawReceipt
    ) -> None:
        prompt = _build_prompt(sample_raw_receipt, pdf_text="PDF: Total $99.00")
        assert "--- PDF Attachment Content ---" in prompt
        assert "PDF: Total $99.00" in prompt

    def test_pdf_section_after_email_body(self, sample_raw_receipt: RawReceipt) -> None:
        prompt = _build_prompt(sample_raw_receipt, pdf_text="PDF content here")
        body_pos = prompt.index("--- Email Body ---")
        pdf_pos = prompt.index("--- PDF Attachment Content ---")
        assert pdf_pos > body_pos

    def test_no_pdf_section_when_none(self, sample_raw_receipt: RawReceipt) -> None:
        prompt = _build_prompt(sample_raw_receipt)
        assert "--- PDF Attachment Content ---" not in prompt

    def test_no_pdf_section_when_empty_string(
        self, sample_raw_receipt: RawReceipt
    ) -> None:
        prompt = _build_prompt(sample_raw_receipt, pdf_text="")
        assert "--- PDF Attachment Content ---" not in prompt


class TestStripHtmlTags:
    """Tests for _strip_html_tags."""

    def test_simple_tags(self) -> None:
        assert _strip_html_tags("<p>Hello</p>") == "Hello"

    def test_nested_tags(self) -> None:
        result = _strip_html_tags("<div><p>Nested <em>text</em></p></div>")
        assert result == "Nested text"

    def test_empty_string(self) -> None:
        assert _strip_html_tags("") == ""

    def test_no_tags(self) -> None:
        assert _strip_html_tags("Plain text") == "Plain text"

    def test_preserves_entity_refs(self) -> None:
        result = _strip_html_tags("<p>Price &amp; tax</p>")
        assert "Price" in result
        assert "tax" in result

    def test_attributes_stripped(self) -> None:
        result = _strip_html_tags('<a href="http://example.com">Link</a>')
        assert result == "Link"
        assert "href" not in result


class TestExtractMetadata:
    """Tests for extract_metadata."""

    def test_returns_receipt_metadata(self, sample_raw_receipt: RawReceipt) -> None:
        expected = ReceiptMetadata(
            vendor="Amazon",
            amount=Decimal("42.99"),
            date=date(2025, 6, 15),
            confidence=0.95,
            field_confidence=_FIELD_CONFIDENCE,
        )

        mock_result = _mock_agent_result(expected)
        mock_agent = MagicMock()
        mock_agent.run_sync.return_value = mock_result

        result = extract_metadata(sample_raw_receipt, agent=mock_agent)

        assert result.metadata == expected
        assert result.metadata.vendor == "Amazon"
        assert result.metadata.amount == Decimal("42.99")
        assert result.input_tokens == 100
        assert result.output_tokens == 50

    def test_passes_prompt_to_agent(self, sample_raw_receipt: RawReceipt) -> None:
        mock_result = _mock_agent_result(
            ReceiptMetadata(
                vendor="Amazon",
                amount=Decimal("42.99"),
                date=date(2025, 6, 15),
                confidence=0.95,
                field_confidence=_FIELD_CONFIDENCE,
            )
        )

        mock_agent = MagicMock()
        mock_agent.run_sync.return_value = mock_result

        extract_metadata(sample_raw_receipt, agent=mock_agent)

        call_args = mock_agent.run_sync.call_args
        prompt = call_args[0][0]
        assert "Subject: Your Amazon.com order" in prompt
        assert "Order Total: $42.99" in prompt

    def test_uses_injected_agent(self, sample_raw_receipt: RawReceipt) -> None:
        mock_result = _mock_agent_result(
            ReceiptMetadata(
                vendor="Test",
                amount=Decimal("1.00"),
                date=date(2025, 1, 1),
                confidence=0.5,
                field_confidence=_FIELD_CONFIDENCE,
            )
        )

        mock_agent = MagicMock()
        mock_agent.run_sync.return_value = mock_result

        extract_metadata(sample_raw_receipt, agent=mock_agent)

        # Agent's run_sync should have been called exactly once
        mock_agent.run_sync.assert_called_once()

    @patch(
        "receipt_index.extraction._extract_pdf_text",
        return_value="PDF Amount: $99.00",
    )
    def test_includes_pdf_text_in_prompt(self, _mock_pdf_extract: MagicMock) -> None:
        raw = RawReceipt(
            source_id="test",
            source_name="test-source",
            source_type="imap",
            subject="Receipt",
            sender="shop@example.com",
            date=datetime(2025, 1, 1, tzinfo=UTC),
            text_body="See attached",
            attachments=[
                Attachment(
                    filename="receipt.pdf",
                    content_type="application/pdf",
                    data=b"%PDF",
                ),
            ],
        )
        mock_result = _mock_agent_result(
            ReceiptMetadata(
                vendor="Shop",
                amount=Decimal("99.00"),
                date=date(2025, 1, 1),
                confidence=0.9,
                field_confidence=_FIELD_CONFIDENCE,
            )
        )
        mock_agent = MagicMock()
        mock_agent.run_sync.return_value = mock_result

        extract_metadata(raw, agent=mock_agent)

        prompt = mock_agent.run_sync.call_args[0][0]
        assert "PDF Amount: $99.00" in prompt
        assert "--- PDF Attachment Content ---" in prompt

    @patch("receipt_index.extraction._extract_pdf_text", return_value=None)
    def test_no_pdf_section_without_attachments(
        self, _mock_pdf_extract: MagicMock, sample_raw_receipt: RawReceipt
    ) -> None:
        mock_result = _mock_agent_result(
            ReceiptMetadata(
                vendor="Amazon",
                amount=Decimal("42.99"),
                date=date(2025, 6, 15),
                confidence=0.95,
                field_confidence=_FIELD_CONFIDENCE,
            )
        )
        mock_agent = MagicMock()
        mock_agent.run_sync.return_value = mock_result

        extract_metadata(sample_raw_receipt, agent=mock_agent)

        prompt = mock_agent.run_sync.call_args[0][0]
        assert "--- PDF Attachment Content ---" not in prompt


class TestExtractPdfText:
    """Tests for _extract_pdf_text."""

    @patch("receipt_index.pdf_reader.extract_text", return_value="first pdf text")
    def test_extracts_only_first_pdf(self, mock_extract: MagicMock) -> None:
        attachments = [
            Attachment(
                filename="receipt1.pdf",
                content_type="application/pdf",
                data=b"%PDF-first",
            ),
            Attachment(
                filename="receipt2.pdf",
                content_type="application/pdf",
                data=b"%PDF-second",
            ),
        ]
        result = _extract_pdf_text(attachments)
        assert result == "first pdf text"
        mock_extract.assert_called_once_with(b"%PDF-first")


class TestHasSufficientText:
    """Tests for _has_sufficient_text."""

    def test_sufficient_text(self) -> None:
        assert _has_sufficient_text("A" * 20) is True

    def test_insufficient_text(self) -> None:
        assert _has_sufficient_text("short") is False

    def test_whitespace_stripped(self) -> None:
        # 10 chars with spaces should not count as 20
        assert _has_sufficient_text("a b c d e f g h i j") is False

    def test_empty_string(self) -> None:
        assert _has_sufficient_text("") is False


class TestExtractMetadataRouting:
    """Tests for source_type routing in extract_metadata."""

    def _mock_agent(self) -> MagicMock:
        mock_result = _mock_agent_result(
            ReceiptMetadata(
                vendor="TestVendor",
                amount=Decimal("10.00"),
                date=date(2025, 1, 1),
                confidence=0.9,
                field_confidence=_FIELD_CONFIDENCE,
            )
        )
        agent = MagicMock()
        agent.run_sync.return_value = mock_result
        return agent

    def test_email_source_uses_email_path(self) -> None:
        raw = RawReceipt(
            source_id="test",
            source_name="test-source",
            source_type="imap",
            date=datetime(2025, 1, 1, tzinfo=UTC),
            subject="Receipt",
            sender="shop@example.com",
            text_body="Total: $10.00",
        )
        agent = self._mock_agent()
        result = extract_metadata(raw, agent=agent)

        assert result.metadata.vendor == "TestVendor"
        # Email path sends a string prompt, not a list
        prompt = agent.run_sync.call_args[0][0]
        assert isinstance(prompt, str)
        assert "Subject: Receipt" in prompt

    @patch(
        "receipt_index.pdf_reader.extract_text",
        return_value="Vendor: Shop Total: $25",
    )
    def test_gdrive_source_uses_document_path(self, _mock_extract: MagicMock) -> None:
        raw = RawReceipt(
            source_id="drive-file-1",
            source_name="test-source",
            date=datetime(2025, 1, 1, tzinfo=UTC),
            source_type="gdrive",
            file_content=b"%PDF-test-content",
            file_content_type="application/pdf",
        )
        agent = self._mock_agent()
        result = extract_metadata(raw, agent=agent)

        assert result.metadata.vendor == "TestVendor"
        # PDF with sufficient text sends a text prompt
        prompt = agent.run_sync.call_args[0][0]
        assert isinstance(prompt, str)
        assert "Vendor: Shop" in prompt


class TestExtractFromDocument:
    """Tests for _extract_from_document."""

    def _mock_agent(self) -> MagicMock:
        mock_result = _mock_agent_result(
            ReceiptMetadata(
                vendor="Store",
                amount=Decimal("15.00"),
                date=date(2025, 3, 1),
                confidence=0.85,
                field_confidence=_FIELD_CONFIDENCE,
            )
        )
        agent = MagicMock()
        agent.run_sync.return_value = mock_result
        return agent

    def test_raises_without_file_content(self) -> None:
        raw = RawReceipt(
            source_id="test",
            source_name="test-source",
            date=datetime(2025, 1, 1, tzinfo=UTC),
            source_type="gdrive",
            file_content=None,
            file_content_type="application/pdf",
        )
        with pytest.raises(ValueError, match="no file_content"):
            _extract_from_document(raw, agent=self._mock_agent())

    def test_raises_for_unsupported_content_type(self) -> None:
        raw = RawReceipt(
            source_id="test",
            source_name="test-source",
            date=datetime(2025, 1, 1, tzinfo=UTC),
            source_type="gdrive",
            file_content=b"data",
            file_content_type="text/plain",
        )
        with pytest.raises(ValueError, match="Unsupported file content type"):
            _extract_from_document(raw, agent=self._mock_agent())

    @patch(
        "receipt_index.pdf_reader.extract_text",
        return_value="Sufficient text content that is longer than 20 chars",
    )
    def test_pdf_with_sufficient_text_uses_text_path(
        self, mock_extract: MagicMock
    ) -> None:
        raw = RawReceipt(
            source_id="test",
            source_name="test-source",
            date=datetime(2025, 1, 1, tzinfo=UTC),
            source_type="gdrive",
            file_content=b"%PDF-test",
            file_content_type="application/pdf",
        )
        agent = self._mock_agent()
        _extract_from_document(raw, agent=agent)

        mock_extract.assert_called_once_with(b"%PDF-test")
        prompt = agent.run_sync.call_args[0][0]
        assert isinstance(prompt, str)
        assert "Sufficient text content" in prompt

    @patch("receipt_index.pdf_reader.extract_text", return_value="short")
    def test_pdf_with_insufficient_text_uses_vision(
        self, _mock_extract: MagicMock
    ) -> None:
        raw = RawReceipt(
            source_id="test",
            source_name="test-source",
            date=datetime(2025, 1, 1, tzinfo=UTC),
            source_type="gdrive",
            file_content=b"%PDF-scanned",
            file_content_type="application/pdf",
        )
        agent = self._mock_agent()
        _extract_from_document(raw, agent=agent)

        # Vision path sends a list with BinaryContent
        call_args = agent.run_sync.call_args[0][0]
        assert isinstance(call_args, list)
        assert len(call_args) == 2

    def test_image_jpeg_uses_vision(self) -> None:
        raw = RawReceipt(
            source_id="test",
            source_name="test-source",
            date=datetime(2025, 1, 1, tzinfo=UTC),
            source_type="gdrive",
            file_content=b"\xff\xd8\xff\xe0fake-jpeg",
            file_content_type="image/jpeg",
        )
        agent = self._mock_agent()
        _extract_from_document(raw, agent=agent)

        call_args = agent.run_sync.call_args[0][0]
        assert isinstance(call_args, list)
        assert len(call_args) == 2

    def test_image_png_uses_vision(self) -> None:
        raw = RawReceipt(
            source_id="test",
            source_name="test-source",
            date=datetime(2025, 1, 1, tzinfo=UTC),
            source_type="gdrive",
            file_content=b"\x89PNGfake-png",
            file_content_type="image/png",
        )
        agent = self._mock_agent()
        _extract_from_document(raw, agent=agent)

        call_args = agent.run_sync.call_args[0][0]
        assert isinstance(call_args, list)


class TestEmailImageAttachments:
    """Tests for image-attachment handling in the email extraction path."""

    def _mock_agent(self) -> MagicMock:
        mock_result = _mock_agent_result(
            ReceiptMetadata(
                vendor="Acme Property Management",
                amount=Decimal("3500.00"),
                date=date(2026, 6, 6),
                confidence=0.95,
                field_confidence=_FIELD_CONFIDENCE,
            )
        )
        agent = MagicMock()
        agent.run_sync.return_value = mock_result
        return agent

    def _raw(self, attachments: list[Attachment]) -> RawReceipt:
        return RawReceipt(
            source_id="msg-1",
            source_name="personal-email",
            source_type="imap",
            date=datetime(2026, 6, 6, tzinfo=UTC),
            subject="Screenshot 2026-06-06 at 12.02.34 PM",
            sender="user@example.com",
            attachments=attachments,
        )

    def test_image_attachment_sent_to_vision(self) -> None:
        raw = self._raw(
            [Attachment(filename="shot.png", content_type="image/png", data=b"\x89PNG")]
        )
        agent = self._mock_agent()
        result = extract_metadata(raw, agent=agent)

        assert result.metadata.vendor == "Acme Property Management"
        # Image present → message is a list with the text prompt plus binary image
        message = agent.run_sync.call_args[0][0]
        assert isinstance(message, list)
        assert isinstance(message[0], str)
        assert "Subject: Screenshot" in message[0]
        binaries = [m for m in message if isinstance(m, BinaryContent)]
        assert len(binaries) == 1
        assert binaries[0].media_type == "image/png"

    def test_content_type_with_params_is_normalized(self) -> None:
        raw = self._raw(
            [
                Attachment(
                    filename="s.png", content_type="image/PNG; name=s.png", data=b"x"
                )
            ]
        )
        agent = self._mock_agent()
        extract_metadata(raw, agent=agent)

        message = agent.run_sync.call_args[0][0]
        binaries = [m for m in message if isinstance(m, BinaryContent)]
        assert len(binaries) == 1
        assert binaries[0].media_type == "image/png"

    def test_multiple_images_all_sent(self) -> None:
        # Also exercises gif/webp support beyond png/jpeg.
        raw = self._raw(
            [
                Attachment(filename="a.png", content_type="image/png", data=b"a"),
                Attachment(filename="b.jpg", content_type="image/jpeg", data=b"b"),
                Attachment(filename="c.gif", content_type="image/gif", data=b"c"),
                Attachment(filename="d.webp", content_type="image/webp", data=b"d"),
            ]
        )
        agent = self._mock_agent()
        extract_metadata(raw, agent=agent)

        message = agent.run_sync.call_args[0][0]
        binaries = [m for m in message if isinstance(m, BinaryContent)]
        assert len(binaries) == 4

    @patch(
        "receipt_index.pdf_reader.extract_text",
        return_value="Vendor: Shop Total: $25",
    )
    def test_pdf_and_image_combined(self, _mock_extract: MagicMock) -> None:
        raw = self._raw(
            [
                Attachment(
                    filename="r.pdf", content_type="application/pdf", data=b"%PDF"
                ),
                Attachment(filename="s.png", content_type="image/png", data=b"\x89PNG"),
            ]
        )
        agent = self._mock_agent()
        extract_metadata(raw, agent=agent)

        message = agent.run_sync.call_args[0][0]
        assert isinstance(message, list)
        # PDF text still rides in the text prompt at message[0]...
        assert "Vendor: Shop Total: $25" in message[0]
        # ...and the image is still sent to vision.
        binaries = [m for m in message if isinstance(m, BinaryContent)]
        assert len(binaries) == 1

    def test_no_image_attachments_sends_string(self) -> None:
        raw = RawReceipt(
            source_id="msg-2",
            source_name="personal-email",
            source_type="imap",
            date=datetime(2026, 6, 6, tzinfo=UTC),
            subject="Receipt",
            sender="shop@example.com",
            text_body="Total: $10.00",
        )
        agent = self._mock_agent()
        extract_metadata(raw, agent=agent)

        message = agent.run_sync.call_args[0][0]
        assert isinstance(message, str)


_PROMPTS = pytest.mark.parametrize(
    "prompt",
    [_SYSTEM_PROMPT, DOCUMENT_SYSTEM_PROMPT],
    ids=["email", "document"],
)

# One phrase per paid-amount rule in the data-model design, so that an edit
# dropping a rule from either prompt fails a test.
_PAID_AMOUNT_RULES = {
    "includes tax, shipping and tip": "tax, shipping and tip",
    "after discounts": "after discounts and coupons",
    "excludes gift cards and credit": "gift cards, store credit, points or vouchers",
    "description notes gift cards": "say in the description",
    "invoice payment, not balance": "the payment, not the balance or the invoice total",
    "charged in parts": "charged in parts",
    "hold is not a payment": "pre-authorization or hold is not a payment",
    "unpaid invoice is 0": "nothing has been paid (an unpaid invoice), use 0",
    "refund returned to payment methods": "for a refund, what was returned",
    "refund excludes store credit": "returned as store credit, a gift card or points",
    "unknown is null": "does not say what was paid or returned, use null",
    "never copied from amount": "Never copy amount into paid_amount",
    "refund is a positive number": "as a positive number, never negative",
}


class TestPaidAmountPrompts:
    """Both system prompts carry the paid-amount rules and field confidence."""

    @_PROMPTS
    @pytest.mark.parametrize(
        "phrase", list(_PAID_AMOUNT_RULES.values()), ids=list(_PAID_AMOUNT_RULES)
    )
    def test_paid_amount_rule_present(self, prompt: str, phrase: str) -> None:
        assert phrase in " ".join(prompt.split())

    @_PROMPTS
    def test_amount_is_the_total(self, prompt: str) -> None:
        assert "amount: The order, invoice or statement total" in prompt

    @_PROMPTS
    def test_old_total_rule_removed(self, prompt: str) -> None:
        assert "use the total amount" not in prompt
        assert "The total amount charged" not in prompt

    @_PROMPTS
    def test_field_confidence_instruction(self, prompt: str) -> None:
        flat = " ".join(prompt.split())
        assert "field_confidence:" in flat
        assert "vendor, amount, paid_amount and date" in flat
        assert "below 0.5 when a figure is inferred rather than printed" in flat
        # A null paid_amount still gets a numeric confidence; null would fail
        # validation and lose the receipt.
        assert "always a number, even when paid_amount is null" in flat
