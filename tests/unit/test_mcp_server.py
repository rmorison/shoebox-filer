"""Tests for receipt_index.mcp_server: the read-only MCP tools."""

from __future__ import annotations

import base64
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch
from uuid import UUID

import anyio
import pytest
from mcp import Client
from mcp_types import Implementation

from receipt_index.mcp_server import (
    _INSTRUCTIONS,
    AccessLog,
    AccessLogError,
    FailureOut,
    ReceiptOut,
    RoleCheckError,
    _client_info,
    build_server,
    check_read_only_role,
    default_access_log_path,
)
from receipt_index.models import IngestLogEntry, Receipt
from receipt_index.store import LocalEvidenceStore, pdf_path_for, store_rendition

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mcp_types import CallToolResult

_ID = UUID("019572a0-0000-7000-8000-000000000001")
_MISSING_ID = "019572a0-0000-7000-8000-0000000000ff"
_PDF_PATH = "2025/06/2025-06-15__acme-parking__42.99.pdf"
_PDF_BYTES = b"%PDF-1.4\n% receipt\n%%EOF\n"


def _receipt(**overrides: Any) -> Receipt:
    row: dict[str, Any] = {
        "id": _ID,
        "source_id": "<msg-1@example.com>",
        "source_type": "imap",
        "source_name": "test-email",
        "vendor": "Acme Parking (U.S.), LLC",
        "amount": Decimal("42.99"),
        "currency": "USD",
        "receipt_date": date(2025, 6, 15),
        "description": "Parking",
        "confidence": 0.95,
        "pdf_path": _PDF_PATH,
        "email_subject": "Your receipt",
        "email_sender": "billing@example.com",
        "email_date": datetime(2025, 6, 15, 10, 30, tzinfo=UTC),
        "file_name": None,
        "created_at": datetime(2025, 6, 15, 12, 0, tzinfo=UTC),
        "updated_at": datetime(2025, 6, 15, 12, 0, tzinfo=UTC),
    }
    row.update(overrides)
    return Receipt.model_validate(row)


def _failure(n: int, **overrides: Any) -> IngestLogEntry:
    return IngestLogEntry.model_validate(
        overrides
        | {
            "id": UUID(f"019572a0-0000-7000-8000-{n:012d}"),
            "source_id": f"<fail-{n}@example.com>",
            "source_type": "imap",
            "status": "failed",
            "receipt_id": None,
            "vendor": None,
            "amount": Decimal("1.50"),
            "email_subject": "Statement",
            "email_sender": "billing@example.com",
            "email_date": None,
            "error_message": "extraction failed",
            "created_at": datetime(2025, 6, 15, 12, 0, tzinfo=UTC),
        }
    )


@pytest.fixture()
def store_root(tmp_path: Path) -> Path:
    root = tmp_path / "store"
    pdf = root / _PDF_PATH
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(_PDF_BYTES)
    return root


@pytest.fixture()
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "mcp-access.jsonl"


@pytest.fixture()
def conn() -> Iterator[MagicMock]:
    """Patch the server's connection factory; yield the mock connection."""
    mock_conn = MagicMock()
    mock_conn.__enter__.return_value = mock_conn
    with patch(
        "receipt_index.mcp_server.get_connection", return_value=mock_conn
    ) as factory:
        mock_conn.factory = factory
        yield mock_conn


def _server(store_root: Path, log_path: Path, max_content_bytes: int = 1024) -> Any:
    return build_server(
        database_url="postgresql://reader@localhost/test",  # pragma: allowlist secret
        store_root=store_root,
        access_log=AccessLog(log_path),
        max_content_bytes=max_content_bytes,
    )


def _call(
    server: Any,
    tool: str,
    args: dict[str, Any],
    client_name: str | None = "test-client",
) -> CallToolResult:
    async def go() -> CallToolResult:
        info = (
            Implementation(name=client_name, version="9.9")
            if client_name is not None
            else None
        )
        async with Client(server, client_info=info) as client:
            return await client.call_tool(tool, args)

    return anyio.run(go)


def _text(result: CallToolResult) -> str:
    return " ".join(getattr(c, "text", "") for c in result.content)


def _log_lines(log_path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log_path.read_text().splitlines()]


# ---------------------------------------------------------------------------
# Tool listing
# ---------------------------------------------------------------------------


class TestListTools:
    def test_lists_four_read_only_tools(self, store_root: Path, log_path: Path) -> None:
        async def go() -> Any:
            async with Client(_server(store_root, log_path)) as client:
                return await client.list_tools()

        tools = {t.name: t for t in anyio.run(go).tools}
        assert set(tools) == {
            "search_receipts",
            "get_receipt",
            "get_receipt_file",
            "list_failures",
        }
        for tool in tools.values():
            assert tool.annotations is not None
            assert tool.annotations.read_only_hint is True
            assert tool.annotations.destructive_hint is False


# ---------------------------------------------------------------------------
# search_receipts
# ---------------------------------------------------------------------------


class TestSearchReceipts:
    def test_returns_receipts_with_file_reference(
        self, conn: MagicMock, store_root: Path, log_path: Path
    ) -> None:
        with (
            patch(
                "receipt_index.repository.search_receipts",
                return_value=[_receipt()],
            ) as search,
            patch("receipt_index.repository.count_receipts", return_value=1),
        ):
            result = _call(
                _server(store_root, log_path),
                "search_receipts",
                {"vendor": "acme", "amount": "42.99", "date_from": "2025-06-01"},
            )

        assert result.is_error is False
        data = result.structured_content
        assert data is not None
        assert data["total"] == 1
        assert data["next_offset"] is None
        receipt = data["receipts"][0]
        assert receipt["amount"] == "42.99"
        assert receipt["vendor"] == "Acme Parking (U.S.), LLC"
        assert receipt["file"] == {
            "path": str((store_root / _PDF_PATH).resolve()),
            "exists": True,
        }
        kwargs = search.call_args.kwargs
        assert kwargs["vendor"] == "acme"
        assert kwargs["amount"] == Decimal("42.99")
        assert kwargs["date_from"] == date(2025, 6, 1)
        assert kwargs["limit"] == 50
        assert kwargs["offset"] == 0

    def test_connection_is_read_only_snapshot(
        self, conn: MagicMock, store_root: Path, log_path: Path
    ) -> None:
        import psycopg

        with (
            patch("receipt_index.repository.search_receipts", return_value=[]),
            patch("receipt_index.repository.count_receipts", return_value=0),
        ):
            _call(_server(store_root, log_path), "search_receipts", {})

        assert conn.factory.call_args.kwargs == {"read_only": True}
        assert conn.isolation_level == psycopg.IsolationLevel.REPEATABLE_READ
        assert conn.read_only is True

    @pytest.mark.parametrize(
        ("offset", "page_len", "total", "expected_next"),
        [
            (0, 2, 5, 2),  # first page
            (2, 2, 5, 4),  # middle page
            (4, 1, 5, None),  # last page
            (10, 0, 5, None),  # past the end
        ],
    )
    def test_paging(
        self,
        conn: MagicMock,
        store_root: Path,
        log_path: Path,
        offset: int,
        page_len: int,
        total: int,
        expected_next: int | None,
    ) -> None:
        with (
            patch(
                "receipt_index.repository.search_receipts",
                return_value=[_receipt()] * page_len,
            ) as search,
            patch("receipt_index.repository.count_receipts", return_value=total),
        ):
            result = _call(
                _server(store_root, log_path),
                "search_receipts",
                {"limit": 2, "offset": offset},
            )

        data = result.structured_content
        assert data is not None
        assert data["limit"] == 2
        assert data["offset"] == offset
        assert data["total"] == total
        assert data["next_offset"] == expected_next
        assert len(data["receipts"]) == page_len
        assert search.call_args.kwargs["limit"] == 2
        assert search.call_args.kwargs["offset"] == offset

    @pytest.mark.parametrize(
        "args",
        [
            {"limit": 0},
            {"limit": 201},
            {"offset": -1},
            {"amount": "1", "amount_min": "0.5"},
            {"amount": "1", "amount_max": "2"},
            {"amount": "abc"},
            {"amount_min": "NaN"},
            {"date_from": "15/06/2025"},
        ],
    )
    def test_rejects_bad_arguments(
        self,
        conn: MagicMock,
        store_root: Path,
        log_path: Path,
        args: dict[str, Any],
    ) -> None:
        with patch("receipt_index.repository.search_receipts") as search:
            result = _call(_server(store_root, log_path), "search_receipts", args)

        assert result.is_error is True
        search.assert_not_called()

    def test_missing_file_is_flagged(
        self, conn: MagicMock, store_root: Path, log_path: Path
    ) -> None:
        with (
            patch(
                "receipt_index.repository.search_receipts",
                return_value=[_receipt(pdf_path="2025/06/gone.pdf")],
            ),
            patch("receipt_index.repository.count_receipts", return_value=1),
        ):
            result = _call(_server(store_root, log_path), "search_receipts", {})

        data = result.structured_content
        assert data is not None
        assert data["receipts"][0]["file"]["exists"] is False


# ---------------------------------------------------------------------------
# get_receipt
# ---------------------------------------------------------------------------


class TestGetReceipt:
    def test_returns_receipt(
        self, conn: MagicMock, store_root: Path, log_path: Path
    ) -> None:
        with patch(
            "receipt_index.repository.get_receipt_by_id", return_value=_receipt()
        ) as get:
            result = _call(
                _server(store_root, log_path), "get_receipt", {"receipt_id": str(_ID)}
            )

        assert result.is_error is False
        data = result.structured_content
        assert data is not None
        assert data["id"] == str(_ID)
        assert data["amount"] == "42.99"
        assert data["file"]["exists"] is True
        assert get.call_args.args[1] == _ID

    def test_missing_receipt_is_error(
        self, conn: MagicMock, store_root: Path, log_path: Path
    ) -> None:
        with patch("receipt_index.repository.get_receipt_by_id", return_value=None):
            result = _call(
                _server(store_root, log_path),
                "get_receipt",
                {"receipt_id": _MISSING_ID},
            )

        assert result.is_error is True
        assert f"Receipt not found: {_MISSING_ID}" in _text(result)

    def test_invalid_uuid_is_error(
        self, conn: MagicMock, store_root: Path, log_path: Path
    ) -> None:
        with patch("receipt_index.repository.get_receipt_by_id") as get:
            result = _call(
                _server(store_root, log_path), "get_receipt", {"receipt_id": "nope"}
            )

        assert result.is_error is True
        assert "Invalid UUID" in _text(result)
        get.assert_not_called()


# ---------------------------------------------------------------------------
# get_receipt_file
# ---------------------------------------------------------------------------


class TestGetReceiptFile:
    def test_path_only_by_default(
        self, conn: MagicMock, store_root: Path, log_path: Path
    ) -> None:
        with patch(
            "receipt_index.repository.get_receipt_by_id", return_value=_receipt()
        ):
            result = _call(
                _server(store_root, log_path),
                "get_receipt_file",
                {"receipt_id": str(_ID)},
            )

        assert result.is_error is False
        data = result.structured_content
        assert data is not None
        assert data["path"] == str((store_root / _PDF_PATH).resolve())
        assert data["size_bytes"] == len(_PDF_BYTES)
        assert data["mime_type"] == "application/pdf"
        assert data["content_included"] is False
        assert data["content_omitted_reason"] == "not_requested"
        assert not [c for c in result.content if c.type == "resource"]

        (entry,) = _log_lines(log_path)
        assert entry["tool"] == "get_receipt_file"
        assert entry["receipt_id"] == str(_ID)
        assert entry["pdf_path"] == _PDF_PATH
        assert entry["outcome"] == "ok"
        assert entry["content_included"] is False
        assert entry["size_bytes"] == len(_PDF_BYTES)
        assert entry["client"] == {"name": "test-client", "version": "9.9"}
        assert datetime.fromisoformat(entry["ts"]).tzinfo is not None

    def test_returns_content_when_asked(
        self, conn: MagicMock, store_root: Path, log_path: Path
    ) -> None:
        with patch(
            "receipt_index.repository.get_receipt_by_id", return_value=_receipt()
        ):
            result = _call(
                _server(store_root, log_path),
                "get_receipt_file",
                {"receipt_id": str(_ID), "include_content": True},
            )

        data = result.structured_content
        assert data is not None
        assert data["content_included"] is True
        assert data["content_omitted_reason"] is None
        (resource,) = [c for c in result.content if c.type == "resource"]
        assert resource.resource.mime_type == "application/pdf"
        assert base64.b64decode(resource.resource.blob) == _PDF_BYTES
        (entry,) = _log_lines(log_path)
        assert entry["content_included"] is True
        assert entry["outcome"] == "ok"

    def test_too_large_returns_path_only(
        self, conn: MagicMock, store_root: Path, log_path: Path
    ) -> None:
        with patch(
            "receipt_index.repository.get_receipt_by_id", return_value=_receipt()
        ):
            result = _call(
                _server(store_root, log_path, max_content_bytes=len(_PDF_BYTES) - 1),
                "get_receipt_file",
                {"receipt_id": str(_ID), "include_content": True},
            )

        assert result.is_error is False
        data = result.structured_content
        assert data is not None
        assert data["content_included"] is False
        assert data["content_omitted_reason"] == "too_large"
        assert data["path"] == str((store_root / _PDF_PATH).resolve())
        assert not [c for c in result.content if c.type == "resource"]
        (entry,) = _log_lines(log_path)
        assert entry["outcome"] == "too_large"

    def test_content_at_exact_limit_is_returned(
        self, conn: MagicMock, store_root: Path, log_path: Path
    ) -> None:
        with patch(
            "receipt_index.repository.get_receipt_by_id", return_value=_receipt()
        ):
            result = _call(
                _server(store_root, log_path, max_content_bytes=len(_PDF_BYTES)),
                "get_receipt_file",
                {"receipt_id": str(_ID), "include_content": True},
            )

        data = result.structured_content
        assert data is not None
        assert data["content_included"] is True

    @pytest.mark.parametrize(
        ("receipt", "receipt_id", "outcome", "message"),
        [
            (None, _MISSING_ID, "not_found", "Receipt not found"),
            ("missing-file", str(_ID), "file_missing", "file is missing"),
            ("outside", str(_ID), "path_outside_store", "outside the file store"),
            ("unused", "not-a-uuid", "invalid_id", "Invalid UUID"),
        ],
    )
    def test_errors_are_logged(
        self,
        conn: MagicMock,
        store_root: Path,
        log_path: Path,
        receipt: str | None,
        receipt_id: str,
        outcome: str,
        message: str,
    ) -> None:
        (store_root.parent / "secret.pdf").write_bytes(b"not a receipt")
        found = {
            None: None,
            "missing-file": _receipt(pdf_path="2025/06/gone.pdf"),
            "outside": _receipt(pdf_path="../secret.pdf"),
            "unused": None,
        }[receipt]
        with patch("receipt_index.repository.get_receipt_by_id", return_value=found):
            result = _call(
                _server(store_root, log_path),
                "get_receipt_file",
                {"receipt_id": receipt_id, "include_content": True},
            )

        assert result.is_error is True
        assert message in _text(result)
        assert not [c for c in result.content if c.type == "resource"]
        (entry,) = _log_lines(log_path)
        assert entry["outcome"] == outcome
        assert entry["content_included"] is False

    def test_database_error_is_logged(
        self, conn: MagicMock, store_root: Path, log_path: Path
    ) -> None:
        import psycopg

        with patch(
            "receipt_index.repository.get_receipt_by_id",
            side_effect=psycopg.OperationalError("server closed the connection"),
        ):
            result = _call(
                _server(store_root, log_path),
                "get_receipt_file",
                {"receipt_id": str(_ID), "include_content": True},
            )

        assert result.is_error is True
        assert "file lookup failed (OperationalError)" in _text(result)
        (entry,) = _log_lines(log_path)
        assert entry["outcome"] == "error"
        assert entry["content_included"] is False

    def test_absent_client_info_logs_null(
        self, conn: MagicMock, store_root: Path, log_path: Path
    ) -> None:
        with patch(
            "receipt_index.repository.get_receipt_by_id", return_value=_receipt()
        ):
            _call(
                _server(store_root, log_path),
                "get_receipt_file",
                {"receipt_id": str(_ID)},
                client_name=None,
            )

        (entry,) = _log_lines(log_path)
        # The SDK fills in its own default client name when none is given;
        # whatever arrives is recorded, and a missing field becomes null.
        assert set(entry["client"]) == {"name", "version"}

    def test_unwritable_access_log_withholds_file(
        self, conn: MagicMock, store_root: Path, tmp_path: Path
    ) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("a file, not a directory")
        with patch(
            "receipt_index.repository.get_receipt_by_id", return_value=_receipt()
        ):
            result = _call(
                _server(store_root, blocker / "mcp-access.jsonl"),
                "get_receipt_file",
                {"receipt_id": str(_ID), "include_content": True},
            )

        assert result.is_error is True
        assert "access log" in _text(result)
        assert not [c for c in result.content if c.type == "resource"]


# ---------------------------------------------------------------------------
# list_failures
# ---------------------------------------------------------------------------


class TestListFailures:
    def test_pages_failures(
        self, conn: MagicMock, store_root: Path, log_path: Path
    ) -> None:
        with (
            patch(
                "receipt_index.repository.get_ingest_failures",
                return_value=[_failure(1), _failure(2)],
            ) as get,
            patch("receipt_index.repository.count_ingest_failures", return_value=3),
        ):
            result = _call(
                _server(store_root, log_path),
                "list_failures",
                {"limit": 2, "offset": 0},
            )

        data = result.structured_content
        assert data is not None
        assert data["total"] == 3
        assert data["next_offset"] == 2
        assert [f["source_id"] for f in data["failures"]] == [
            "<fail-1@example.com>",
            "<fail-2@example.com>",
        ]
        assert data["failures"][0]["amount"] == "1.50"
        assert get.call_args.kwargs == {"status": "failed", "limit": 2, "offset": 0}

    def test_rejects_bad_limit(
        self, conn: MagicMock, store_root: Path, log_path: Path
    ) -> None:
        result = _call(_server(store_root, log_path), "list_failures", {"limit": 0})
        assert result.is_error is True

    def test_lists_link_only_items(
        self, conn: MagicMock, store_root: Path, log_path: Path
    ) -> None:
        link = "https://receipts.example.com/r/abc123"
        skipped = _failure(1).model_copy(
            update={
                "status": "skipped",
                "error_message": "Skipped: link-only receipt (receipts.example.com)",
                "receipt_link": link,
            }
        )
        with (
            patch(
                "receipt_index.repository.get_ingest_failures", return_value=[skipped]
            ) as get,
            patch(
                "receipt_index.repository.count_ingest_failures", return_value=1
            ) as count,
        ):
            result = _call(
                _server(store_root, log_path),
                "list_failures",
                {"status": "link-only"},
            )

        data = result.structured_content
        assert data is not None
        assert data["failures"][0]["status"] == "skipped"
        assert data["failures"][0]["receipt_link"] == link
        assert get.call_args.kwargs["status"] == "link-only"
        assert count.call_args.kwargs == {"status": "link-only"}

    def test_rejects_other_statuses(
        self, conn: MagicMock, store_root: Path, log_path: Path
    ) -> None:
        # Read-only and narrow: only the two lists the consumer agent needs.
        result = _call(
            _server(store_root, log_path), "list_failures", {"status": "skipped"}
        )
        assert result.is_error is True

    def test_instructions_name_link_only_items(self) -> None:
        # The default lists failures only: an agent checking for missing
        # receipts must learn about link-only items from the instructions.
        from receipt_index.mcp_server import _INSTRUCTIONS

        flat = " ".join(_INSTRUCTIONS.split())
        assert "list_failures with status='link-only'" in flat
        assert "never fetch it automatically" in flat

    def test_receipt_link_is_marked_untrusted(self) -> None:
        description = FailureOut.model_fields["receipt_link"].description or ""
        assert "must not be fetched" in description


# ---------------------------------------------------------------------------
# Startup role check and access log
# ---------------------------------------------------------------------------


class TestCheckReadOnlyRole:
    def _conn(
        self,
        rows: list[dict[str, Any]],
        can_read: bool = True,
        usage: bool = True,
    ) -> MagicMock:
        conn = MagicMock()
        usage_cur = MagicMock()
        usage_cur.fetchone.return_value = {"usage": usage}
        read_cur = MagicMock()
        read_cur.fetchone.return_value = {"can_read": can_read}
        write_cur = MagicMock()
        write_cur.fetchall.return_value = rows
        conn.execute.side_effect = [usage_cur, write_cur, read_cur]
        return conn

    def test_read_only_role_passes(self) -> None:
        check_read_only_role(self._conn([]))

    def test_column_level_insert_is_refused(self) -> None:
        conn = self._conn([{"object": "receipt.receipts", "privilege": "INSERT"}])
        with pytest.raises(RoleCheckError, match=r"receipt\.receipts: INSERT"):
            check_read_only_role(conn)
        sql = conn.execute.call_args_list[1].args[0]
        assert "has_any_column_privilege" in sql
        assert "has_schema_privilege" in sql
        assert "pg_has_role(current_user, oid, 'MEMBER')" in sql

    def test_role_without_schema_usage_is_refused_first(self) -> None:
        conn = self._conn([], usage=False)
        with pytest.raises(RoleCheckError, match="lacks SELECT"):
            check_read_only_role(conn)
        # The privilege queries would fail without USAGE, so they never run.
        assert conn.execute.call_count == 1

    def test_role_without_select_is_refused(self) -> None:
        with pytest.raises(RoleCheckError, match="SELECT"):
            check_read_only_role(self._conn([], can_read=False))


class TestAccessLog:
    def test_appends_json_lines(self, log_path: Path) -> None:
        log = AccessLog(log_path)
        log.check_writable()
        log.record({"a": 1})
        log.record({"b": 2})
        assert _log_lines(log_path) == [{"a": 1}, {"b": 2}]

    def test_check_writable_raises(self, tmp_path: Path) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        with pytest.raises(AccessLogError):
            AccessLog(blocker / "log.jsonl").check_writable()

    def test_default_path_uses_xdg_state_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        assert default_access_log_path() == (
            tmp_path / "receipt-index" / "mcp-access.jsonl"
        )

    def test_default_path_falls_back_to_local_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert default_access_log_path() == (
            tmp_path / ".local" / "state" / "receipt-index" / "mcp-access.jsonl"
        )


class TestClientInfo:
    def test_no_session_params_gives_nulls(self) -> None:
        ctx = MagicMock()
        type(ctx).session = property(lambda _self: (_ for _ in ()).throw(ValueError))
        assert _client_info(ctx) == {"name": None, "version": None}

    def test_missing_client_info_gives_nulls(self) -> None:
        ctx = MagicMock()
        ctx.session.client_params.client_info = None
        assert _client_info(ctx) == {"name": None, "version": None}

    def test_client_params_none_gives_nulls(self) -> None:
        ctx = MagicMock()
        ctx.session.client_params = None
        assert _client_info(ctx) == {"name": None, "version": None}


class TestFileRef:
    def test_path_outside_store_is_not_reported_as_existing(
        self, conn: MagicMock, store_root: Path, log_path: Path
    ) -> None:
        (store_root.parent / "secret.pdf").write_bytes(b"not a receipt")
        with (
            patch(
                "receipt_index.repository.search_receipts",
                return_value=[_receipt(pdf_path="../secret.pdf")],
            ),
            patch("receipt_index.repository.count_receipts", return_value=1),
        ):
            result = _call(_server(store_root, log_path), "search_receipts", {})

        data = result.structured_content
        assert data is not None
        assert data["receipts"][0]["file"]["exists"] is False


class TestOutputModelsMatchDomainModels:
    """A new column on Receipt or IngestLogEntry must reach the MCP output."""

    def test_receipt_out_fields(self) -> None:
        domain = set(Receipt.model_fields) | set(Receipt.model_computed_fields)
        assert set(ReceiptOut.model_fields) - {"file"} == domain

    def test_failure_out_fields(self) -> None:
        assert set(FailureOut.model_fields) == set(IngestLogEntry.model_fields)


class TestClientIdClipping:
    def test_long_invalid_id_is_clipped_in_log_and_error(
        self, conn: MagicMock, store_root: Path, log_path: Path
    ) -> None:
        huge = "x" * 100_000
        result = _call(
            _server(store_root, log_path), "get_receipt_file", {"receipt_id": huge}
        )

        assert result.is_error is True
        text = _text(result)
        assert len(text) < 300
        assert "(100000 chars)" in text
        (entry,) = _log_lines(log_path)
        assert entry["outcome"] == "invalid_id"
        assert entry["receipt_id"] == "x" * 64 + "... (100000 chars)"
        assert len(log_path.read_text()) < 1000

    def test_get_receipt_error_is_clipped(
        self, conn: MagicMock, store_root: Path, log_path: Path
    ) -> None:
        result = _call(
            _server(store_root, log_path), "get_receipt", {"receipt_id": "y" * 5000}
        )
        assert result.is_error is True
        assert len(_text(result)) < 300

    def test_short_value_is_unchanged(self) -> None:
        from receipt_index.mcp_server import _clip

        assert _clip("not-a-uuid") == "not-a-uuid"


class TestPaddedUuid:
    """UUID() strips hyphens, so a padded ID parses; it must not be echoed."""

    _PADDED = "-" * 5000 + _MISSING_ID.replace("-", "")

    def test_get_receipt_not_found_uses_canonical_id(
        self, conn: MagicMock, store_root: Path, log_path: Path
    ) -> None:
        with patch("receipt_index.repository.get_receipt_by_id", return_value=None):
            result = _call(
                _server(store_root, log_path),
                "get_receipt",
                {"receipt_id": self._PADDED},
            )
        assert result.is_error is True
        assert _text(result).endswith(f"Receipt not found: {_MISSING_ID}")
        assert len(_text(result)) < 200

    def test_get_receipt_file_uses_canonical_id(
        self, conn: MagicMock, store_root: Path, log_path: Path
    ) -> None:
        with patch("receipt_index.repository.get_receipt_by_id", return_value=None):
            result = _call(
                _server(store_root, log_path),
                "get_receipt_file",
                {"receipt_id": self._PADDED},
            )
        assert result.is_error is True
        assert _text(result).endswith(f"Receipt not found: {_MISSING_ID}")
        assert len(_text(result)) < 200
        (entry,) = _log_lines(log_path)
        assert entry["receipt_id"] == _MISSING_ID
        assert entry["outcome"] == "not_found"


class TestPaidAmountOutput:
    """The MCP output carries the total, the paid amount and the effective amount."""

    def test_get_receipt_paid_known(
        self, conn: MagicMock, store_root: Path, log_path: Path
    ) -> None:
        receipt = _receipt(
            amount=Decimal("50.00"),
            paid_amount=Decimal("30.00"),
            field_confidence={"vendor": 0.9, "paid_amount": 0.6},
        )
        with patch("receipt_index.repository.get_receipt_by_id", return_value=receipt):
            result = _call(
                _server(store_root, log_path), "get_receipt", {"receipt_id": str(_ID)}
            )

        assert result.is_error is False, _text(result)
        data = result.structured_content
        assert data is not None
        assert data["amount"] == "50.00"
        assert data["paid_amount"] == "30.00"
        assert data["effective_amount"] == "30.00"
        assert data["field_confidence"] == {"vendor": 0.9, "paid_amount": 0.6}

    def test_search_paid_unknown(
        self, conn: MagicMock, store_root: Path, log_path: Path
    ) -> None:
        with (
            patch(
                "receipt_index.repository.search_receipts",
                return_value=[_receipt(amount=Decimal("75.00"))],
            ),
            patch("receipt_index.repository.count_receipts", return_value=1),
        ):
            result = _call(_server(store_root, log_path), "search_receipts", {})

        assert result.is_error is False, _text(result)
        data = result.structured_content
        assert data is not None
        (receipt,) = data["receipts"]
        assert receipt["amount"] == "75.00"
        assert receipt["paid_amount"] is None
        assert receipt["effective_amount"] == "75.00"

    def test_failure_paid_amount_is_a_string(
        self, conn: MagicMock, store_root: Path, log_path: Path
    ) -> None:
        with (
            patch(
                "receipt_index.repository.get_ingest_failures",
                return_value=[_failure(1, paid_amount=Decimal("0"))],
            ),
            patch("receipt_index.repository.count_ingest_failures", return_value=1),
        ):
            result = _call(_server(store_root, log_path), "list_failures", {})

        assert result.is_error is False, _text(result)
        data = result.structured_content
        assert data is not None
        assert data["failures"][0]["paid_amount"] == "0"

    def test_instructions_cover_refunds(self) -> None:
        flat = " ".join(_INSTRUCTIONS.split())
        assert "for a refund, what was returned to them" in flat
        assert "a sum of effective_amount values counts them as charges" in flat

    def test_amount_descriptions_follow_the_design(self) -> None:
        schema = ReceiptOut.model_json_schema()["properties"]
        assert "total" in schema["amount"]["description"]
        assert "Paid amount" not in schema["amount"]["description"]
        assert "null" in schema["paid_amount"]["description"].lower()
        assert "otherwise the total" in schema["effective_amount"]["description"]


# ---------------------------------------------------------------------------
# Old and new store layouts through the evidence store
# ---------------------------------------------------------------------------


_READER_URL = "postgresql://reader@localhost/test"  # pragma: allowlist secret


class TestBothStoreLayouts:
    """A legacy YYYY/MM receipt and a new renditions/ receipt both come back.

    The server is built the way ``receipt-index mcp`` builds it, with the
    store, and fetches each file through the store's interface.
    """

    def test_legacy_and_new_receipts_are_returned(
        self, conn: MagicMock, store_root: Path, log_path: Path
    ) -> None:
        store = LocalEvidenceStore(store_root)
        new_bytes = b"%PDF-1.4\n% new layout\n%%EOF\n"
        new_path = pdf_path_for(
            store_rendition(
                store,
                new_bytes,
                receipt_date=date(2026, 10, 14),
                vendor="Acme Parking (U.S.), LLC",
                amount=Decimal("12.00"),
            ).ref
        )
        assert new_path.startswith("renditions/2026/10/")
        server = build_server(
            database_url=_READER_URL,
            store=store,
            access_log=AccessLog(log_path),
            max_content_bytes=1024,
        )

        for pdf_path, data in ((_PDF_PATH, _PDF_BYTES), (new_path, new_bytes)):
            with patch(
                "receipt_index.repository.get_receipt_by_id",
                return_value=_receipt(pdf_path=pdf_path),
            ):
                result = _call(
                    server,
                    "get_receipt_file",
                    {"receipt_id": str(_ID), "include_content": True},
                )
            assert result.is_error is False, _text(result)
            out = result.structured_content
            assert out is not None
            assert out["path"] == str((store_root / pdf_path).resolve())
            assert out["size_bytes"] == len(data)
            (resource,) = [c for c in result.content if c.type == "resource"]
            assert base64.b64decode(resource.resource.blob) == data

        entries = _log_lines(log_path)
        assert [e["pdf_path"] for e in entries] == [_PDF_PATH, new_path]
        assert [e["outcome"] for e in entries] == ["ok", "ok"]

    def test_store_or_store_root_but_not_both(
        self, store_root: Path, log_path: Path
    ) -> None:
        for kwargs in (
            {},
            {"store": LocalEvidenceStore(store_root), "store_root": store_root},
        ):
            with pytest.raises(ValueError, match="exactly one"):
                build_server(
                    database_url=_READER_URL,
                    access_log=AccessLog(log_path),
                    **kwargs,  # type: ignore[arg-type]
                )
