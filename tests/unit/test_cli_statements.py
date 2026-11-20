"""The ``statements`` commands' argument checks, refusals and text, no database.

The commands against a real database are in
``tests/integration/test_cli_statements.py``.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, call, patch
from uuid import UUID

import pytest
from click.testing import CliRunner

from receipt_index.cli import cli
from receipt_index.statement_repository import (
    RepublishStoppedError,
    republish_each,
)
from receipt_index.statements import RerunOutcome
from tests.unit.test_cli import _MINIMAL_CONFIG_YAML, _make_config

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_ID = "0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"
# Input that isn't a UUID and must never be echoed back.
_NOT_A_UUID = "not-a-uuid-planted-text"


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "receipt-index.yaml"
    path.write_text(_MINIMAL_CONFIG_YAML)
    return path


@pytest.fixture
def unwritable_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the access log under a regular file, so it can't be written."""
    blocker = tmp_path / "blocker"
    blocker.write_text("")
    path = blocker / "access.jsonl"
    monkeypatch.setenv("RECEIPT_INDEX_MCP_ACCESS_LOG", str(path))
    return path


@pytest.fixture
def snapshot() -> Iterator[MagicMock]:
    """Patch the config and the read snapshot; yield the snapshot's connection."""
    conn = MagicMock(name="conn")

    @contextmanager
    def fake_snapshot(_url: str) -> Iterator[MagicMock]:
        yield conn

    with (
        patch("receipt_index.cli.load_config", return_value=_make_config()),
        patch("receipt_index.cli._statement_snapshot", fake_snapshot),
    ):
        yield conn


def _invoke(config_file: Path, *args: str) -> Any:
    return CliRunner().invoke(cli, ["--config", str(config_file), "statements", *args])


_REVISION: dict[str, Any] = {
    "revision_id": UUID(int=1),
    "statement_id": UUID(_ID),
    "revision": 3,
    "is_current": True,
    "current_revision": 3,
    "created_at": datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
    "status": "held",
    "hold_reason": "account_held",
    "document_type": "bank_statement",
    "rejected_bound": None,
    "masked_runs": 2,
    "input_tokens": 12000,
    "output_tokens": 3400,
    "source_sha256": bytes(32),
}
_ACCOUNT_ROW: dict[str, Any] = {
    "ordinal": 1,
    "account_type": "checking",
    "last4": "0000",
    "status": "held",
    "failure_class": "balance_mismatch",
    "held_by_statement": False,
    "line_count": 5,
}


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------


def test_republish_refuses_ids_with_all(config_file: Path, snapshot: MagicMock) -> None:
    result = _invoke(config_file, "republish", _ID, "--all")
    assert result.exit_code == 2
    assert "Give statement IDs or --all, not both." in result.output


def test_republish_refuses_neither_ids_nor_all(
    config_file: Path, snapshot: MagicMock
) -> None:
    result = _invoke(config_file, "republish")
    assert result.exit_code == 2
    assert "Give statement IDs or --all, not both." in result.output


def test_republish_names_a_bad_id_by_position_only(
    config_file: Path, snapshot: MagicMock
) -> None:
    with patch("receipt_index.statement_repository.feed_positions") as positions:
        result = _invoke(config_file, "republish", _ID, _NOT_A_UUID, "--yes")
    assert result.exit_code == 1
    assert "statement ID 2 is not a UUID; nothing was republished." in result.stderr
    assert _NOT_A_UUID not in result.output
    positions.assert_not_called()


def test_republish_writes_nothing_when_an_id_is_unknown(
    config_file: Path, snapshot: MagicMock
) -> None:
    known = {"statement_id": UUID(_ID), "revision": 1, "change_seq": 7}
    other = "0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5c"
    with (
        patch(
            "receipt_index.statement_repository.feed_positions", return_value=[known]
        ),
        patch("receipt_index.statement_repository.republish_each") as each,
        patch("receipt_index.db.get_connection") as write_conn,
    ):
        result = _invoke(config_file, "republish", _ID, other, "--yes")
    assert result.exit_code == 1
    assert f"no statement {other}; nothing was republished." in result.stderr
    each.assert_not_called()
    write_conn.assert_not_called()


@pytest.mark.parametrize("command", ["show", "file", "rerun"])
def test_a_statement_id_that_is_not_a_uuid_is_not_echoed(
    config_file: Path, snapshot: MagicMock, command: str
) -> None:
    result = _invoke(config_file, command, _NOT_A_UUID)
    assert result.exit_code == 1
    assert "the statement ID is not a UUID" in result.stderr
    assert _NOT_A_UUID not in result.output


@pytest.mark.parametrize("revision", [str(2**63), str(-(2**63) - 1)])
def test_show_refuses_a_revision_past_bigint(
    config_file: Path, snapshot: MagicMock, revision: str
) -> None:
    with patch("receipt_index.statement_repository.get_revision") as get:
        result = _invoke(config_file, "show", _ID, "--revision", revision)
    assert result.exit_code == 1
    assert "--revision is out of range" in result.stderr
    get.assert_not_called()


def test_list_refuses_an_unknown_status(config_file: Path, snapshot: MagicMock) -> None:
    result = _invoke(config_file, "list", "--status", "pending")
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# Refusals when the access log can't be written
# ---------------------------------------------------------------------------


def test_show_withholds_even_when_the_access_log_cannot_be_written(
    config_file: Path, snapshot: MagicMock, unwritable_log: Path
) -> None:
    with (
        patch(
            "receipt_index.statement_repository.get_revision", return_value=_REVISION
        ),
        patch("receipt_index.statement_repository.get_accounts", return_value=[{}]),
        patch("receipt_index.statement_contract.account_out", return_value={}),
        patch(
            "receipt_index.statement_contract.statement_read_failures",
            return_value=["institution"],
        ),
        patch("receipt_index.statement_contract.statement_out") as shaped,
    ):
        result = _invoke(config_file, "show", _ID)
    assert result.exit_code == 1
    assert result.stdout == ""
    shaped.assert_not_called()
    assert (
        f"withheld: statement {_ID} revision 3: a stored field failed the read "
        "check (institution)."
    ) in result.stderr
    assert "Warning: access log" in result.stderr
    assert not unwritable_log.exists()


def test_file_prints_no_path_when_the_access_log_cannot_be_written(
    config_file: Path, snapshot: MagicMock, unwritable_log: Path, tmp_path: Path
) -> None:
    location = {"source_sha256": bytes(32), "ref": "originals/ab/cd.pdf"}
    store = MagicMock()
    store.local_path.return_value = tmp_path / "store" / "originals" / "ab" / "cd.pdf"
    with (
        patch(
            "receipt_index.statement_repository.original_location",
            return_value=location,
        ),
        patch("receipt_index.store.open_store", return_value=store),
    ):
        result = _invoke(config_file, "file", _ID)
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "; no path printed." in result.stderr
    assert "cd.pdf" not in result.output


def test_file_refuses_a_statement_with_no_stored_original(
    config_file: Path, snapshot: MagicMock
) -> None:
    location = {"source_sha256": bytes(32), "ref": None}
    with patch(
        "receipt_index.statement_repository.original_location", return_value=location
    ):
        result = _invoke(config_file, "file", _ID)
    assert result.exit_code == 1
    assert f"statement {_ID} has no stored original." in result.stderr


# ---------------------------------------------------------------------------
# The rerun preview
# ---------------------------------------------------------------------------


def test_the_rerun_preview_calls_no_model_and_says_what_becomes_current(
    config_file: Path, snapshot: MagicMock
) -> None:
    with (
        patch(
            "receipt_index.statement_repository.get_revision", return_value=_REVISION
        ),
        patch(
            "receipt_index.statement_repository.get_accounts",
            return_value=[_ACCOUNT_ROW],
        ),
        patch("receipt_index.cli._rerun_agents") as agents,
        patch("receipt_index.statements.rerun_statement") as rerun,
        patch("receipt_index.db.get_connection") as write_conn,
    ):
        result = _invoke(config_file, "rerun", _ID)
    assert result.exit_code == 0, result.output
    agents.assert_not_called()
    rerun.assert_not_called()
    write_conn.assert_not_called()
    lines = result.stdout.splitlines()
    assert lines[:9] == [
        f"Statement:         {_ID}",
        f"SHA-256:           {'00' * 32}",
        "Current revision:  3 (extracted 2026-11-01T12:00:00.000000Z)",
        "Status:            held (account_held)",
        "Type:              bank_statement",
        "Masked digit runs: 2",
        "Last run's tokens: 12,000 input, 3,400 output",
        "Accounts:          1",
        "  [1] checking 0000: held (balance_mismatch), 5 lines",
    ]
    text = " ".join(lines)
    assert "Dry run: nothing was changed." in text
    assert "becomes current whatever its result" in text
    assert "if it is held or rejected, it is still current" in text
    assert f"`statements show {_ID} --revision 3`" in text


def test_the_rerun_preview_in_json_is_marked_a_dry_run(
    config_file: Path, snapshot: MagicMock
) -> None:
    import json

    with (
        patch(
            "receipt_index.statement_repository.get_revision", return_value=_REVISION
        ),
        patch(
            "receipt_index.statement_repository.get_accounts",
            return_value=[_ACCOUNT_ROW],
        ),
        patch("receipt_index.cli._rerun_agents") as agents,
    ):
        result = _invoke(config_file, "rerun", _ID, "--output", "json")
    assert result.exit_code == 0, result.output
    agents.assert_not_called()
    data = json.loads(result.stdout)
    assert data["dry_run"] is True
    assert data["current"]["revision"] == 3
    assert data["current"]["accounts"] == [_ACCOUNT_ROW]


# ---------------------------------------------------------------------------
# republish_each
# ---------------------------------------------------------------------------


def test_republish_each_commits_after_every_statement() -> None:
    conn = MagicMock(name="conn")
    ids = [UUID(int=1), UUID(int=2)]
    with patch(
        "receipt_index.statement_repository.republish", side_effect=[11, 12]
    ) as republish:
        assert republish_each(conn, ids) == [11, 12]
    assert republish.call_args_list == [call(conn, ids[0]), call(conn, ids[1])]
    # One commit before, to start clean, then one per statement.
    assert conn.commit.call_count == 3


def test_republish_each_keeps_the_ones_before_a_failure() -> None:
    conn = MagicMock(name="conn")
    with (
        patch(
            "receipt_index.statement_repository.republish",
            side_effect=[11, LookupError("no statement")],
        ),
        pytest.raises(RepublishStoppedError) as stopped,
    ):
        republish_each(conn, [UUID(int=1), UUID(int=2)])
    assert conn.commit.call_count == 2
    conn.rollback.assert_called_once_with()
    assert stopped.value.statement_id == UUID(int=2)
    assert stopped.value.republished == [11]


# ---------------------------------------------------------------------------
# Review round 1
# ---------------------------------------------------------------------------


def test_file_logs_with_the_shared_entry_and_echoes_it_at_info(
    config_file: Path,
    snapshot: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    log = tmp_path / "logs" / "access.jsonl"
    monkeypatch.setenv("RECEIPT_INDEX_MCP_ACCESS_LOG", str(log))
    location = {"source_sha256": bytes(32), "ref": "originals/ab/cd.pdf"}
    store = MagicMock()
    path = tmp_path / "store" / "originals" / "ab" / "cd.pdf"
    store.local_path.return_value = path
    with (
        patch(
            "receipt_index.statement_repository.original_location",
            return_value=location,
        ),
        patch("receipt_index.store.open_store", return_value=store),
    ):
        result = _invoke(config_file, "file", _ID)
    assert result.exit_code == 0, result.output
    assert result.stdout == f"{path}  (missing)\n"
    (line,) = log.read_text().splitlines()
    entry = json.loads(line)
    assert set(entry) == {
        "ts",
        "tool",
        "statement_id",
        "source_sha256",
        "outcome",
        "client",
    }
    assert entry["tool"] == "cli statements file"
    assert entry["outcome"] == "path"
    assert entry["source_sha256"] == "00" * 32
    # As documented: the line is also logged at INFO on stderr.
    assert f"access: {line}" in result.stderr


@contextmanager
def _rerun_yes(
    outcome: RerunOutcome | Exception, revisions: dict[int | None, dict[str, Any]]
) -> Iterator[MagicMock]:
    """Patch a --yes rerun: the run's result and the revisions read back."""

    def get_revision(_conn: Any, _uid: UUID, revision: int | None = None) -> Any:
        return revisions[revision]

    rerun = MagicMock(name="rerun_statement")
    if isinstance(outcome, Exception):
        rerun.side_effect = outcome
    else:
        rerun.return_value = outcome
    with (
        patch(
            "receipt_index.statement_repository.get_revision",
            side_effect=get_revision,
        ) as get,
        patch(
            "receipt_index.statement_repository.get_accounts",
            return_value=[_ACCOUNT_ROW],
        ),
        patch("receipt_index.cli._rerun_agents"),
        patch("receipt_index.store.open_store"),
        patch("receipt_index.db.get_connection"),
        patch("receipt_index.statements.rerun_statement", rerun),
    ):
        yield get


def test_the_rerun_report_reads_the_revision_it_wrote(
    config_file: Path, snapshot: MagicMock
) -> None:
    written = {**_REVISION, "revision": 4, "is_current": False, "current_revision": 5}
    newer = {**_REVISION, "revision": 5, "status": "complete", "hold_reason": None}
    revisions = {None: _REVISION, 3: _REVISION, 4: written, 5: newer}
    with _rerun_yes(RerunOutcome(3, 4, 99), revisions) as get:
        result = _invoke(config_file, "rerun", _ID, "--yes")
    assert result.exit_code == 0, result.output
    assert [c.args[2] for c in get.call_args_list] == [None, 4]
    lines = result.stdout.splitlines()
    assert lines[0] == f"Rerun of statement {_ID}: revision 3 -> 4, made current."
    assert lines[1] == (
        "Revision 4 is no longer current: revision 5 was written after it."
    )


def test_the_rerun_report_rereads_the_revision_it_replaced(
    config_file: Path, snapshot: MagicMock
) -> None:
    replaced = {**_REVISION, "revision": 4, "masked_runs": 7}
    written = {**_REVISION, "revision": 5, "masked_runs": 7}
    revisions = {None: _REVISION, 4: replaced, 5: written}
    with _rerun_yes(RerunOutcome(4, 5, 99), revisions) as get:
        result = _invoke(config_file, "rerun", _ID, "--yes")
    assert result.exit_code == 0, result.output
    assert [c.args[2] for c in get.call_args_list] == [None, 5, 4]
    assert "revision 4 -> 5, made current." in result.stdout
    assert "Masked digit runs: 7 -> 7 (unchanged)" in result.stdout


_PLANTED = "planted-statement-text"


@pytest.mark.parametrize(
    "error", [ValueError(_PLANTED), OSError(5, _PLANTED), AssertionError(_PLANTED)]
)
def test_an_unexpected_rerun_error_is_named_by_class_only(
    config_file: Path, snapshot: MagicMock, error: Exception
) -> None:
    with _rerun_yes(error, {None: _REVISION}):
        result = _invoke(config_file, "rerun", _ID, "--yes")
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert (
        f"Error: the rerun failed ({type(error).__name__}); nothing was written."
        in result.stderr
    )
    assert _PLANTED not in result.output


def test_a_failed_read_back_names_the_written_revision_by_class_only(
    config_file: Path, snapshot: MagicMock
) -> None:
    import psycopg

    calls = 0

    def get_revision(_conn: Any, _uid: UUID, revision: int | None = None) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _REVISION
        raise psycopg.OperationalError(_PLANTED)

    with (
        _rerun_yes(RerunOutcome(3, 4, 99), {None: _REVISION}),
        patch(
            "receipt_index.statement_repository.get_revision",
            side_effect=get_revision,
        ),
    ):
        result = _invoke(config_file, "rerun", _ID, "--yes")
    assert result.exit_code == 1
    assert (
        "revision 4 was written and made current, but reading it back failed "
        "(OperationalError)."
    ) in result.stderr
    assert _PLANTED not in result.output


def test_republish_reports_how_many_were_done_when_one_disappears(
    config_file: Path, snapshot: MagicMock
) -> None:
    other = UUID("0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5c")
    positions = [
        {"statement_id": UUID(_ID), "revision": 1, "change_seq": 7},
        {"statement_id": other, "revision": 2, "change_seq": 8},
    ]
    with (
        patch(
            "receipt_index.statement_repository.feed_positions",
            return_value=positions,
        ),
        patch(
            "receipt_index.statement_repository.republish_each",
            side_effect=RepublishStoppedError(other, [21]),
        ),
        patch("receipt_index.db.get_connection"),
    ):
        result = _invoke(config_file, "republish", "--all", "--yes")
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert (
        f"Error: statement {other} no longer exists; 1 of 2 statements were "
        "republished before it, and stay republished. Run the command again "
        "to finish."
    ) in result.stderr
    assert result.stdout == ""


def test_republish_names_a_database_error_by_class_only(
    config_file: Path, snapshot: MagicMock
) -> None:
    import psycopg

    positions = [{"statement_id": UUID(_ID), "revision": 1, "change_seq": 7}]
    with (
        patch(
            "receipt_index.statement_repository.feed_positions",
            return_value=positions,
        ),
        patch(
            "receipt_index.statement_repository.republish_each",
            side_effect=psycopg.errors.CheckViolation(_PLANTED),
        ),
        patch("receipt_index.db.get_connection"),
    ):
        result = _invoke(config_file, "republish", _ID, "--yes")
    assert result.exit_code == 1
    assert "the database refused the republish (CheckViolation)" in result.stderr
    assert _PLANTED not in result.output
