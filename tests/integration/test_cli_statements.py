"""The ``receipt-index statements`` commands against Postgres (plan U8).

Statements are written through the real write path (``process_statement``
with stubbed models), then read and changed through the CLI as the write
login. Every statement, institution and amount here is synthetic, and so is
every card-length number.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from typing import TYPE_CHECKING, Any
from uuid import UUID

import psycopg
import pytest
from click.testing import CliRunner
from psycopg import errors

from receipt_index import statement_repository as repo
from receipt_index.access_log import count_withheld
from receipt_index.cli import _statement_snapshot, cli
from receipt_index.statement_extraction import within_mask_bound
from receipt_index.store import LocalEvidenceStore
from tests.integration.conftest import _DB_DSN
from tests.integration.test_db_logins import (
    _WRITE_LOGIN,
    _config,
    logins,  # noqa: F401 (a fixture)
)
from tests.integration.test_db_logins import _dsn as _login_dsn
from tests.integration.test_mcp_statements import (
    _DEPOSIT_PASS,
    _MIDDLE,
    _PLANTED,
    _PLANTED_ACCOUNT,
    KindReplies,
    _call,
    _ingest,
    _ok,
    _server,
)
from tests.integration.test_mcp_stdio import roles  # noqa: F401 (a fixture)
from tests.integration.test_statement_pipeline import (
    _ACCOUNT,
    _GROUPED_NAME,
    Replies,
    _refused,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from click.testing import Result

    from receipt_index.statements import StatementAgents

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("roles", "logins")]

Conn = psycopg.Connection[dict[str, Any]]

# Every eight-digit window of the planted number: none may be printed.
_DIGITS = re.sub(r"\D", "", _PLANTED)
_WINDOWS = [_DIGITS[i : i + 8] for i in range(len(_DIGITS) - 7)]
_SHAPES = (
    _PLANTED,
    _PLANTED.replace(" ", "-"),
    " ".join(_MIDDLE),
    "-".join(_MIDDLE),
    "_".join(_MIDDLE),
    "".join(_MIDDLE),
)


@pytest.fixture
def evidence(tmp_path: Path) -> LocalEvidenceStore:
    # The CLI's config points at the same root (test_db_logins._config).
    root = tmp_path / "store"
    root.mkdir(exist_ok=True)
    return LocalEvidenceStore(root)


@pytest.fixture
def log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "mcp-logs" / "mcp-access.jsonl"
    monkeypatch.setenv("RECEIPT_INDEX_MCP_ACCESS_LOG", str(path))
    return path


@pytest.fixture
def config(tmp_path: Path) -> Path:
    """A CLI config that logs in as the write login only."""
    return _config(tmp_path, login=_WRITE_LOGIN, user="cli", source="cli-test")


class Run:
    """Runs the CLI with stubbed statement models for ``rerun``."""

    def __init__(self, config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.config = config
        self.replies: Replies = Replies()
        monkeypatch.setattr(
            "receipt_index.cli._rerun_agents", lambda _config: self._agents()
        )

    def _agents(self) -> StatementAgents:
        return self.replies.agents()

    def __call__(self, *args: str) -> Result:
        result = CliRunner().invoke(cli, ["--config", str(self.config), *args])
        if result.exception and not isinstance(result.exception, SystemExit):
            raise result.exception
        return result


@pytest.fixture
def run(config: Path, monkeypatch: pytest.MonkeyPatch) -> Run:
    return Run(config, monkeypatch)


# The text lines of `statements show` that carry free text.
_TEXT_LABELS = ("Institution:", "Section totals:", "Column totals:")


def _assert_no_planted(out: str) -> None:
    for window in _WINDOWS:
        assert window not in out, window
    for shape in _SHAPES:
        assert shape not in out, shape


def _history(conn: Conn) -> dict[str, list[dict[str, Any]]]:
    """Every row of the append-only tables, in a stable order."""
    rows = {
        table: conn.execute(f"SELECT * FROM receipt.{table} ORDER BY id").fetchall()
        for table in ("statement_revisions", "statement_accounts", "statement_lines")
    }
    conn.commit()
    return rows


def _statement(conn: Conn, statement_id: str) -> dict[str, Any]:
    row = conn.execute(
        """\
        SELECT s.change_seq, s.publish_count, s.status, r.revision, r.origin,
               r.masked_runs
        FROM receipt.statements s
        JOIN receipt.statement_revisions r ON r.id = s.current_revision_id
        WHERE s.id = %s
        """,
        (statement_id,),
    ).fetchone()
    conn.commit()
    assert row is not None
    return row


def _planted(pg_conn: Conn, evidence: LocalEvidenceStore) -> str:
    """A statement with a planted number in every free-text field."""
    replies = KindReplies(_DEPOSIT_PASS, {"deposit": _PLANTED_ACCOUNT})
    return _ingest(
        pg_conn, evidence, replies, page="Synthetic CLI page", name=_GROUPED_NAME
    )


def _plain(pg_conn: Conn, evidence: LocalEvidenceStore, page: str) -> str:
    return _ingest(pg_conn, evidence, Replies(), page=page)


# ---------------------------------------------------------------------------
# The mask, per command
# ---------------------------------------------------------------------------


def test_list_prints_no_planted_number(
    pg_conn: Conn, evidence: LocalEvidenceStore, run: Run
) -> None:
    statement_id = _planted(pg_conn, evidence)

    text = run("statements", "list")
    as_json = run("statements", "list", "--output", "json")

    assert text.exit_code == 0, text.output
    assert statement_id in text.output
    _assert_no_planted(text.output)
    assert as_json.exit_code == 0, as_json.output
    data = json.loads(as_json.stdout)
    assert [s["statement_id"] for s in data["statements"]] == [statement_id]
    _assert_no_planted(as_json.output)


def test_show_prints_the_planted_fields_within_the_bound(
    pg_conn: Conn, evidence: LocalEvidenceStore, run: Run, log: Path
) -> None:
    statement_id = _planted(pg_conn, evidence)

    text = run("statements", "show", statement_id)
    as_json = run("statements", "show", statement_id, "--output", "json")

    assert as_json.exit_code == 0, as_json.output
    data = json.loads(as_json.stdout)
    (account,) = data["accounts"]
    texts = [
        account["institution"],
        *(t["section"] for t in account["section_totals"]),
        *(t["section"] for t in account["column_totals"]),
    ]
    assert len(texts) == 3
    for value in texts:
        assert within_mask_bound(value), value
        assert not any(group in value for group in _MIDDLE), value
    assert "7319" in account["institution"]  # the masked last four
    _assert_no_planted(as_json.output)

    assert text.exit_code == 0, text.output
    _assert_no_planted(text.output)
    shown = [
        line
        for line in text.output.splitlines()
        if line.strip().startswith(_TEXT_LABELS)
    ]
    assert len(shown) == 3, text.output
    for line in shown:
        value = line.split(":", 1)[1]
        assert within_mask_bound(value), line
        assert not any(group in value for group in _MIDDLE), line
    assert "7319" in text.output
    # Nothing was withheld.
    assert not log.exists()


def test_file_prints_no_planted_number(
    pg_conn: Conn, evidence: LocalEvidenceStore, run: Run, log: Path
) -> None:
    statement_id = _planted(pg_conn, evidence)

    text = run("statements", "file", statement_id)
    as_json = run("statements", "file", statement_id, "--output", "json")

    for result in (text, as_json):
        assert result.exit_code == 0, result.output
        _assert_no_planted(result.output)
    assert json.loads(as_json.stdout)["exists"] is True


def test_rerun_prints_no_planted_number(
    pg_conn: Conn, evidence: LocalEvidenceStore, run: Run
) -> None:
    statement_id = _planted(pg_conn, evidence)
    run.replies = KindReplies(_DEPOSIT_PASS, {"deposit": _PLANTED_ACCOUNT})

    outputs = [
        run("statements", "rerun", statement_id),
        run("statements", "rerun", statement_id, "--output", "json"),
        run("statements", "rerun", statement_id, "--yes"),
        run("statements", "rerun", statement_id, "--yes", "--output", "json"),
    ]

    for result in outputs:
        assert result.exit_code == 0, result.output
        _assert_no_planted(result.output)
        assert "Synthetic" not in result.output  # no free text at all


def test_republish_prints_no_planted_number(
    pg_conn: Conn, evidence: LocalEvidenceStore, run: Run
) -> None:
    statement_id = _planted(pg_conn, evidence)

    outputs = [
        run("statements", "republish", statement_id),
        run("statements", "republish", "--all", "--output", "json"),
        run("statements", "republish", statement_id, "--yes"),
        run("statements", "republish", "--all", "--yes", "--output", "json"),
    ]

    for result in outputs:
        assert result.exit_code == 0, result.output
        _assert_no_planted(result.output)
        assert "Synthetic" not in result.output


# ---------------------------------------------------------------------------
# masked_runs (the plan's U8 entry)
# ---------------------------------------------------------------------------

_TWO_RUNS_ACCOUNT: dict[str, Any] = {
    **_ACCOUNT,
    "institution": f"Synthetic Savings Bank {_PLANTED}",
    "lines": [
        {**_ACCOUNT["lines"][0], "description": f"Synthetic ref {_PLANTED}"},
        _ACCOUNT["lines"][1],
    ],
}


def test_show_prints_masked_runs_in_text_and_json(
    pg_conn: Conn, evidence: LocalEvidenceStore, run: Run
) -> None:
    replies = KindReplies(_DEPOSIT_PASS, {"deposit": _TWO_RUNS_ACCOUNT})
    statement_id = _ingest(pg_conn, evidence, replies, page="Synthetic two runs")
    assert _statement(pg_conn, statement_id)["masked_runs"] == 2

    text = run("statements", "show", statement_id)
    as_json = run("statements", "show", statement_id, "--output", "json")

    assert text.exit_code == 0, text.output
    assert "Masked digit runs: 2" in text.output
    assert json.loads(as_json.stdout)["masked_runs"] == 2


# ---------------------------------------------------------------------------
# The read guard: withheld
# ---------------------------------------------------------------------------


def _withheld_line(result: Result) -> str:
    """The refusal's line on stderr (log lines may come before it)."""
    (line,) = [
        line for line in result.stderr.splitlines() if line.startswith("withheld: ")
    ]
    return line


def _past_the_mask(pg_conn: Conn, statement_id: str) -> None:
    """Write an institution the CHECK passes and the guard refuses.

    As the superuser: replica mode skips the append-only triggers.
    """
    pg_conn.execute("SET LOCAL session_replication_role = replica")
    pg_conn.execute(
        """\
        UPDATE receipt.statement_accounts a SET institution = %s
        FROM receipt.statements s
        WHERE s.id = %s AND a.revision_id = s.current_revision_id
        """,
        (f"Synthetic {_PLANTED}", statement_id),
    )
    pg_conn.commit()


def test_show_withholds_a_row_past_the_mask_and_logs_it(
    pg_conn: Conn, evidence: LocalEvidenceStore, run: Run, log: Path
) -> None:
    statement_id = _plain(pg_conn, evidence, "Synthetic withheld page")
    _past_the_mask(pg_conn, statement_id)
    revision = _statement(pg_conn, statement_id)["revision"]

    text = run("statements", "show", statement_id)
    as_json = run("statements", "show", statement_id, "--output", "json")

    for result in (text, as_json):
        assert result.exit_code == 1
        assert result.stdout == ""
        assert _withheld_line(result).startswith(
            f"withheld: statement {statement_id} revision {revision}: "
        ), result.stderr
        assert "accounts[1].institution" in result.stderr
        _assert_no_planted(result.output)
        assert "Synthetic" not in result.output
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert [(e["tool"], e["fields"], e["outcome"]) for e in entries] == [
        ("cli statements show", ["accounts[1].institution"], "withheld")
    ] * 2
    assert {e["statement_id"] for e in entries} == {statement_id}
    _assert_no_planted(log.read_text())
    withheld = count_withheld(log)
    assert withheld is not None
    assert withheld.count == 2


def test_show_withholds_even_when_the_access_log_cannot_be_written(
    pg_conn: Conn,
    evidence: LocalEvidenceStore,
    run: Run,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statement_id = _plain(pg_conn, evidence, "Synthetic unlogged page")
    _past_the_mask(pg_conn, statement_id)
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("")
    monkeypatch.setenv("RECEIPT_INDEX_MCP_ACCESS_LOG", str(blocked / "log.jsonl"))

    result = run("statements", "show", statement_id)

    assert result.exit_code == 1
    assert result.stdout == ""
    assert _withheld_line(result)
    assert "Warning: access log" in result.stderr


# ---------------------------------------------------------------------------
# One serialisation with the MCP tools
# ---------------------------------------------------------------------------


def test_show_and_list_json_equal_the_mcp_tools(
    pg_conn: Conn,
    evidence: LocalEvidenceStore,
    run: Run,
    tmp_path: Path,
) -> None:
    statement_id = _plain(pg_conn, evidence, "Synthetic drift page")
    other_id = _plain(pg_conn, evidence, "Synthetic other drift page")
    first = _statement(pg_conn, statement_id)["revision"]
    assert run("statements", "rerun", statement_id, "--yes").exit_code == 0
    server = _server(evidence, tmp_path / "server-access.jsonl")

    for revision in (None, first):
        args = ["statements", "show", statement_id, "--output", "json"]
        call: dict[str, Any] = {"statement_id": statement_id}
        if revision is not None:
            args += ["--revision", str(revision)]
            call["revision"] = revision
        shown = run(*args)
        assert shown.exit_code == 0, shown.output
        assert json.loads(shown.stdout) == _ok(_call(server, "get_statement", call))

    listed = json.loads(run("statements", "list", "--output", "json").stdout)
    feed = _ok(_call(server, "list_statement_changes", {}))
    assert listed["contract_version"] == feed["contract_version"]
    assert sorted(listed["statements"], key=lambda s: s["statement_id"]) == sorted(
        feed["changes"], key=lambda s: s["statement_id"]
    )
    assert {s["statement_id"] for s in listed["statements"]} == {
        statement_id,
        other_id,
    }


# ---------------------------------------------------------------------------
# Reads run in a READ ONLY transaction
# ---------------------------------------------------------------------------


def test_the_read_snapshot_refuses_a_write_even_without_the_session_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import receipt_index.db

    real = receipt_index.db.get_connection

    def without_default(url: str, **kwargs: Any) -> Any:
        # Drop the session default: only the transaction's own READ ONLY
        # may refuse the write.
        kwargs["read_only"] = False
        return real(url, **kwargs)

    monkeypatch.setattr(receipt_index.db, "get_connection", without_default)
    # As the superuser, so no missing grant can be what refuses it.
    with (
        pytest.raises(errors.ReadOnlySqlTransaction),
        _statement_snapshot(_DB_DSN) as conn,
    ):
        assert conn.execute("SHOW default_transaction_read_only").fetchone() == {
            "default_transaction_read_only": "off"
        }
        conn.execute("SELECT receipt.lock_statement_feed()")


@pytest.mark.parametrize("command", ["list", "show", "file"])
def test_list_show_and_file_read_through_the_snapshot(
    pg_conn: Conn,
    evidence: LocalEvidenceStore,
    run: Run,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    statement_id = _plain(pg_conn, evidence, f"Synthetic snapshot {command}")
    import receipt_index.cli

    seen: list[tuple[Any, Any]] = []
    real = receipt_index.cli._statement_snapshot

    def spy(url: str) -> Any:
        snapshot = real(url)
        conn = snapshot.__enter__()
        seen.append((conn.read_only, conn.isolation_level))
        return _Entered(snapshot, conn)

    monkeypatch.setattr(receipt_index.cli, "_statement_snapshot", spy)
    args = ["statements", command] + ([] if command == "list" else [statement_id])

    result = run(*args)

    assert result.exit_code == 0, result.output
    assert seen
    assert all(ro is True for ro, _ in seen)
    assert all(level == psycopg.IsolationLevel.REPEATABLE_READ for _, level in seen)


class _Entered:
    """A context manager already entered, for the snapshot spy."""

    def __init__(self, snapshot: Any, conn: Any) -> None:
        self.snapshot, self.conn = snapshot, conn

    def __enter__(self) -> Any:
        return self.conn

    def __exit__(self, *exc: object) -> Any:
        return self.snapshot.__exit__(*exc)


# ---------------------------------------------------------------------------
# rerun
# ---------------------------------------------------------------------------


def test_rerun_without_yes_writes_nothing_and_calls_no_model(
    pg_conn: Conn, evidence: LocalEvidenceStore, run: Run
) -> None:
    statement_id = _plain(pg_conn, evidence, "Synthetic preview page")
    before = (_history(pg_conn), _statement(pg_conn, statement_id))

    text = run("statements", "rerun", statement_id)
    as_json = run("statements", "rerun", statement_id, "--output", "json")

    assert (_history(pg_conn), _statement(pg_conn, statement_id)) == before
    assert run.replies.calls == []
    assert text.exit_code == 0, text.output
    said = " ".join(text.output.split())
    assert "becomes current whatever its result" in said
    assert "held or rejected, it is still current" in said
    assert "--revision" in said
    assert "Dry run" in said
    data = json.loads(as_json.stdout)
    assert data["dry_run"] is True
    assert data["current"]["revision"] == before[1]["revision"]
    assert data["current"]["accounts"][0]["line_count"] == 2


def test_rerun_appends_one_revision_and_edits_none(
    pg_conn: Conn, evidence: LocalEvidenceStore, run: Run
) -> None:
    statement_id = _plain(pg_conn, evidence, "Synthetic rerun page")
    before = _history(pg_conn)
    old = _statement(pg_conn, statement_id)
    run.replies = KindReplies(_DEPOSIT_PASS, {"deposit": _TWO_RUNS_ACCOUNT})

    result = run("statements", "rerun", statement_id, "--yes", "--output", "json")

    assert result.exit_code == 0, result.output
    after = _history(pg_conn)
    for table, rows in before.items():
        # Every earlier row is still there, unchanged.
        assert [r for r in after[table] if r in rows] == rows, table
    old_revisions = before["statement_revisions"]
    new_revisions = [r for r in after["statement_revisions"] if r not in old_revisions]
    assert len(new_revisions) == 1
    (added,) = new_revisions
    assert added["origin"] == "rerun"
    now = _statement(pg_conn, statement_id)
    assert now["revision"] == added["revision"] > old["revision"]
    assert now["change_seq"] > old["change_seq"]
    feed = repo.changes_after(pg_conn, 0, 100)
    pg_conn.commit()
    assert [c.revision for c in feed if str(c.statement_id) == statement_id] == [
        added["revision"]
    ]

    data = json.loads(result.stdout)
    assert data["dry_run"] is False
    assert data["previous"]["revision"] == old["revision"]
    assert data["current"]["revision"] == added["revision"]
    assert data["previous"]["masked_runs"] == 0
    assert data["current"]["masked_runs"] == 2
    assert data["masked_runs_changed"] is True
    assert data["current"]["accounts"][0]["line_count"] == 2


def test_rerun_report_in_text(
    pg_conn: Conn, evidence: LocalEvidenceStore, run: Run
) -> None:
    statement_id = _plain(pg_conn, evidence, "Synthetic rerun text page")
    run.replies = KindReplies(_DEPOSIT_PASS, {"deposit": _TWO_RUNS_ACCOUNT})

    result = run("statements", "rerun", statement_id, "--yes")

    assert result.exit_code == 0, result.output
    assert "Masked digit runs: 0 -> 2 (changed)" in result.output
    assert "2 lines" in result.output


def test_a_held_rerun_still_becomes_current(
    pg_conn: Conn, evidence: LocalEvidenceStore, run: Run
) -> None:
    statement_id = _plain(pg_conn, evidence, "Synthetic held rerun page")
    # Doesn't foot: the account is held by its own gates.
    run.replies = Replies(account={**_ACCOUNT, "ending_balance": "999.00"})

    result = run("statements", "rerun", statement_id, "--yes")

    assert result.exit_code == 0, result.output
    assert _statement(pg_conn, statement_id)["status"] == "held"
    said = " ".join(result.output.split())
    assert "held or rejected, it is still current" in said


def test_the_write_login_cannot_use_replica_mode() -> None:
    with (
        psycopg.connect(_login_dsn(_WRITE_LOGIN)) as conn,
        pytest.raises(errors.InsufficientPrivilege),
    ):
        conn.execute("SET session_replication_role = replica")


def test_a_failed_rerun_writes_nothing(
    pg_conn: Conn, evidence: LocalEvidenceStore, run: Run
) -> None:
    statement_id = _plain(pg_conn, evidence, "Synthetic failing rerun page")
    before = (_history(pg_conn), _statement(pg_conn, statement_id))
    run.replies = KindReplies(_DEPOSIT_PASS, {"deposit": _refused})

    result = run("statements", "rerun", statement_id, "--yes")

    assert result.exit_code == 1
    assert "refused" in result.stderr
    assert "nothing was written" in result.stderr
    assert (_history(pg_conn), _statement(pg_conn, statement_id)) == before


def test_a_rerun_refuses_a_stored_original_whose_hash_differs(
    pg_conn: Conn, evidence: LocalEvidenceStore, run: Run
) -> None:
    statement_id = _plain(pg_conn, evidence, "Synthetic changed original")
    path = _original_path(pg_conn, evidence, statement_id)
    path.chmod(0o644)
    path.write_bytes(b"%PDF-1.4 not the same bytes")
    before = (_history(pg_conn), _statement(pg_conn, statement_id))

    result = run("statements", "rerun", statement_id, "--yes")

    assert result.exit_code == 1
    assert "SHA-256" in result.stderr
    assert run.replies.calls == []
    assert (_history(pg_conn), _statement(pg_conn, statement_id)) == before


def _original_path(conn: Conn, evidence: LocalEvidenceStore, statement_id: str) -> Path:
    row = conn.execute(
        """\
        SELECT l.backend, l.locator FROM receipt.statements s
        JOIN receipt.file_locations l ON l.file_id = s.original_file_id
        WHERE s.id = %s AND l.purpose = 'original' AND l.owner = 'store'
        """,
        (statement_id,),
    ).fetchone()
    conn.commit()
    assert row is not None
    return evidence.local_path(f"{row['backend']}:{row['locator']}")


# ---------------------------------------------------------------------------
# republish
# ---------------------------------------------------------------------------


def test_republish_without_yes_changes_nothing(
    pg_conn: Conn, evidence: LocalEvidenceStore, run: Run
) -> None:
    statement_id = _plain(pg_conn, evidence, "Synthetic republish preview")
    before = (_history(pg_conn), _statement(pg_conn, statement_id))

    ids = run("statements", "republish", statement_id)
    every = run("statements", "republish", "--all")

    for result in (ids, every):
        assert result.exit_code == 0, result.output
        assert "Dry run" in result.output
        assert statement_id in result.output
    assert (_history(pg_conn), _statement(pg_conn, statement_id)) == before


def test_republish_moves_the_feed_and_touches_no_revision(
    pg_conn: Conn, evidence: LocalEvidenceStore, run: Run
) -> None:
    statement_id = _plain(pg_conn, evidence, "Synthetic republish page")
    before = _history(pg_conn)
    old = _statement(pg_conn, statement_id)

    result = run("statements", "republish", statement_id, "--yes", "--output", "json")

    assert result.exit_code == 0, result.output
    assert _history(pg_conn) == before
    now = _statement(pg_conn, statement_id)
    assert now["revision"] == old["revision"]
    assert now["change_seq"] > old["change_seq"]
    assert now["publish_count"] == old["publish_count"] + 1
    (entry,) = json.loads(result.stdout)["statements"]
    assert entry == {
        "statement_id": statement_id,
        "revision": old["revision"],
        "change_seq": old["change_seq"],
        "new_change_seq": now["change_seq"],
    }


def test_republish_with_an_unknown_id_writes_nothing(
    pg_conn: Conn, evidence: LocalEvidenceStore, run: Run
) -> None:
    statement_id = _plain(pg_conn, evidence, "Synthetic unknown republish")
    unknown = "019572a0-0000-7000-8000-00000000dead"
    before = _statement(pg_conn, statement_id)

    result = run("statements", "republish", statement_id, unknown, "--yes")

    assert result.exit_code == 1
    assert unknown in result.stderr
    assert _statement(pg_conn, statement_id) == before


def test_republish_all_commits_one_transaction_per_statement(
    pg_conn: Conn, evidence: LocalEvidenceStore, run: Run
) -> None:
    ids = [_plain(pg_conn, evidence, f"Synthetic all {n}") for n in range(3)]
    old = {i: _statement(pg_conn, i) for i in ids}

    result = run("statements", "republish", "--all", "--yes")

    assert result.exit_code == 0, result.output
    assert "Republished 3 statements" in result.output
    for i in ids:
        now = _statement(pg_conn, i)
        assert now["change_seq"] > old[i]["change_seq"]
        assert now["revision"] == old[i]["revision"]
    xmins = pg_conn.execute(
        "SELECT xmin::text AS x FROM receipt.statements WHERE id = ANY(%s)",
        ([UUID(i) for i in ids],),
    ).fetchall()
    pg_conn.commit()
    assert len({r["x"] for r in xmins}) == 3


# ---------------------------------------------------------------------------
# The feed lock
# ---------------------------------------------------------------------------


def _waiting_on_a_lock(conn: Conn, count: int) -> bool:
    row = conn.execute(
        "SELECT count(*) AS n FROM pg_stat_activity "
        "WHERE usename = %s AND wait_event_type = 'Lock'",
        (_WRITE_LOGIN,),
    ).fetchone()
    conn.commit()
    return row is not None and row["n"] >= count


def _in_thread(action: Callable[[], Result]) -> tuple[threading.Thread, list[Result]]:
    results: list[Result] = []
    thread = threading.Thread(target=lambda: results.append(action()))
    thread.start()
    return thread, results


def test_rerun_and_republish_wait_on_the_feed_lock_and_both_commit(
    pg_conn: Conn, evidence: LocalEvidenceStore, run: Run
) -> None:
    statement_id = _plain(pg_conn, evidence, "Synthetic lock page")
    old = _statement(pg_conn, statement_id)
    holder = psycopg.connect(_DB_DSN)
    holder.execute("SELECT receipt.lock_statement_feed()")
    watcher = psycopg.connect(_DB_DSN, row_factory=psycopg.rows.dict_row)
    try:
        rerun, rerun_out = _in_thread(
            lambda: run("statements", "rerun", statement_id, "--yes")
        )
        republish, republish_out = _in_thread(
            lambda: run("statements", "republish", statement_id, "--yes")
        )
        deadline = time.monotonic() + 30
        while not _waiting_on_a_lock(watcher, 2):
            assert time.monotonic() < deadline, "neither writer reached the lock"
            time.sleep(0.05)
        assert rerun.is_alive()
        assert republish.is_alive()
        holder.commit()
        rerun.join(60)
        republish.join(60)
    finally:
        holder.close()
        watcher.close()

    assert [r.exit_code for r in rerun_out + republish_out] == [0, 0], [
        r.output for r in rerun_out + republish_out
    ]
    now = _statement(pg_conn, statement_id)
    assert now["revision"] > old["revision"]
    assert now["publish_count"] == old["publish_count"] + 1
    assert now["change_seq"] > old["change_seq"]


# ---------------------------------------------------------------------------
# file
# ---------------------------------------------------------------------------


def test_file_logs_then_prints_the_path_and_never_opens_it(
    pg_conn: Conn,
    evidence: LocalEvidenceStore,
    run: Run,
    log: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statement_id = _plain(pg_conn, evidence, "Synthetic file page")
    expected = _original_path(pg_conn, evidence, statement_id)

    def no_open(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("statements file opened the original")

    monkeypatch.setattr(LocalEvidenceStore, "open", no_open)

    text = run("statements", "file", statement_id)
    as_json = run("statements", "file", statement_id, "--output", "json")

    assert text.exit_code == 0, text.output
    assert text.stdout == f"{expected}\n"
    data = json.loads(as_json.stdout)
    sha = hashlib.sha256(expected.read_bytes()).hexdigest()
    assert data == {
        "statement_id": statement_id,
        "source_sha256": sha,
        "ref": data["ref"],
        "path": str(expected),
        "exists": True,
    }
    assert data["ref"].startswith("local:")
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert [(e["tool"], e["statement_id"], e["outcome"]) for e in entries] == [
        ("cli statements file", statement_id, "path")
    ] * 2
    assert entries[0]["source_sha256"] == sha
    withheld = count_withheld(log)
    assert withheld is not None
    assert withheld.count == 0


def test_file_refuses_when_the_access_log_cannot_be_written(
    pg_conn: Conn,
    evidence: LocalEvidenceStore,
    run: Run,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statement_id = _plain(pg_conn, evidence, "Synthetic unlogged file")
    path = _original_path(pg_conn, evidence, statement_id)
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("")
    monkeypatch.setenv("RECEIPT_INDEX_MCP_ACCESS_LOG", str(blocked / "log.jsonl"))

    for args in ((), ("--output", "json")):
        result = run("statements", "file", statement_id, *args)
        assert result.exit_code == 1
        assert result.stdout == ""
        assert str(path) not in result.output
        assert "access log" in result.stderr


def test_file_marks_a_missing_original(
    pg_conn: Conn, evidence: LocalEvidenceStore, run: Run, log: Path
) -> None:
    statement_id = _plain(pg_conn, evidence, "Synthetic missing file")
    path = _original_path(pg_conn, evidence, statement_id)
    path.chmod(0o644)
    path.unlink()

    result = run("statements", "file", statement_id)

    assert result.exit_code == 0, result.output
    assert result.stdout == f"{path}  (missing)\n"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ("show",),
        ("show", "--revision", "1"),
        ("file",),
        ("rerun",),
        ("rerun", "--yes"),
    ],
)
def test_an_unknown_statement_exits_1(run: Run, args: tuple[str, ...]) -> None:
    unknown = "019572a0-0000-7000-8000-00000000beef"
    command, *rest = args

    result = run("statements", command, unknown, *rest)

    assert result.exit_code == 1
    assert result.stdout == ""
    assert f"no statement {unknown}" in result.stderr


def test_an_unknown_revision_exits_1(
    pg_conn: Conn, evidence: LocalEvidenceStore, run: Run
) -> None:
    statement_id = _plain(pg_conn, evidence, "Synthetic revision page")

    result = run("statements", "show", statement_id, "--revision", "7")

    assert result.exit_code == 1
    assert result.stdout == ""
    assert f"no revision 7 of statement {statement_id}" in result.stderr
