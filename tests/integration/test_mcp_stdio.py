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
from receipt_index.mcp_server import (
    AccessLog,
    RoleCheckError,
    build_server,
    check_read_only_role,
    startup_check,
)
from receipt_index.repository import insert_ingest_log, insert_receipt

from .ports import dsn

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer
    from mcp_types import CallToolResult

pytestmark = pytest.mark.integration

_PASSWORD = "localpass"  # pragma: allowlist secret


def _dsn(role: str) -> str:
    return dsn(role, _PASSWORD)


_READ_ROLE = "receipt_index_dev_read"
_WRITE_ROLE = "receipt_index_dev_write"
_COLUMN_WRITER = "receipt_index_mcp_test_column_writer"
_TRUNCATER = "receipt_index_mcp_test_truncater"
_SCHEMA_CREATOR = "receipt_index_mcp_test_schema_creator"
_NO_USAGE = "receipt_index_mcp_test_no_usage"
_NOINHERIT_MEMBER = "receipt_index_mcp_test_noinherit_member"
_INHERIT_FALSE_MEMBER = "receipt_index_mcp_test_inherit_false_member"
# Read roles that can also write one of the newer tables.
_HISTORY_WRITER = "receipt_index_mcp_test_history_writer"
_CURSOR_WRITER = "receipt_index_mcp_test_cursor_writer"

_CLI = str(Path(sys.executable).parent / "receipt-index")


def _ensure_login_role(conn: psycopg.Connection[dict[str, Any]], role: str) -> None:
    exists = conn.execute(
        "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
    ).fetchone()
    # A role that already exists may be a NOLOGIN one, which the migration
    # tests' scratch databases create when they run first: make it a login.
    verb = "CREATE" if exists is None else "ALTER"
    conn.execute(
        psycopg.sql.SQL(verb + " ROLE {} LOGIN PASSWORD {}").format(
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
    for role in (
        _READ_ROLE,
        _WRITE_ROLE,
        _COLUMN_WRITER,
        _TRUNCATER,
        _SCHEMA_CREATOR,
        _NO_USAGE,
    ):
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
        f"GRANT INSERT (source_id, vendor) ON receipt.receipts TO {_WRITE_ROLE}; "
        f"GRANT UPDATE (vendor) ON receipt.receipts TO {_WRITE_ROLE}"
    )
    pg_conn.execute(
        "GRANT SELECT ON receipt.receipts, receipt.ingest_log "
        f"TO {_COLUMN_WRITER}; "
        f"GRANT INSERT (vendor) ON receipt.receipts TO {_COLUMN_WRITER}"
    )
    pg_conn.execute(
        f"GRANT USAGE ON SCHEMA receipt TO {_TRUNCATER}, {_SCHEMA_CREATOR}; "
        "GRANT SELECT ON receipt.receipts, receipt.ingest_log "
        f"TO {_TRUNCATER}, {_SCHEMA_CREATOR}; "
        f"GRANT TRUNCATE ON receipt.ingest_log TO {_TRUNCATER}; "
        f"GRANT CREATE ON SCHEMA receipt TO {_SCHEMA_CREATOR}"
    )
    # _NO_USAGE gets CONNECT only, like a login role created without
    # membership in receipt_index_dev_read.
    pg_conn.execute(f"REVOKE ALL ON SCHEMA receipt FROM {_NO_USAGE}")
    # Read roles that are also members of the writer without inheriting its
    # privileges, so has_*_privilege on the login role alone sees no write.
    for role in (_NOINHERIT_MEMBER, _INHERIT_FALSE_MEMBER):
        _ensure_login_role(pg_conn, role)
        pg_conn.execute(
            f"GRANT USAGE ON SCHEMA receipt TO {role}; "
            f"GRANT SELECT ON receipt.receipts, receipt.ingest_log TO {role}"
        )
    for role in (_HISTORY_WRITER, _CURSOR_WRITER):
        _ensure_login_role(pg_conn, role)
        pg_conn.execute(
            f"GRANT USAGE ON SCHEMA receipt TO {role}; "
            f"GRANT SELECT ON receipt.receipts, receipt.ingest_log TO {role}"
        )
    pg_conn.execute(
        f"GRANT INSERT (output) ON receipt.extractions TO {_HISTORY_WRITER}"
    )
    pg_conn.execute(f"GRANT UPDATE ON receipt.sync_state TO {_CURSOR_WRITER}")
    pg_conn.execute(f"ALTER ROLE {_NOINHERIT_MEMBER} NOINHERIT")
    pg_conn.execute(f"GRANT {_WRITE_ROLE} TO {_NOINHERIT_MEMBER}")
    pg_conn.execute(f"ALTER ROLE {_INHERIT_FALSE_MEMBER} INHERIT")
    pg_conn.execute(
        f"GRANT {_WRITE_ROLE} TO {_INHERIT_FALSE_MEMBER} WITH INHERIT FALSE"
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


# ---------------------------------------------------------------------------
# In-process: the same tools against the real database, as the read role
# ---------------------------------------------------------------------------


def _in_process(store: Path, log: Path) -> MCPServer:
    return build_server(
        database_url=_dsn(_READ_ROLE),
        store_root=store,
        access_log=AccessLog(log),
    )


@pytest.mark.usefixtures("roles")
def test_tools_against_database_in_process(
    pg_conn: psycopg.Connection[dict[str, Any]],
    store: Path,
    tmp_path: Path,
    seeded: list[str],
) -> None:
    for n in range(3):
        insert_ingest_log(
            pg_conn,
            source_id=f"<fail-{n}@example.com>",
            source_type="imap",
            status="failed",
            error_message="extraction failed",
        )
    insert_ingest_log(
        pg_conn, source_id="<skip@example.com>", source_type="imap", status="skipped"
    )
    log = tmp_path / "mcp-access.jsonl"
    with pg_conn_as(_READ_ROLE) as reader:
        check_read_only_role(reader)

    async def session() -> dict[str, CallToolResult]:
        async with Client(_in_process(store, log)) as client:
            return {
                "exact": await client.call_tool("search_receipts", {"amount": "42.99"}),
                "range": await client.call_tool(
                    "search_receipts",
                    {
                        "amount_min": "7.00",
                        "amount_max": "12.50",
                        "date_from": "2025-06-10",
                        "date_to": "2025-06-15",
                        "source": "test-email",
                    },
                ),
                "none": await client.call_tool(
                    "search_receipts", {"vendor": "no-such-vendor"}
                ),
                "one": await client.call_tool("get_receipt", {"receipt_id": seeded[0]}),
                "file": await client.call_tool(
                    "get_receipt_file", {"receipt_id": seeded[2]}
                ),
                "failures": await client.call_tool(
                    "list_failures", {"limit": 2, "offset": 1}
                ),
            }

    r = anyio.run(session)

    exact = r["exact"].structured_content
    assert exact is not None
    assert [x["id"] for x in exact["receipts"]] == [seeded[1]]
    assert exact["total"] == 1

    in_range = r["range"].structured_content
    assert in_range is not None
    # 2025-06-15 is 42.99 (over the max), 2025-06-10 is 7.00 (in range).
    assert [x["id"] for x in in_range["receipts"]] == [seeded[2]]

    none = r["none"].structured_content
    assert none == {
        "receipts": [],
        "total": 0,
        "limit": 50,
        "offset": 0,
        "next_offset": None,
    }

    one = r["one"].structured_content
    assert one is not None
    assert one["amount"] == "12.50"
    assert one["file"]["exists"] is True

    file_result = r["file"].structured_content
    assert file_result is not None
    assert file_result["content_omitted_reason"] == "not_requested"
    assert file_result["size_bytes"] == len(b"%PDF-1.4 7.00")

    failures = r["failures"].structured_content
    assert failures is not None
    assert failures["total"] == 3
    assert len(failures["failures"]) == 2
    assert failures["next_offset"] is None
    assert all(f["status"] == "failed" for f in failures["failures"])

    (entry,) = [json.loads(line) for line in log.read_text().splitlines()]
    assert entry["receipt_id"] == seeded[2]
    assert entry["outcome"] == "ok"


def pg_conn_as(role: str) -> psycopg.Connection[dict[str, Any]]:
    return psycopg.connect(_dsn(role), row_factory=psycopg.rows.dict_row)


@pytest.mark.usefixtures("roles")
def test_startup_check_passes_read_role() -> None:
    startup_check(_dsn(_READ_ROLE))


@pytest.mark.usefixtures("roles")
@pytest.mark.parametrize(
    ("role", "match"),
    [
        (_COLUMN_WRITER, r"receipt\.receipts: INSERT"),
        (_TRUNCATER, r"receipt\.ingest_log: TRUNCATE"),
        (_SCHEMA_CREATOR, r"schema receipt: CREATE"),
        (_WRITE_ROLE, r"receipt\.receipts: UPDATE"),
        (_NO_USAGE, r"lacks SELECT"),
        (_NOINHERIT_MEMBER, r"receipt\.receipts: DELETE"),
        (_INHERIT_FALSE_MEMBER, r"receipt\.receipts: DELETE"),
        (_HISTORY_WRITER, r"receipt\.extractions: INSERT"),
        (_CURSOR_WRITER, r"receipt\.sync_state: UPDATE"),
    ],
)
def test_startup_check_refuses_in_process(role: str, match: str) -> None:
    with pytest.raises(RoleCheckError, match=match):
        startup_check(_dsn(role))
