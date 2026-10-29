"""The ``retry`` command and ``failures --status/--since/--source``."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from click.testing import CliRunner

from receipt_index.cli import cli
from receipt_index.models import IngestLogEntry
from receipt_index.requeue import OrphanReport, RequeueOutcome
from tests.unit.test_cli import _MINIMAL_CONFIG_YAML, _make_config

if TYPE_CHECKING:
    from pathlib import Path

_REQUEST = UUID("019572a0-0000-7000-8000-0000000000ff")


def _entry(source_id: str, **kw: Any) -> IngestLogEntry:
    return IngestLogEntry.model_validate(
        {
            "id": uuid4(),
            "source_id": source_id,
            "source_type": "imap",
            "source_name": "test-email",
            "status": "failed",
            "receipt_id": None,
            "vendor": None,
            "amount": None,
            "email_subject": f"Receipt {source_id}",
            "email_sender": "shop@example.com",
            "email_date": None,
            "error_message": "Overloaded",
            "error_class": "transient",
            "created_at": datetime(2026, 10, 26, 9, 0, tzinfo=UTC),
            **kw,
        }
    )


@pytest.fixture
def env(tmp_path: Path) -> Any:
    path = tmp_path / "receipt-index.yaml"
    path.write_text(_MINIMAL_CONFIG_YAML)
    candidates = [_entry("<a@example.com>"), _entry("<b@example.com>")]
    with (
        patch("receipt_index.cli.load_config", return_value=_make_config()),
        patch("receipt_index.db.get_connection"),
        patch("receipt_index.store.open_store"),
        patch(
            "receipt_index.repository.select_retry_candidates",
            return_value=candidates,
        ) as select,
        patch(
            "receipt_index.requeue.orphan_report", return_value=OrphanReport()
        ) as orphans,
        patch("receipt_index.requeue.apply_requeue") as apply,
    ):
        apply.return_value = RequeueOutcome(
            _REQUEST, requeued=[c.source_id for c in candidates]
        )
        yield {
            "file": path,
            "select": select,
            "orphans": orphans,
            "apply": apply,
            "candidates": candidates,
        }


def _invoke(env: dict[str, Any], *args: str) -> Any:
    return CliRunner().invoke(cli, ["--config", str(env["file"]), "retry", *args])


class TestGuards:
    def test_status_alone_is_refused(self, env: dict[str, Any]) -> None:
        result = _invoke(env)
        assert result.exit_code == 2
        assert "would requeue every failed item" in result.output
        env["select"].assert_not_called()

    def test_source_alone_is_refused(self, env: dict[str, Any]) -> None:
        result = _invoke(env, "--source", "test-email", "--status", "skipped")
        assert result.exit_code == 2
        assert "every skipped item of source test-email" in result.output

    def test_all_allows_it(self, env: dict[str, Any]) -> None:
        assert _invoke(env, "--all").exit_code == 0

    @pytest.mark.parametrize(
        "args",
        [
            ["<a@example.com>"],
            ["--source-id", "<a@example.com>"],
            ["--error-like", "overloaded"],
            ["--transient"],
            ["--class", "content"],
            ["--since", "2026-10-01"],
        ],
    )
    def test_any_narrowing_filter_is_enough(
        self, env: dict[str, Any], args: list[str]
    ) -> None:
        assert _invoke(env, *args).exit_code == 0

    def test_transient_conflicts_with_another_class(self, env: dict[str, Any]) -> None:
        result = _invoke(env, "--transient", "--class", "content")
        assert result.exit_code == 2


class TestDryRun:
    def test_default_is_a_dry_run(self, env: dict[str, Any]) -> None:
        result = _invoke(env, "--error-like", "overload")
        assert result.exit_code == 0
        env["apply"].assert_not_called()
        assert "Dry run: 2 item(s) would be requeued" in result.output
        assert "Receipt <a@example.com>" in result.output

    def test_filters_reach_the_query(self, env: dict[str, Any]) -> None:
        _invoke(
            env,
            "<a@example.com>",
            "--source-id",
            "<b@example.com>",
            "--source-id",
            "<a@example.com>",
            "--status",
            "skipped",
            "--error-like",
            "Overload",
            "--transient",
            "--since",
            "2026-10-01",
            "--source",
            "test-email",
        )
        spec = env["select"].call_args.args[1]
        assert spec.source_ids == ("<a@example.com>", "<b@example.com>")
        assert spec.status == "skipped"
        assert spec.error_like == "Overload"
        assert spec.error_class == "transient"
        assert spec.source_name == "test-email"
        assert spec.since is not None
        assert spec.since.tzinfo is not None

    def test_json(self, env: dict[str, Any]) -> None:
        result = _invoke(env, "--transient", "--output", "json")
        data = json.loads(result.output)
        assert data["dry_run"] is True
        assert data["matched"] == 2
        assert [i["source_id"] for i in data["items"]] == [
            "<a@example.com>",
            "<b@example.com>",
        ]
        assert "requeued" not in data

    def test_orphans_are_reported(self, env: dict[str, Any]) -> None:
        env["orphans"].return_value = OrphanReport(
            files={"<a@example.com>": ["local:renditions/2026/10/x.pdf"]},
            unrecorded=["<b@example.com>"],
        )
        result = _invoke(env, "--transient")
        assert (
            "Orphaned file for <a@example.com>: local:renditions/2026/10/x.pdf"
            in result.output
        )
        assert "1 item(s) failed before stored files were recorded" in result.output

    def test_nothing_matches(self, env: dict[str, Any]) -> None:
        env["select"].return_value = []
        result = _invoke(env, "--transient", "--yes")
        assert result.exit_code == 0
        assert "No items match." in result.output
        env["apply"].assert_not_called()


class TestApply:
    def test_yes_requeues_what_the_dry_run_listed(self, env: dict[str, Any]) -> None:
        result = _invoke(env, "--transient", "--yes")
        assert result.exit_code == 0
        assert env["apply"].call_args.args[2] == env["candidates"]
        assert f"Requeued 2 of 2 item(s) (request {_REQUEST})" in result.output

    def test_items_left_alone_exit_1(self, env: dict[str, Any]) -> None:
        env["apply"].return_value = RequeueOutcome(
            _REQUEST,
            requeued=["<a@example.com>"],
            not_requeued=[("<b@example.com>", "another process holds it")],
        )
        result = _invoke(env, "--transient", "--yes", "--output", "json")
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["requeued"] == ["<a@example.com>"]
        assert data["not_requeued"] == [
            {"source_id": "<b@example.com>", "reason": "another process holds it"}
        ]


class TestFailures:
    @pytest.fixture
    def failures_env(self, tmp_path: Path) -> Any:
        path = tmp_path / "receipt-index.yaml"
        path.write_text(_MINIMAL_CONFIG_YAML)
        with (
            patch("receipt_index.cli.load_config", return_value=_make_config()),
            patch("receipt_index.db.get_connection"),
            patch(
                "receipt_index.repository.get_ingest_failures",
                return_value=[
                    _entry("<a@example.com>", next_retry_at=None),
                    _entry(
                        "<b@example.com>",
                        next_retry_at=datetime(2026, 10, 27, 9, 0, tzinfo=UTC),
                    ),
                ],
            ) as get,
        ):
            yield {"file": path, "get": get}

    def _invoke(self, env: dict[str, Any], *args: str) -> Any:
        return CliRunner().invoke(
            cli, ["--config", str(env["file"]), "failures", *args]
        )

    def test_default_is_failed(self, failures_env: dict[str, Any]) -> None:
        result = self._invoke(failures_env)
        kw = failures_env["get"].call_args.kwargs
        assert (kw["status"], kw["since"], kw["source_name"]) == ("failed", None, None)
        assert "terminal" in result.output
        assert "2 failed ingest(s)." in result.output

    def test_filters(self, failures_env: dict[str, Any]) -> None:
        self._invoke(
            failures_env,
            "--status",
            "retrying",
            "--since",
            "2026-10-01",
            "--source",
            "test-email",
        )
        kw = failures_env["get"].call_args.kwargs
        assert kw["status"] == "retrying"
        assert kw["source_name"] == "test-email"
        assert kw["since"].date().isoformat() == "2026-10-01"

    def test_json_carries_the_retry_fields(self, failures_env: dict[str, Any]) -> None:
        result = self._invoke(failures_env, "--output", "json")
        data = json.loads(result.output)["failures"]
        assert data[0]["error_class"] == "transient"
        assert data[1]["next_retry_at"].startswith("2026-10-27")


def test_requeue_count_is_written_after_a_failed_write() -> None:
    """A write that fails mid-loop aborts the transaction; the count still lands."""
    from unittest.mock import MagicMock

    from receipt_index import requeue
    from receipt_index.repository import RetryFilter

    entries = [_entry("<a@example.com>"), _entry("<b@example.com>")]
    conn = MagicMock()
    calls: list[str] = []
    conn.rollback.side_effect = lambda: calls.append("rollback")
    with (
        patch.object(requeue, "insert_retry_request", return_value=_REQUEST),
        patch.object(
            requeue,
            "get_item_history",
            side_effect=lambda c, sid, limit: [
                e for e in entries if e.source_id == sid
            ],
        ),
        patch.object(requeue, "has_receipt", return_value=False),
        patch.object(
            requeue,
            "insert_ingest_log",
            side_effect=[None, RuntimeError("connection reset")],
        ),
        patch.object(
            requeue,
            "set_retry_request_count",
            side_effect=lambda c, rid, n: calls.append(f"count={n}"),
        ),
        pytest.raises(RuntimeError),
    ):
        requeue.apply_requeue(conn, RetryFilter(error_class="transient"), entries)
    assert calls[-2:] == ["rollback", "count=1"]


def test_a_failed_count_update_keeps_the_loop_error() -> None:
    from unittest.mock import MagicMock

    from receipt_index import requeue
    from receipt_index.repository import RetryFilter

    entries = [_entry("<a@example.com>")]
    with (
        patch.object(requeue, "insert_retry_request", return_value=_REQUEST),
        patch.object(requeue, "get_item_history", return_value=entries),
        patch.object(requeue, "has_receipt", return_value=False),
        patch.object(
            requeue, "insert_ingest_log", side_effect=RuntimeError("connection reset")
        ),
        patch.object(
            requeue, "set_retry_request_count", side_effect=OSError("db gone")
        ),
        pytest.raises(RuntimeError, match="connection reset"),
    ):
        requeue.apply_requeue(
            MagicMock(), RetryFilter(error_class="transient"), entries
        )


def test_a_failed_count_update_alone_still_raises() -> None:
    from unittest.mock import MagicMock

    from receipt_index import requeue
    from receipt_index.repository import RetryFilter

    with (
        patch.object(requeue, "insert_retry_request", return_value=_REQUEST),
        patch.object(
            requeue, "set_retry_request_count", side_effect=OSError("db gone")
        ),
        pytest.raises(OSError, match="db gone"),
    ):
        requeue.apply_requeue(MagicMock(), RetryFilter(error_class="transient"), [])


class TestOrphans:
    def _invoke(self, tmp_path: Path, *args: str, exists: set[str]) -> Any:
        from unittest.mock import MagicMock

        path = tmp_path / "receipt-index.yaml"
        path.write_text(_MINIMAL_CONFIG_YAML)
        store = MagicMock()
        store.exists.side_effect = lambda ref: ref in exists
        with (
            patch("receipt_index.cli.load_config", return_value=_make_config()),
            patch("receipt_index.db.get_connection"),
            patch("receipt_index.store.open_store", return_value=store),
            patch(
                "receipt_index.repository.orphaned_refs",
                return_value={"<a@x>": ["local:a.pdf"], "<b@x>": ["local:b.pdf"]},
            ) as found,
        ):
            result = CliRunner().invoke(
                cli, ["--config", str(path), "failures", "--orphans", *args]
            )
        assert found.call_args.args[1] is None  # every item, whatever its state
        return result

    def test_lists_files_that_still_exist(self, tmp_path: Path) -> None:
        result = self._invoke(tmp_path, exists={"local:a.pdf"})
        assert result.exit_code == 0
        assert "<a@x>  local:a.pdf" in result.output
        assert "local:b.pdf" not in result.output
        assert "1 orphaned file(s)" in result.output

    def test_json_reports_existence(self, tmp_path: Path) -> None:
        result = self._invoke(tmp_path, "--output", "json", exists={"local:a.pdf"})
        assert json.loads(result.output) == [
            {"source_id": "<a@x>", "stored_ref": "local:a.pdf", "exists": True},
            {"source_id": "<b@x>", "stored_ref": "local:b.pdf", "exists": False},
        ]

    def test_none(self, tmp_path: Path) -> None:
        assert "No orphaned files." in self._invoke(tmp_path, exists=set()).output

    @pytest.mark.parametrize(
        "args",
        [
            ["--source", "test-email"],
            ["--since", "2026-10-01"],
            ["--status", "skipped"],
        ],
    )
    def test_filters_are_refused(self, tmp_path: Path, args: list[str]) -> None:
        path = tmp_path / "receipt-index.yaml"
        path.write_text(_MINIMAL_CONFIG_YAML)
        with (
            patch("receipt_index.cli.load_config", return_value=_make_config()),
            patch("receipt_index.db.get_connection"),
            patch("receipt_index.store.open_store"),
            patch("receipt_index.repository.orphaned_refs", return_value={}) as found,
        ):
            result = CliRunner().invoke(
                cli, ["--config", str(path), "failures", "--orphans", *args]
            )
        assert result.exit_code == 2
        assert (
            "takes no --status, --since, --source, --limit or --offset" in result.output
        )
        found.assert_not_called()

    def test_a_store_error_is_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from unittest.mock import MagicMock

        from receipt_index.requeue import stored_file_exists

        store = MagicMock()
        store.exists.side_effect = PermissionError("store root")
        with caplog.at_level("WARNING"):
            assert stored_file_exists(store, "local:a.pdf") is False
        assert "local:a.pdf" in caplog.text


class TestLinkOnly:
    def test_failures_lists_link_only_items(self, tmp_path: Path) -> None:
        path = tmp_path / "receipt-index.yaml"
        path.write_text(_MINIMAL_CONFIG_YAML)
        entry = _entry(
            "<link@example.com>",
            status="skipped",
            error_class=None,
            error_message="Skipped: link-only receipt (receipts.example.com)",
            receipt_link="https://receipts.example.com/r/abc123",
        )
        with (
            patch("receipt_index.cli.load_config", return_value=_make_config()),
            patch("receipt_index.db.get_connection"),
            patch(
                "receipt_index.repository.get_ingest_failures", return_value=[entry]
            ) as get,
        ):
            text = CliRunner().invoke(
                cli, ["--config", str(path), "failures", "--status", "link-only"]
            )
            as_json = CliRunner().invoke(
                cli,
                [
                    "--config",
                    str(path),
                    "failures",
                    "--status",
                    "link-only",
                    "--output",
                    "json",
                ],
            )

        assert text.exit_code == 0, text.output
        assert get.call_args.kwargs["status"] == "link-only"
        # The text output names the host only: the path can carry a token.
        assert "receipts.example.com" in text.output
        assert "/r/abc123" not in text.output
        assert "1 link-only item(s)." in text.output
        data = json.loads(as_json.output)["failures"]
        assert data[0]["receipt_link"] == "https://receipts.example.com/r/abc123"

    def test_retry_link_only_is_status_wide(self, env: dict[str, Any]) -> None:
        # Like --status skipped: a filter on the status alone needs --all.
        refused = _invoke(env, "--status", "link-only")
        assert refused.exit_code == 2
        assert "would requeue every link-only item" in refused.output

        assert _invoke(env, "--status", "link-only", "--all").exit_code == 0
        assert env["select"].call_args.args[1].status == "link-only"
