"""Integration tests for the read-only MCP server over stdio.

Requires the devtest Postgres (see tests/integration/conftest.py). Each test
starts ``receipt-index mcp`` as a subprocess and talks to it over stdio with
the MCP SDK's client, as an MCP client would.

The roles below are created here, as the ``main`` superuser, because the CI
database has no application roles and the conftest bootstrap recreates the
tables without their grants.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import textwrap
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import psycopg
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp_types import Implementation

from receipt_index.db import get_connection
from receipt_index.repository import insert_receipt

if TYPE_CHECKING:
    from mcp_types import CallToolResult

pytestmark = pytest.mark.integration

_PORT = os.environ.get("POSTGRES_HOST_PORT", "15432")
_PASSWORD = "localpass"  # pragma: allowlist secret


def _dsn(role: str) -> str:
    return f"postgresql://{role}:{_PASSWORD}@localhost:{_PORT}/receipt_index_dev"


_READ_ROLE = "receipt_index_dev_read"
_WRITE_ROLE = "receipt_index_dev_write"
_COLUMN_WRITER = "receipt_index_mcp_test_column_writer"

_CLI = str(Path(sys.executable).parent / "receipt-index")


def _ensure_login_role(conn: psycopg.Connection[dict[str, Any]], role: str) -> None:
    exists = conn.execute(
        "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
    ).fetchone()
    if exists is None:
        conn.execute(
            psycopg.sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                psycopg.sql.Identifier(role), psycopg.sql.Literal(_PASSWORD)
            )
        )
    conn.execute(
        psycopg.sql.SQL("GRANT CONNECT ON DATABASE receipt_index_dev TO {}").format(
            psycopg.sql.Identifier(role)
        )
    )


@pytest.fixture()
def roles(pg_conn: psycopg.Connection[dict[str, Any]]) -> None:
    """Login roles with the grants migrations 000002/000004 give, plus one
    that holds only a column-level INSERT."""
    for role in (_READ_ROLE, _WRITE_ROLE, _COLUMN_WRITER):
        _ensure_login_role(pg_conn, role)
    pg_conn.execute(
        f"GRANT USAGE ON SCHEMA receipt TO {_READ_ROLE}, {_WRITE_ROLE}, "
        f"{_COLUMN_WRITER}"
    )
    pg_conn.execute(
        f"GRANT SELECT ON receipt.receipts, receipt.ingest_log TO {_READ_ROLE}"
    )
    pg_conn.execute(
        f"GRANT SELECT, DELETE ON receipt.receipts TO {_WRITE_ROLE}; "
        f"GRANT SELECT ON receipt.ingest_log TO {_WRITE_ROLE}; "
        f"GRANT INSERT (source_id, vendor) ON receipt.receipts TO {_WRITE_ROLE}"
    )
    pg_conn.execute(
        "GRANT SELECT ON receipt.receipts, receipt.ingest_log "
        f"TO {_COLUMN_WRITER}; "
        f"GRANT INSERT (vendor) ON receipt.receipts TO {_COLUMN_WRITER}"
    )
    pg_conn.commit()


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    return tmp_path / "store"


@pytest.fixture()
def seeded(pg_conn: psycopg.Connection[dict[str, Any]], store: Path) -> list[str]:
    """Insert three receipts with PDFs; return their IDs, newest first."""
    ids = []
    for day, vendor, amount in [
        (date(2025, 6, 20), "Acme Parking (U.S.), LLC", Decimal("12.50")),
        (date(2025, 6, 15), "Acme Parking (U.S.), LLC", Decimal("42.99")),
        (date(2025, 6, 10), "Acme Parking (U.S.), LLC", Decimal("7.00")),
    ]:
        rel = f"2025/06/{day.isoformat()}__acme__{amount}.pdf"
        (store / rel).parent.mkdir(parents=True, exist_ok=True)
        (store / rel).write_bytes(b"%PDF-1.4 " + str(amount).encode())
        receipt = insert_receipt(
            pg_conn,
            source_id=f"<{day.isoformat()}@example.com>",
            source_type="imap",
            source_name="test-email",
            vendor=vendor,
            amount=amount,
            currency="USD",
            receipt_date=day,
            description=None,
            confidence=0.9,
            pdf_path=rel,
            email_subject="Receipt",
            email_sender="billing@example.com",
            email_date=None,
        )
        ids.append(str(receipt.id))
    return ids


@pytest.fixture()
def config_file(tmp_path: Path, store: Path) -> Path:
    path = tmp_path / "receipt-index.yaml"
    path.write_text(
        textwrap.dedent(f"""\
            sources:
              - name: test-email
                type: imap
                host: mail.example.com
                username: user@example.com
                password: unused
            database:
              url: {_dsn("main")}
            store:
              path: {store}
            llm:
              api_key: unused
            logging:
              level: INFO
            """)
    )
    return path


def _env(database_url: str) -> dict[str, str]:
    env = dict(os.environ)
    env["RECEIPT_INDEX_MCP_DATABASE_URL"] = database_url
    env.pop("RECEIPT_INDEX_CONFIG", None)
    return env


def _params(config_file: Path, log: Path, database_url: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=_CLI,
        args=["--config", str(config_file), "mcp", "--access-log", str(log)],
        env=_env(database_url),
        cwd=str(config_file.parent),
    )


@pytest.mark.usefixtures("roles")
def test_lists_tools_and_searches_over_stdio(
    config_file: Path, tmp_path: Path, seeded: list[str]
) -> None:
    log = tmp_path / "mcp-access.jsonl"

    async def session() -> tuple[set[str], CallToolResult, CallToolResult]:
        async with Client(
            _params(config_file, log, _dsn(_READ_ROLE)),
            client_info=Implementation(name="integration-client", version="1.0"),
        ) as client:
            tools = {t.name for t in (await client.list_tools()).tools}
            page1 = await client.call_tool(
                "search_receipts", {"vendor": "acme", "limit": 2}
            )
            page2 = await client.call_tool(
                "search_receipts", {"vendor": "acme", "limit": 2, "offset": 2}
            )
            return tools, page1, page2

    tools, page1, page2 = anyio.run(session)

    assert tools == {
        "search_receipts",
        "get_receipt",
        "get_receipt_file",
        "list_failures",
    }
    first = page1.structured_content
    second = page2.structured_content
    assert first is not None
    assert second is not None
    assert page1.is_error is False
    assert first["total"] == 3
    assert first["next_offset"] == 2
    assert [r["id"] for r in first["receipts"]] == seeded[:2]
    assert [r["amount"] for r in first["receipts"]] == ["12.50", "42.99"]
    assert all(r["file"]["exists"] for r in first["receipts"])
    assert second["next_offset"] is None
    assert [r["id"] for r in second["receipts"]] == seeded[2:]


@pytest.mark.usefixtures("roles")
def test_file_fetch_returns_content_and_is_logged(
    config_file: Path, tmp_path: Path, seeded: list[str]
) -> None:
    log = tmp_path / "mcp-access.jsonl"

    async def session() -> CallToolResult:
        async with Client(
            _params(config_file, log, _dsn(_READ_ROLE)),
            client_info=Implementation(name="integration-client", version="1.0"),
        ) as client:
            return await client.call_tool(
                "get_receipt_file",
                {"receipt_id": seeded[1], "include_content": True},
            )

    result = anyio.run(session)

    assert result.is_error is False
    (resource,) = [c for c in result.content if c.type == "resource"]
    assert base64.b64decode(resource.resource.blob) == b"%PDF-1.4 42.99"
    (entry,) = [json.loads(line) for line in log.read_text().splitlines()]
    assert entry["receipt_id"] == seeded[1]
    assert entry["outcome"] == "ok"
    assert entry["content_included"] is True
    assert entry["client"] == {"name": "integration-client", "version": "1.0"}


@pytest.mark.usefixtures("roles")
@pytest.mark.parametrize(
    ("role", "privilege"),
    [
        (_WRITE_ROLE, "receipt.receipts: DELETE"),
        (_COLUMN_WRITER, "receipt.receipts: INSERT"),
        ("main", "receipt.receipts: INSERT"),
    ],
)
def test_refuses_to_start_on_writable_role(
    config_file: Path, tmp_path: Path, role: str, privilege: str
) -> None:
    proc = subprocess.run(
        [
            _CLI,
            "--config",
            str(config_file),
            "mcp",
            "--access-log",
            str(tmp_path / "log.jsonl"),
        ],
        env=_env(_dsn(role)),
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 1
    assert "the database role can write" in proc.stderr
    assert privilege in proc.stderr
    assert proc.stdout == ""


@pytest.mark.usefixtures("roles")
def test_read_only_connection_rejects_writes() -> None:
    # Even the superuser can't write over a read-only connection.
    with (
        get_connection(_dsn("main"), read_only=True) as conn,
        pytest.raises(psycopg.errors.ReadOnlySqlTransaction),
    ):
        conn.execute("DELETE FROM receipt.receipts")
