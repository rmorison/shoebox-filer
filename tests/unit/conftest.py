"""Unit-test fixtures: a keyless stub of the Anthropic Messages API.

Every other test mocks the agents, so a lock that breaks the pydantic-ai and
anthropic pairing leaves them green. The stack tests run the real agents
against this stub instead. It listens on 127.0.0.1 only, and the fixture
removes any API key from the environment, so a test passes only when the
key reaches the SDK through the code under test.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, Literal

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

STUB_INPUT_TOKENS = 321
STUB_OUTPUT_TOKENS = 45

Reply = Literal["answer", "refusal", "partial_refusal"]


@dataclass
class StubAnthropic:
    """What the stub received, and how it answers.

    ``reply`` is ``"answer"`` (a thinking block, then ``tool_input`` as a
    call to the request's first tool or, under structured output, as JSON
    text; or ``text`` when the request asks for neither) or ``"refusal"``
    (``stop_reason: "refusal"`` with no content, as Haiku 5.5 declines) or
    ``"partial_refusal"`` (some text, then the refusal).
    """

    tool_input: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    reply: Reply = "answer"
    refusal_category: str | None = "cyber"
    refusal_explanation: str | None = "The request was declined."
    requests: list[dict[str, Any]] = field(default_factory=list)
    api_keys: list[str | None] = field(default_factory=list)


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _blocks(stub: StubAnthropic, request: dict[str, Any]) -> list[dict[str, Any]]:
    if stub.reply == "refusal":
        return []
    if stub.reply == "partial_refusal":
        return [{"type": "text", "text": '{"vendor": "Acme'}]
    # Haiku 5.5 thinks by default; its thinking text is omitted (empty).
    thinking = {"type": "thinking", "thinking": "", "signature": "sig_stub"}
    answer: dict[str, Any]
    if request.get("tools"):
        tool = request["tools"][0]["name"]
        answer = {
            "type": "tool_use",
            "id": "toolu_stub",
            "name": tool,
            "input": stub.tool_input,
        }
    elif request.get("output_config", {}).get("format"):
        # Structured output: the answer is JSON text in the schema.
        answer = {"type": "text", "text": json.dumps(stub.tool_input)}
    else:
        answer = {"type": "text", "text": stub.text}
    return [thinking, answer]


def _stop(stub: StubAnthropic, request: dict[str, Any]) -> dict[str, Any]:
    if stub.reply in ("refusal", "partial_refusal"):
        return {
            "stop_reason": "refusal",
            "stop_details": {
                "type": "refusal",
                "category": stub.refusal_category,
                "explanation": stub.refusal_explanation,
            },
        }
    return {
        "stop_reason": "tool_use" if request.get("tools") else "end_turn",
        "stop_details": None,
    }


def _block_events(index: int, block: dict[str, Any]) -> list[str]:
    if block["type"] == "thinking":
        start = {"type": "thinking", "thinking": "", "signature": ""}
        deltas = [{"type": "signature_delta", "signature": block["signature"]}]
    elif block["type"] == "tool_use":
        start = dict(block, input={})
        deltas = [
            {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
        ]
    else:
        start = {"type": "text", "text": ""}
        deltas = [{"type": "text_delta", "text": block["text"]}]
    events = [
        _sse(
            "content_block_start",
            {"type": "content_block_start", "index": index, "content_block": start},
        )
    ]
    events += [
        _sse(
            "content_block_delta",
            {"type": "content_block_delta", "index": index, "delta": delta},
        )
        for delta in deltas
    ]
    events.append(
        _sse("content_block_stop", {"type": "content_block_stop", "index": index})
    )
    return events


def _reply(stub: StubAnthropic, request: dict[str, Any]) -> tuple[str, bytes]:
    """Answer a Messages API request, streaming when it asks for a stream."""
    blocks = _blocks(stub, request)
    stop = _stop(stub, request)
    message: dict[str, Any] = {
        "id": "msg_stub",
        "type": "message",
        "role": "assistant",
        "model": request["model"],
        "content": blocks,
        "stop_sequence": None,
        "usage": {
            "input_tokens": STUB_INPUT_TOKENS,
            "output_tokens": STUB_OUTPUT_TOKENS,
        },
        **stop,
    }
    if not request.get("stream"):
        return "application/json", json.dumps(message).encode()
    start = dict(
        message,
        content=[],
        stop_reason=None,
        stop_details=None,
        usage={"input_tokens": STUB_INPUT_TOKENS, "output_tokens": 0},
    )
    events = [_sse("message_start", {"type": "message_start", "message": start})]
    for index, block in enumerate(blocks):
        events += _block_events(index, block)
    events += [
        _sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {**stop, "stop_sequence": None},
                "usage": {"output_tokens": STUB_OUTPUT_TOKENS},
            },
        ),
        _sse("message_stop", {"type": "message_stop"}),
    ]
    return "text/event-stream", "".join(events).encode()


@pytest.fixture
def stub_api(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[StubAnthropic]:
    """Serve a stub Messages API and point the Anthropic SDK at it.

    No API key is left in the environment: the agents under test must pass
    their own. Yields the stub, to set its reply and read what it received.
    """
    stub = StubAnthropic()

    class Handler(BaseHTTPRequestHandler):
        # pre-commit's ruff needs N802 here; the project's ruff calls it unused.
        def do_POST(self) -> None:  # noqa: N802, RUF100
            length = int(self.headers["content-length"])
            body = json.loads(self.rfile.read(length))
            stub.requests.append(body)
            bearer = self.headers.get("authorization", "")
            stub.api_keys.append(
                self.headers.get("x-api-key") or bearer.removeprefix("Bearer ") or None
            )
            content_type, out = _reply(stub, body)
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
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    # Nor a credentials profile on disk.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    try:
        yield stub
    finally:
        server.shutdown()
        server.server_close()
