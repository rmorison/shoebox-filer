"""The MCP statement tools against Postgres, as the read role (plan U7).

Statements are written through the real write path (``process_statement``
with stubbed models, or the repository's own feed writes), then read back
through the tools in process, as an MCP client would. Every statement,
institution and amount here is synthetic, and so is every card-length
number.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import anyio
import psycopg
import pytest
from mcp import Client
from psycopg.rows import dict_row
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from receipt_index import statement_repository as repo
from receipt_index.access_log import count_withheld
from receipt_index.mcp_server import (
    AccessLog,
    RoleCheckError,
    build_server,
    startup_check,
)
from receipt_index.statement_extraction import within_mask_bound
from receipt_index.store import LocalEvidenceStore
from tests.integration.conftest import _DB_DSN, apply_migration_grants
from tests.integration.test_mcp_stdio import (
    _READ_ROLE,
    _READ_TABLES,
    _dsn,
    _ensure_login_role,
    roles,  # noqa: F401 (a fixture)
)
from tests.integration.test_statement_pipeline import (
    _ACCOUNT,
    _FIRST_PASS,
    _GROUPED_NAME,
    _PAGE,
    Replies,
    _capped,
    _raw,
    _refused,
    _run,
)
from tests.pdfs import make_pdf

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mcp.server.mcpserver import MCPServer
    from mcp_types import CallToolResult

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("roles")]

# A synthetic card-length number in groups, which the database CHECK (eight
# ASCII digits in a row) can't see, and its middle groups, which must never
# come back.
_PLANTED = "4929 3817 6620 7319"
_MIDDLE = ("3817", "6620")
# The same first two groups in Arabic-Indic digits: eight in a row, which the
# CHECK reads as no digits at all.
_ARABIC_INDIC = "\u0664\u0669\u0662\u0669\u0663\u0668\u0661\u0667"

_DEPOSIT_PASS = _FIRST_PASS


def _first_pass(*kinds: str, document_type: str = "deposit") -> dict[str, Any]:
    return {
        **_FIRST_PASS,
        "document_type": document_type,
        "accounts": [
            {
                "label": f"Account {n}",
                "account_type": kind,
                "first_page": 1,
                "last_page": 1,
            }
            for n, kind in enumerate(kinds, start=1)
        ],
        "printed_account_count": len(kinds),
    }


class KindReplies(Replies):
    """Stubbed models with one reply per account kind, or a shaped response."""

    def __init__(
        self,
        first_pass: dict[str, Any],
        by_kind: dict[str, dict[str, Any] | Callable[[str], ModelResponse]],
    ) -> None:
        super().__init__(first_pass=first_pass)
        self.by_kind = by_kind

    def _model(self, kind: str) -> FunctionModel:
        if kind == "first_pass" or kind not in self.by_kind:
            return super()._model(kind)
        reply_for = self.by_kind[kind]

        def reply(_: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            self.calls.append(kind)
            tool = info.output_tools[0].name
            if callable(reply_for):
                return reply_for(tool)
            return ModelResponse(parts=[ToolCallPart(tool, reply_for)])

        return FunctionModel(reply)


@pytest.fixture(autouse=True)
def _migration_grants(pg_conn: psycopg.Connection[dict[str, Any]]) -> None:
    """The read role holds what the migrations grant it, and only that.

    Applied here, so these tests don't depend on an earlier module having
    applied them; ``roles`` then adds its test logins on top.
    """
    apply_migration_grants(pg_conn)
    pg_conn.commit()


@pytest.fixture
def evidence(tmp_path: Path) -> LocalEvidenceStore:
    root = tmp_path / "store"
    root.mkdir()
    return LocalEvidenceStore(root)


@pytest.fixture
def log(tmp_path: Path) -> Path:
    return tmp_path / "mcp-access.jsonl"


def _server(evidence: LocalEvidenceStore, log: Path) -> MCPServer:
    return build_server(
        database_url=_dsn(_READ_ROLE), store=evidence, access_log=AccessLog(log)
    )


def _calls(
    server: MCPServer, *calls: tuple[str, dict[str, Any]]
) -> list[CallToolResult]:
    async def session() -> list[CallToolResult]:
        async with Client(server) as client:
            return [await client.call_tool(name, args) for name, args in calls]

    return anyio.run(session)


def _call(server: MCPServer, name: str, args: dict[str, Any]) -> CallToolResult:
    (result,) = _calls(server, (name, args))
    return result


def _ok(result: CallToolResult) -> dict[str, Any]:
    assert not result.is_error, _text(result)
    assert result.structured_content is not None
    return result.structured_content


def _text(result: CallToolResult) -> str:
    return " ".join(getattr(part, "text", "") for part in result.content)


def _refusal(result: CallToolResult) -> str:
    assert result.is_error
    return _text(result)


def _dump(result: CallToolResult) -> str:
    return json.dumps(result.model_dump(mode="json"), ensure_ascii=False)


def _ingest(
    conn: psycopg.Connection[dict[str, Any]],
    evidence: LocalEvidenceStore,
    replies: Replies,
    *,
    page: str,
    name: str = "statement.pdf",
) -> str:
    """Run one synthetic statement through the pipeline; return its ID."""
    # The pipeline's own page text, long enough not to read as a scan.
    data = make_pdf(f"{_PAGE} ({page})")
    outcome = _run(conn, _raw(data, name=name), evidence, replies)
    assert outcome.status == "success"
    row = conn.execute(
        "SELECT id FROM receipt.statements WHERE source_sha256 = %s",
        (hashlib.sha256(data).digest(),),
    ).fetchone()
    assert row is not None
    return str(row["id"])


def _all_lines(
    server: MCPServer, statement_id: str, revision: int, ordinal: int
) -> Any:
    lines: list[dict[str, Any]] = []
    offset: int | None = 0
    while offset is not None:
        page = _ok(
            _call(
                server,
                "get_statement_lines",
                {
                    "statement_id": statement_id,
                    "revision": revision,
                    "account_ordinal": ordinal,
                    "offset": offset,
                },
            )
        )
        lines.extend(page["lines"])
        offset = page["next_offset"]
    return lines


# ---------------------------------------------------------------------------
# The mask, per tool
# ---------------------------------------------------------------------------

_SECTION = f"Fees {_PLANTED.replace(' ', '-')}"
_PLANTED_ACCOUNT: dict[str, Any] = {
    "institution": f"Synthetic Bank {_PLANTED}",
    "last4": "7319",
    "currency": "USD",
    "beginning_balance": "100.00",
    "ending_balance": "90.00",
    "line_order": "oldest_first",
    "section_totals": [{"section": _SECTION, "amount": "10.00"}],
    "column_totals": [{"section": _SECTION, "debits": "10.00"}],
    "lines": [
        {
            "posted": {"month": 1, "day": 5, "year": None},
            "description": f"Synthetic fee ref {_PLANTED}",
            "amount": "-10.00",
            "running_balance": "90.00",
            "section": _SECTION,
            "page": 1,
        }
    ],
}


def _free_text(statement: dict[str, Any], lines: list[dict[str, Any]]) -> list[str]:
    texts: list[str] = []
    for account in statement["accounts"]:
        texts.append(account["institution"])
        texts.extend(t["section"] for t in account["section_totals"])
        texts.extend(t["section"] for t in account["column_totals"])
    for line in lines:
        texts.extend((line["description"], line["section"]))
    return texts


def test_a_planted_number_comes_back_within_the_bound_from_every_tool(
    pg_conn: psycopg.Connection[dict[str, Any]],
    evidence: LocalEvidenceStore,
    log: Path,
) -> None:
    replies = KindReplies(_DEPOSIT_PASS, {"deposit": _PLANTED_ACCOUNT})
    statement_id = _ingest(
        pg_conn, evidence, replies, page="Synthetic planted page", name=_GROUPED_NAME
    )
    # A statement item that fails, under the same grouped name, for
    # list_failures.
    failing = KindReplies(_DEPOSIT_PASS, {"deposit": _refused})
    _run(
        pg_conn,
        _raw(make_pdf(f"{_PAGE} (failing)"), name=_GROUPED_NAME),
        evidence,
        failing,
    )
    server = _server(evidence, log)

    changes, statement, failures = _calls(
        server,
        ("list_statement_changes", {}),
        ("get_statement", {"statement_id": statement_id}),
        ("list_failures", {}),
    )
    got = _ok(statement)
    (account,) = got["accounts"]
    assert account["status"] == "clean", account
    lines = _all_lines(server, statement_id, got["revision"], 1)
    assert len(lines) == 1

    texts = _free_text(got, lines)
    assert len(texts) == 5
    for text in texts:
        assert within_mask_bound(text), text
        assert not any(group in text for group in _MIDDLE), text
    assert got["masked_runs"] >= 5
    assert _ok(failures)["total"] >= 1
    for result in (changes, statement, failures):
        out = _dump(result)
        for shape in (_PLANTED, _PLANTED.replace(" ", "-"), "".join(_MIDDLE)):
            assert shape not in out
        assert "3817_6620" not in out
    # Nothing was withheld: what the mask wrote passes the read check.
    assert not log.exists()


# ---------------------------------------------------------------------------
# The read guard: rows the CHECK passes, the tools refuse
# ---------------------------------------------------------------------------


def test_rows_the_check_passes_but_the_guard_does_not_are_withheld(
    pg_conn: psycopg.Connection[dict[str, Any]],
    evidence: LocalEvidenceStore,
    log: Path,
) -> None:
    statement_id = _ingest(pg_conn, evidence, Replies(), page="Synthetic guard page")
    # Written past the mask, as a superuser: replica mode skips the tables'
    # append-only triggers, but the CHECKs still run, and pass.
    pg_conn.execute("SET LOCAL session_replication_role = replica")
    pg_conn.execute(
        "UPDATE receipt.statement_lines SET description = %s WHERE ordinal = 1",
        (f"Synthetic {_PLANTED}",),
    )
    pg_conn.execute(
        "UPDATE receipt.statement_accounts SET institution = %s",
        (f"Synthetic {_ARABIC_INDIC}",),
    )
    pg_conn.commit()
    server = _server(evidence, log)

    statement, changes = _calls(
        server,
        ("get_statement", {"statement_id": statement_id}),
        ("list_statement_changes", {}),
    )
    revision = _ok(changes)["changes"][0]["revision"]
    (lines,) = _calls(
        server,
        (
            "get_statement_lines",
            {"statement_id": statement_id, "revision": revision, "account_ordinal": 1},
        ),
    )

    for result in (statement, lines):
        message = _refusal(result)
        assert message.startswith("withheld: "), message
        assert "held for review" in message
        out = _dump(result)
        assert _PLANTED not in out
        assert _ARABIC_INDIC not in out
        assert "Synthetic" not in out

    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert [(e["tool"], e["fields"], e["account_ordinal"]) for e in entries] == [
        ("get_statement", ["accounts[1].institution"], None),
        ("get_statement_lines", ["lines[1].description"], 1),
    ]
    assert all(e["outcome"] == "withheld" for e in entries)
    assert {e["statement_id"] for e in entries} == {statement_id}
    raw_log = log.read_text(encoding="utf-8")
    assert _PLANTED not in raw_log
    assert _ARABIC_INDIC not in raw_log
    withheld = count_withheld(log)
    assert withheld is not None
    assert withheld.count == 2


# Rows written past the mask in shapes the database CHECK passes: each holds
# a run the mask would cut, so the guard refuses it (review round 1).
_Q_PROBE = "Synthetic 4929 3817 6620 Q 7319"
_THREE_GROUPS = "Synthetic 4929-3817-6620"


@pytest.mark.parametrize("text", [_Q_PROBE, _THREE_GROUPS])
def test_a_row_the_mask_would_cut_is_withheld(
    pg_conn: psycopg.Connection[dict[str, Any]],
    evidence: LocalEvidenceStore,
    log: Path,
    text: str,
) -> None:
    statement_id = _ingest(pg_conn, evidence, Replies(), page="Synthetic run page")
    pg_conn.execute("SET LOCAL session_replication_role = replica")
    pg_conn.execute(
        "UPDATE receipt.statement_lines SET description = %s WHERE ordinal = 2",
        (text,),
    )
    pg_conn.commit()
    server = _server(evidence, log)
    revision = _feed(server, None)["changes"][0]["revision"]

    result = _call(
        server,
        "get_statement_lines",
        {"statement_id": statement_id, "revision": revision, "account_ordinal": 1},
    )

    assert _refusal(result).startswith("withheld: ")
    out = _dump(result)
    assert "3817" not in out
    assert "6620" not in out
    (entry,) = [json.loads(line) for line in log.read_text().splitlines()]
    assert entry["fields"] == ["lines[2].description"]


@pytest.mark.parametrize(
    ("amount", "label"),
    [
        ("4929381766207319", "a card-length digit string"),
        ("1" * 30, "past Decimal's default precision"),
        ("1E+30", "an exponent form"),
    ],
)
def test_a_stored_total_that_is_not_money_is_withheld(
    pg_conn: psycopg.Connection[dict[str, Any]],
    evidence: LocalEvidenceStore,
    log: Path,
    amount: str,
    label: str,
) -> None:
    statement_id = _ingest(pg_conn, evidence, Replies(), page="Synthetic totals page")
    # No CHECK reads the totals' amounts.
    pg_conn.execute("SET LOCAL session_replication_role = replica")
    pg_conn.execute(
        "UPDATE receipt.statement_accounts SET section_totals = %s::jsonb",
        (json.dumps([{"section": "fees", "amount": amount}]),),
    )
    pg_conn.commit()
    server = _server(evidence, log)

    result = _call(server, "get_statement", {"statement_id": statement_id})

    assert _refusal(result).startswith("withheld: "), label
    assert amount not in _dump(result), label
    (entry,) = [json.loads(line) for line in log.read_text().splitlines()]
    assert entry["fields"] == ["accounts[1].section_totals[0].amount"], label


def test_an_offset_past_64_bits_is_refused_before_postgres(
    pg_conn: psycopg.Connection[dict[str, Any]],
    evidence: LocalEvidenceStore,
    log: Path,
) -> None:
    statement_id = _ingest(pg_conn, evidence, Replies(), page="Synthetic offset page")
    server = _server(evidence, log)
    revision = _feed(server, None)["changes"][0]["revision"]
    args = {"statement_id": statement_id, "revision": revision, "account_ordinal": 1}

    past, last = _calls(
        server,
        ("get_statement_lines", {**args, "offset": 2**63}),
        ("get_statement_lines", {**args, "offset": 2**63 - 1}),
    )

    assert _refusal(past).startswith("invalid_argument: offset must be from ")
    assert _ok(last)["lines"] == []


# ---------------------------------------------------------------------------
# The change feed
# ---------------------------------------------------------------------------


def _feed(server: MCPServer, cursor: str | None) -> dict[str, Any]:
    args = {} if cursor is None else {"cursor": cursor}
    return _ok(_call(server, "list_statement_changes", args))


def test_two_interleaved_writers_never_skip_a_change(
    pg_conn: psycopg.Connection[dict[str, Any]],
    evidence: LocalEvidenceStore,
    log: Path,
) -> None:
    first = _ingest(pg_conn, evidence, Replies(), page="Synthetic feed page one")
    second = _ingest(pg_conn, evidence, Replies(), page="Synthetic feed page two")
    server = _server(evidence, log)
    start = _feed(server, None)["next_cursor"]

    a = psycopg.connect(_DB_DSN, row_factory=dict_row)
    b = psycopg.connect(_DB_DSN, row_factory=dict_row)
    watcher = psycopg.connect(_DB_DSN, row_factory=dict_row, autocommit=True)
    b_written, b_go = threading.Event(), threading.Event()
    errors: list[BaseException] = []

    def write_second() -> None:
        try:
            repo.republish(b, uuid.UUID(second))  # waits on the feed lock
            b_written.set()
            assert b_go.wait(30)
            b.commit()
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)
            b_written.set()

    thread = threading.Thread(target=write_second)
    try:
        # A takes the next place in the feed and holds the lock, uncommitted.
        repo.republish(a, uuid.UUID(first))
        thread.start()
        pid = b.info.backend_pid
        for _ in range(600):
            row = watcher.execute(
                "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (pid,)
            ).fetchone()
            if row is not None and row["wait_event_type"] == "Lock":
                break
            threading.Event().wait(0.05)
        else:
            pytest.fail("the second writer never waited on the feed lock")

        # Neither change is visible: the reader's cursor doesn't move.
        page = _feed(server, start)
        assert page["changes"] == []
        assert page["next_cursor"] == start

        a.commit()
        assert b_written.wait(30)
        # A is visible; B holds a later place, uncommitted.
        page = _feed(server, start)
        assert [c["statement_id"] for c in page["changes"]] == [first]
        cursor = page["next_cursor"]

        b_go.set()
        thread.join(30)
        assert not errors, errors
        page = _feed(server, cursor)
        assert [c["statement_id"] for c in page["changes"]] == [second]
        assert int(page["next_cursor"][3:]) > int(cursor[3:])
    finally:
        b_go.set()
        thread.join(30)
        for conn in (a, b, watcher):
            conn.close()


def test_a_cursor_ahead_of_the_counter_returns_no_changes(
    pg_conn: psycopg.Connection[dict[str, Any]],
    evidence: LocalEvidenceStore,
    log: Path,
) -> None:
    _ingest(pg_conn, evidence, Replies(), page="Synthetic ahead page")
    server = _server(evidence, log)
    latest = _feed(server, None)["next_cursor"]
    ahead = f"s1:{int(latest[3:]) + 1000}"

    page = _feed(server, ahead)

    assert page == {"contract_version": 1, "changes": [], "next_cursor": ahead}


# ---------------------------------------------------------------------------
# Revisions, held accounts and paging
# ---------------------------------------------------------------------------


def test_an_earlier_revision_reports_itself_and_the_current_one(
    pg_conn: psycopg.Connection[dict[str, Any]],
    evidence: LocalEvidenceStore,
    log: Path,
) -> None:
    statement_id = _ingest(pg_conn, evidence, Replies(), page="Synthetic rerun page")
    sid = uuid.UUID(statement_id)
    repo.lock_feed(pg_conn)
    revision_id, _ = repo.insert_revision(
        pg_conn,
        sid,
        repo.RevisionRow(
            origin="rerun",
            document_type="deposit",
            status="held",
            hold_reason="no_accounts",
            pages=1,
        ),
    )
    repo.set_current_revision(
        pg_conn,
        statement_id=sid,
        revision_id=revision_id,
        status="held",
        document_type="deposit",
    )
    pg_conn.commit()
    server = _server(evidence, log)

    current = _ok(_call(server, "get_statement", {"statement_id": statement_id}))
    (change,) = _feed(server, None)["changes"]
    revisions = pg_conn.execute(
        "SELECT revision FROM receipt.statement_revisions"
        " WHERE statement_id = %s ORDER BY revision",
        (sid,),
    ).fetchall()
    older, newer = (r["revision"] for r in revisions)
    earlier = _ok(
        _call(
            server, "get_statement", {"statement_id": statement_id, "revision": older}
        )
    )

    assert (current["revision"], current["is_current"]) == (newer, True)
    assert (current["status"], current["hold_reason"]) == ("held", "no_accounts")
    assert current["accounts"] == []
    assert (change["statement_id"], change["revision"]) == (statement_id, newer)
    assert (earlier["revision"], earlier["is_current"]) == (older, False)
    assert earlier["current_revision"] == newer
    assert earlier["status"] == "complete"
    assert len(_all_lines(server, statement_id, older, 1)) == 2


def test_a_held_account_returns_no_lines(
    pg_conn: psycopg.Connection[dict[str, Any]],
    evidence: LocalEvidenceStore,
    log: Path,
) -> None:
    unfooted = {**_ACCOUNT, "ending_balance": "999.00"}
    replies = KindReplies(_DEPOSIT_PASS, {"deposit": unfooted})
    statement_id = _ingest(pg_conn, evidence, replies, page="Synthetic held page")
    server = _server(evidence, log)

    got = _ok(_call(server, "get_statement", {"statement_id": statement_id}))
    (account,) = got["accounts"]
    page = _ok(
        _call(
            server,
            "get_statement_lines",
            {
                "statement_id": statement_id,
                "revision": got["revision"],
                "account_ordinal": 1,
            },
        )
    )

    assert (account["status"], account["failure_class"]) == ("held", "does_not_foot")
    assert account["line_count"] == 0
    assert page["lines"] == []
    assert (page["total"], page["next_offset"]) == (0, None)


def test_pages_of_lines_join_to_the_line_count(
    pg_conn: psycopg.Connection[dict[str, Any]],
    evidence: LocalEvidenceStore,
    log: Path,
) -> None:
    lines = [
        {
            "posted": {"month": 1, "day": day, "year": None},
            "description": f"Synthetic purchase {day}",
            "amount": "-1.00",
            "running_balance": None,
            "page": 1,
        }
        for day in range(2, 9)
    ]
    account = {**_ACCOUNT, "ending_balance": "93.00", "lines": lines}
    replies = KindReplies(_DEPOSIT_PASS, {"deposit": account})
    statement_id = _ingest(pg_conn, evidence, replies, page="Synthetic paging page")
    server = _server(evidence, log)
    got = _ok(_call(server, "get_statement", {"statement_id": statement_id}))
    assert got["accounts"][0]["line_count"] == 7

    pages = []
    offset: int | None = 0
    while offset is not None:
        page = _ok(
            _call(
                server,
                "get_statement_lines",
                {
                    "statement_id": statement_id,
                    "revision": got["revision"],
                    "account_ordinal": 1,
                    "limit": 3,
                    "offset": offset,
                },
            )
        )
        pages.append(page)
        offset = page["next_offset"]

    assert [len(p["lines"]) for p in pages] == [3, 3, 1]
    assert {p["total"] for p in pages} == {7}
    joined = [line for p in pages for line in p["lines"]]
    assert [line["ordinal"] for line in joined] == list(range(1, 8))
    assert [line["description"] for line in joined] == [
        f"Synthetic purchase {day}" for day in range(2, 9)
    ]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def test_each_error_code(
    pg_conn: psycopg.Connection[dict[str, Any]],
    evidence: LocalEvidenceStore,
    log: Path,
) -> None:
    statement_id = _ingest(pg_conn, evidence, Replies(), page="Synthetic errors page")
    server = _server(evidence, log)
    revision = _ok(_call(server, "get_statement", {"statement_id": statement_id}))[
        "revision"
    ]
    lines = {"statement_id": statement_id, "revision": revision, "account_ordinal": 1}
    cases: list[tuple[str, dict[str, Any], str]] = [
        ("list_statement_changes", {"cursor": "garbage"}, "invalid_cursor"),
        ("list_statement_changes", {"cursor": "s2:5"}, "invalid_cursor"),
        ("list_statement_changes", {"limit": 0}, "invalid_argument"),
        ("list_statement_changes", {"limit": 201}, "invalid_argument"),
        ("get_statement", {"statement_id": "not-a-uuid"}, "invalid_id"),
        ("get_statement", {"statement_id": str(uuid.uuid4())}, "not_found"),
        (
            "get_statement",
            {"statement_id": statement_id, "revision": revision + 1},
            "not_found",
        ),
        ("get_statement_lines", {**lines, "account_ordinal": 9}, "not_found"),
        ("get_statement_lines", {**lines, "revision": revision + 1}, "not_found"),
        ("get_statement_lines", {**lines, "limit": 1001}, "invalid_argument"),
        ("get_statement_lines", {**lines, "offset": -1}, "invalid_argument"),
    ]

    results = _calls(server, *[(name, args) for name, args, _ in cases])

    for (name, args, code), result in zip(cases, results, strict=True):
        message = _refusal(result)
        assert message.startswith(f"{code}: "), (name, args, message)
    assert "garbage" not in _text(results[0])
    assert "not-a-uuid" not in _text(results[4])


# ---------------------------------------------------------------------------
# The digest, recomputed from what the tools return
# ---------------------------------------------------------------------------

_LOAN = {
    "institution": "Synthetic Mortgage Co",
    "last4": "9876",
    "principal_before": "1000.00",
    "principal_after": "900.00",
    "principal_paid": "100.00",
    "interest": "5.00",
}
_FORM_FIELDS = (
    "account_type",
    "institution",
    "last4",
    "currency",
    "period_start",
    "period_end",
    "status",
    "held_by_statement",
    "failure_class",
    "difference",
    "beginning_balance",
    "ending_balance",
    "section_totals",
    "column_totals",
    "line_order",
    "loan",
)
_LINE_FIELDS = (
    "posted_date",
    "transaction_date",
    "description",
    "amount",
    "running_balance",
    "section",
    "page",
)


def _digest(account: dict[str, Any], lines: list[dict[str, Any]]) -> str:
    """The contract's digest, from the contract's fields alone."""
    form = {field: account[field] for field in _FORM_FIELDS}
    form["lines"] = [{field: line[field] for field in _LINE_FIELDS} for line in lines]
    data = json.dumps(form, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def test_a_digest_recomputed_from_the_tools_matches(
    pg_conn: psycopg.Connection[dict[str, Any]],
    evidence: LocalEvidenceStore,
    log: Path,
) -> None:
    mixed = _ingest(
        pg_conn,
        evidence,
        KindReplies(
            _first_pass("deposit", "loan", document_type="loan_statement"),
            {"deposit": _ACCOUNT, "loan": _LOAN},
        ),
        page="Synthetic digest page",
    )
    capped = _ingest(
        pg_conn,
        evidence,
        KindReplies(
            _first_pass("loan", "deposit", document_type="loan_statement"),
            {"loan": _capped, "deposit": _ACCOUNT},
        ),
        page="Synthetic capped page",
    )
    server = _server(evidence, log)

    seen: dict[str, dict[str, Any]] = {}
    for statement_id in (mixed, capped):
        got = _ok(_call(server, "get_statement", {"statement_id": statement_id}))
        for account in got["accounts"]:
            lines = _all_lines(
                server, statement_id, got["revision"], account["ordinal"]
            )
            assert len(lines) == account["line_count"]
            assert _digest(account, lines) == account["content_digest"], account
            seen[f"{statement_id}:{account['ordinal']}"] = account

    loan = seen[f"{mixed}:2"]
    assert loan["status"] == "figures_only"
    assert loan["loan"] == {
        "principal_before": "1000.00",
        "principal_after": "900.00",
        "principal_paid": "100.00",
        "interest": "5.00",
        "escrow": None,
        "fees": None,
        "advances": None,
    }
    assert seen[f"{mixed}:1"]["loan"] is None
    cut_off = seen[f"{capped}:1"]
    assert (cut_off["failure_class"], cut_off["loan"]) == ("too_many_lines", None)
    for account in seen.values():
        for field in ("beginning_balance", "ending_balance"):
            value = account[field]
            assert value is None or Decimal(value).as_tuple().exponent == -2


# ---------------------------------------------------------------------------
# No original over MCP
# ---------------------------------------------------------------------------


def test_no_tool_returns_a_statement_originals_path_or_bytes(
    pg_conn: psycopg.Connection[dict[str, Any]],
    evidence: LocalEvidenceStore,
    log: Path,
    tmp_path: Path,
) -> None:
    data = make_pdf(f"{_PAGE} (original)")
    outcome = _run(pg_conn, _raw(data, name="original.pdf"), evidence, Replies())
    assert outcome.status == "success"
    statement_id = str(
        pg_conn.execute("SELECT id FROM receipt.statements").fetchone()["id"]  # type: ignore[index]
    )
    stored = [p for p in (tmp_path / "store").rglob("*") if p.is_file()]
    assert stored
    server = _server(evidence, log)

    async def session() -> tuple[list[str], list[CallToolResult]]:
        async with Client(server) as client:
            tools = [tool.name for tool in (await client.list_tools()).tools]
            got = await client.call_tool(
                "get_statement", {"statement_id": statement_id}
            )
            revision = got.structured_content["revision"]  # type: ignore[index]
            return tools, [
                got,
                await client.call_tool("list_statement_changes", {}),
                await client.call_tool(
                    "get_statement_lines",
                    {
                        "statement_id": statement_id,
                        "revision": revision,
                        "account_ordinal": 1,
                    },
                ),
                await client.call_tool("search_receipts", {}),
                await client.call_tool("list_failures", {}),
                await client.call_tool("get_receipt", {"receipt_id": statement_id}),
                await client.call_tool(
                    "get_receipt_file",
                    {"receipt_id": statement_id, "include_content": True},
                ),
            ]

    tools, results = anyio.run(session)

    assert not any("file" in name and "statement" in name for name in tools)
    encoded = base64.b64encode(data).decode()
    for result in results:
        out = _dump(result)
        assert encoded[:64] not in out
        assert "%PDF" not in out
        assert "originals/" not in out
        assert "original.pdf" not in out
        for path in stored:
            assert path.name not in out
            assert str(path) not in out
    # The receipt tools don't know a statement's ID.
    assert results[5].is_error
    assert results[6].is_error


# ---------------------------------------------------------------------------
# The startup check
# ---------------------------------------------------------------------------

_NO_LINES_READER = "receipt_index_mcp_test_no_lines_reader"
_ACCOUNT_WRITER = "receipt_index_mcp_test_account_writer"


@pytest.fixture
def statement_roles(pg_conn: psycopg.Connection[dict[str, Any]]) -> None:
    for role in (_NO_LINES_READER, _ACCOUNT_WRITER):
        _ensure_login_role(pg_conn, role)
        pg_conn.execute(f"GRANT USAGE ON SCHEMA receipt TO {role}")
    pg_conn.execute(f"GRANT SELECT ON {_READ_TABLES} TO {_NO_LINES_READER}")
    pg_conn.execute(f"REVOKE SELECT ON receipt.statement_lines FROM {_NO_LINES_READER}")
    pg_conn.execute(f"GRANT SELECT ON {_READ_TABLES} TO {_ACCOUNT_WRITER}")
    pg_conn.execute(
        f"GRANT INSERT (institution) ON receipt.statement_accounts TO {_ACCOUNT_WRITER}"
    )
    pg_conn.commit()


@pytest.mark.usefixtures("statement_roles")
@pytest.mark.parametrize(
    ("role", "match"),
    [
        (_NO_LINES_READER, r"lacks SELECT on .*receipt\.statement_lines"),
        (_ACCOUNT_WRITER, r"receipt\.statement_accounts: INSERT"),
    ],
)
def test_the_startup_check_covers_the_statement_tables(role: str, match: str) -> None:
    with pytest.raises(RoleCheckError, match=match):
        startup_check(_dsn(role))
