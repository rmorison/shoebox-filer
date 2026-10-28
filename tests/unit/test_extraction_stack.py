"""Real agent calls through the locked pydantic-ai and anthropic packages.

Every other extraction test mocks the agent, so a lock that breaks the
pydantic-ai/anthropic pairing leaves them green. These tests run the
extraction agent unmocked, against the stub Messages API in
``conftest.py``, so they need no API key and make no network call beyond
127.0.0.1.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
from pydantic_ai.exceptions import ContentFilterError

from receipt_index.config import LlmConfig
from receipt_index.extraction import (
    ExtractionRefusedError,
    create_extraction_agent,
    extract_from_rendition,
    extract_metadata,
)
from receipt_index.models import Attachment, RawReceipt
from tests.pdfs import make_pdf
from tests.unit.conftest import STUB_INPUT_TOKENS, STUB_OUTPUT_TOKENS

if TYPE_CHECKING:
    from tests.unit.conftest import Reply, StubAnthropic

_MODEL = "claude-haiku-5-5"
_KEY = "stub-key-passed-in"

# A valid 1x1 PNG.
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)
# A PDF with no extractable text: both paths fall back to the PDF itself.
_EMPTY_PDF = b"%PDF-1.4\n%%EOF\n"

_OUTPUT = {
    "vendor": "Acme Parking (U.S.), LLC",
    "amount": "12.34",
    "currency": "USD",
    "date": "2026-10-27",
    "description": "Parking",
    "confidence": 0.9,
    "field_confidence": {"vendor": 0.9, "amount": 0.9, "paid_amount": 0.5, "date": 0.9},
}

_SAMPLING = ("temperature", "top_p", "top_k")


@pytest.fixture
def stub(stub_api: StubAnthropic) -> StubAnthropic:
    stub_api.tool_input = _OUTPUT
    return stub_api


def _content_types(request: dict[str, Any]) -> list[str]:
    return [
        block["type"]
        for message in request["messages"]
        if isinstance(message["content"], list)
        for block in message["content"]
    ]


def _email() -> RawReceipt:
    return RawReceipt(
        source_id="1",
        source_name="mail",
        source_type="imap",
        date=datetime(2026, 10, 27, tzinfo=UTC),
        subject="Your receipt",
        sender="user@example.com",
        text_body="Acme Parking (U.S.), LLC. 2026-10-27. Total $12.34",
    )


def _blocks(request: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [
        block
        for message in request["messages"]
        if isinstance(message["content"], list)
        for block in message["content"]
        if block["type"] == kind
    ]


def _email_with(attachment: Attachment) -> RawReceipt:
    return RawReceipt(
        source_id="4",
        source_name="mail",
        source_type="imap",
        date=datetime(2026, 10, 27, tzinfo=UTC),
        subject="Your receipt",
        sender="user@example.com",
        text_body="Your receipt is attached.",
        attachments=[attachment],
    )


def _assert_result(result: Any) -> None:
    assert result.metadata.vendor == "Acme Parking (U.S.), LLC"
    assert result.metadata.amount == Decimal("12.34")
    assert result.metadata.date == date(2026, 10, 27)
    assert result.input_tokens == STUB_INPUT_TOKENS
    assert result.output_tokens == STUB_OUTPUT_TOKENS
    assert result.requests == 1


def _assert_request(stub: StubAnthropic) -> None:
    """One request, with the key passed in, the effort set, no sampling."""
    assert len(stub.requests) == 1
    request = stub.requests[0]
    assert stub.api_keys == [_KEY]
    assert request["model"] == _MODEL
    assert request["output_config"]["effort"] == "medium"
    assert not set(_SAMPLING) & set(request)


def test_default_model_is_haiku_5_5() -> None:
    assert LlmConfig(api_key="k").model == _MODEL


def test_text_path_through_locked_stack(stub: StubAnthropic) -> None:
    agent = create_extraction_agent(api_key=_KEY, model=_MODEL)

    result = extract_metadata(_email(), agent=agent)

    _assert_result(result)
    _assert_request(stub)
    assert "image" not in _content_types(stub.requests[0])


def test_image_path_through_locked_stack(stub: StubAnthropic) -> None:
    raw = RawReceipt(
        source_id="2",
        source_name="drive",
        source_type="gdrive",
        date=datetime(2026, 10, 27, tzinfo=UTC),
        file_name="receipt.png",
        file_content=_PNG,
        file_content_type="image/png",
    )
    agent = create_extraction_agent(api_key=_KEY, model=_MODEL)

    result = extract_metadata(raw, agent=agent)

    _assert_result(result)
    _assert_request(stub)
    assert "image" in _content_types(stub.requests[0])


def test_pdf_vision_path_through_locked_stack(stub: StubAnthropic) -> None:
    # A PDF with no extractable text falls back to sending the PDF itself
    # as a document block, the path Drive PDFs and renditions take.
    raw = RawReceipt(
        source_id="3",
        source_name="drive",
        source_type="gdrive",
        date=datetime(2026, 10, 27, tzinfo=UTC),
        file_name="receipt.pdf",
        file_content=_EMPTY_PDF,
        file_content_type="application/pdf",
    )
    agent = create_extraction_agent(api_key=_KEY, model=_MODEL)

    result = extract_metadata(raw, agent=agent)

    _assert_result(result)
    _assert_request(stub)
    assert "document" in _content_types(stub.requests[0])


def test_scanned_pdf_email_attachment_through_locked_stack(
    stub: StubAnthropic,
) -> None:
    # A scan has no text layer: the PDF itself goes as a document block,
    # after a label naming the file, in the same request as the email text.
    scan = make_pdf()
    raw = _email_with(Attachment("receipt.pdf", "application/pdf", scan))
    agent = create_extraction_agent(api_key=_KEY, model=_MODEL)

    result = extract_metadata(raw, agent=agent)

    _assert_result(result)
    _assert_request(stub)
    (document,) = _blocks(stub.requests[0], "document")
    assert document["source"]["media_type"] == "application/pdf"
    assert base64.b64decode(document["source"]["data"]) == scan
    texts = [block["text"] for block in _blocks(stub.requests[0], "text")]
    assert "Your receipt is attached." in texts[0]
    assert "--- PDF Attachment (scanned): receipt.pdf ---" in texts


def test_text_pdf_email_attachment_through_locked_stack(stub: StubAnthropic) -> None:
    # A PDF with text goes as its text, as before: no document block.
    pdf = make_pdf("Acme Parking (U.S.), LLC 2026-10-27 Total $12.34")
    raw = _email_with(Attachment("receipt.pdf", "application/pdf", pdf))
    agent = create_extraction_agent(api_key=_KEY, model=_MODEL)

    result = extract_metadata(raw, agent=agent)

    _assert_result(result)
    _assert_request(stub)
    assert "document" not in _content_types(stub.requests[0])
    (message,) = stub.requests[0]["messages"]
    content = message["content"]
    prompt = content if isinstance(content, str) else content[0]["text"]
    assert "--- PDF Attachment Content: receipt.pdf ---" in prompt
    assert "Total $12.34" in prompt


@pytest.mark.parametrize("reply", ["refusal", "partial_refusal"])
def test_refusal_is_a_clear_item_error(stub: StubAnthropic, reply: Reply) -> None:
    stub.reply = reply
    agent = create_extraction_agent(api_key=_KEY, model=_MODEL)

    with pytest.raises(ExtractionRefusedError) as info:
        extract_metadata(_email(), agent=agent)

    assert info.value.category == "cyber"
    assert info.value.explanation == "The request was declined."
    assert str(info.value) == (
        "The model declined to extract this receipt (category: cyber): "
        "The request was declined."
    )
    assert isinstance(info.value.__cause__, ContentFilterError)
    # A refusal isn't re-asked within the run: it would repeat the request.
    assert len(stub.requests) == 1


def test_refusal_without_details(stub: StubAnthropic) -> None:
    stub.reply = "refusal"
    stub.refusal_category = None
    stub.refusal_explanation = None
    agent = create_extraction_agent(api_key=_KEY, model=_MODEL)

    with pytest.raises(ExtractionRefusedError) as info:
        extract_from_rendition(
            _EMPTY_PDF, subject="Receipt", sender="user@example.com", agent=agent
        )

    assert info.value.category is None
    assert info.value.explanation is None
    assert str(info.value) == "The model declined to extract this receipt."


def test_order_number_and_payment_through_locked_stack(stub: StubAnthropic) -> None:
    # The real stack, not a mocked agent: the output schema the API receives
    # carries the new fields, and a full card number in the answer is cut to
    # its last four digits before anything is stored.
    stub.tool_input = {
        **_OUTPUT,
        "description": "Parking, card 4111 1111 1111 1111 exp 12 27",
        "order_number": "A-100",
        "payment": {"method": "card", "last4": "4111111111111111", "payer": "J DOE"},
        "field_confidence": {**_OUTPUT["field_confidence"], "payment": 0.8},
    }
    agent = create_extraction_agent(api_key=_KEY, model=_MODEL)

    result = extract_metadata(_email(), agent=agent)

    meta = result.metadata
    assert meta.order_number == "A-100"
    assert meta.payment is not None
    assert (meta.payment.method, meta.payment.last4, meta.payment.payer) == (
        "card",
        "1111",
        "J DOE",
    )
    assert meta.description == "Parking, card ending 1111 exp 12 27"
    assert meta.field_confidence.payment == 0.8
    request = stub.requests[0]
    schema = json.dumps(request.get("tools") or request["output_config"]["format"])
    assert '"order_number"' in schema
    assert '"transfer"' in schema  # the payment method enum
