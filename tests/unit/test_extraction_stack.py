"""One real agent call through the locked pydantic-ai and anthropic packages.

Every other extraction test mocks the agent, so a lock that breaks the
pydantic-ai/anthropic pairing leaves them green. These tests run
``extract_metadata`` through ``create_extraction_agent`` unmocked, against
a local stub that answers like the Anthropic Messages API, so they need no
API key and make no network call beyond 127.0.0.1.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, date, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

import pytest

from receipt_index.extraction import create_extraction_agent, extract_metadata
from receipt_index.models import RawReceipt

if TYPE_CHECKING:
    from collections.abc import Iterator

_MODEL = "claude-haiku-4-5-20251001"

# A valid 1x1 PNG.
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)

_OUTPUT = {
    "vendor": "Acme Parking (U.S.), LLC",
    "amount": "12.34",
    "currency": "USD",
    "date": "2026-10-27",
    "description": "Parking",
    "confidence": 0.9,
    "field_confidence": {"vendor": 0.9, "amount": 0.9, "paid_amount": 0.5, "date": 0.9},
}
_INPUT_TOKENS = 321
_OUTPUT_TOKENS = 45


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _reply(request: dict[str, Any]) -> tuple[str, bytes]:
    """Answer a Messages API request with a call to its first tool.

    The agent's output tool is the only tool it declares. The reply streams
    when the request asks for a stream, as pydantic-ai's run_sync does.
    """
    tool = request["tools"][0]["name"]
    message: dict[str, Any] = {
        "id": "msg_stub",
        "type": "message",
        "role": "assistant",
        "model": request["model"],
        "content": [
            {"type": "tool_use", "id": "toolu_stub", "name": tool, "input": _OUTPUT}
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {"input_tokens": _INPUT_TOKENS, "output_tokens": _OUTPUT_TOKENS},
    }
    if not request.get("stream"):
        return "application/json", json.dumps(message).encode()
    start = dict(
        message,
        content=[],
        stop_reason=None,
        usage={"input_tokens": _INPUT_TOKENS, "output_tokens": 0},
    )
    block = {"type": "tool_use", "id": "toolu_stub", "name": tool, "input": {}}
    events = [
        _sse("message_start", {"type": "message_start", "message": start}),
        _sse(
            "content_block_start",
            {"type": "content_block_start", "index": 0, "content_block": block},
        ),
        _sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": json.dumps(_OUTPUT),
                },
            },
        ),
        _sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
        _sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                "usage": {"output_tokens": _OUTPUT_TOKENS},
            },
        ),
        _sse("message_stop", {"type": "message_stop"}),
    ]
    return "text/event-stream", "".join(events).encode()


@pytest.fixture
def stub_api(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    """Serve a stub Messages API and point the Anthropic SDK at it.

    Yields the list of request bodies the stub received.
    """
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        # pre-commit's ruff needs N802 here; the project's ruff calls it unused.
        def do_POST(self) -> None:  # noqa: N802, RUF100
            length = int(self.headers["content-length"])
            body = json.loads(self.rfile.read(length))
            requests.append(body)
            content_type, out = _reply(body)
            self.send_response(200)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stub-key")
    try:
        yield requests
    finally:
        server.shutdown()
        server.server_close()


def _content_types(request: dict[str, Any]) -> list[str]:
    return [
        block["type"]
        for message in request["messages"]
        if isinstance(message["content"], list)
        for block in message["content"]
    ]


def _assert_result(result: Any) -> None:
    assert result.metadata.vendor == "Acme Parking (U.S.), LLC"
    assert result.metadata.amount == Decimal("12.34")
    assert result.metadata.date == date(2026, 10, 27)
    assert result.input_tokens == _INPUT_TOKENS
    assert result.output_tokens == _OUTPUT_TOKENS
    assert result.requests == 1


def test_text_path_through_locked_stack(stub_api: list[dict[str, Any]]) -> None:
    raw = RawReceipt(
        source_id="1",
        source_name="mail",
        source_type="imap",
        date=datetime(2026, 10, 27, tzinfo=UTC),
        subject="Your receipt",
        sender="user@example.com",
        text_body="Acme Parking (U.S.), LLC. 2026-10-27. Total $12.34",
    )
    agent = create_extraction_agent(api_key="stub-key", model=_MODEL)

    result = extract_metadata(raw, agent=agent)

    _assert_result(result)
    assert len(stub_api) == 1
    assert stub_api[0]["model"] == _MODEL
    assert "image" not in _content_types(stub_api[0])


def test_image_path_through_locked_stack(stub_api: list[dict[str, Any]]) -> None:
    raw = RawReceipt(
        source_id="2",
        source_name="drive",
        source_type="gdrive",
        date=datetime(2026, 10, 27, tzinfo=UTC),
        file_name="receipt.png",
        file_content=_PNG,
        file_content_type="image/png",
    )
    agent = create_extraction_agent(api_key="stub-key", model=_MODEL)

    result = extract_metadata(raw, agent=agent)

    _assert_result(result)
    assert len(stub_api) == 1
    assert "image" in _content_types(stub_api[0])
