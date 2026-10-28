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
        data = json.loads(result.output)
        assert data[0]["error_class"] == "transient"
        assert data[1]["next_retry_at"].startswith("2026-10-27")
