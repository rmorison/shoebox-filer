"""The ``backfill`` command: arguments, output and exit codes."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from receipt_index.backfill import BackfillResult, OriginalsResult
from receipt_index.cli import cli
from tests.unit.test_cli import _MINIMAL_CONFIG_YAML, _make_config

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def env(tmp_path: Path) -> Any:
    path = tmp_path / "receipt-index.yaml"
    path.write_text(_MINIMAL_CONFIG_YAML)
    config = _make_config()
    with (
        patch("receipt_index.cli.load_config", return_value=config),
        patch("receipt_index.db.get_connection"),
        patch("receipt_index.store.open_store"),
        patch("receipt_index.repository.token_averages", return_value=None) as avg,
        patch("receipt_index.backfill.run_backfill") as run,
        patch("receipt_index.backfill.run_originals_only") as originals,
        patch("receipt_index.extraction.create_extraction_agent") as agent,
    ):
        run.return_value = BackfillResult(listed=3, unseen=2, processed=2)
        originals.return_value = OriginalsResult(to_fetch=1, stored=1)
        yield {
            "config": config,
            "file": path,
            "run": run,
            "originals": originals,
            "agent": agent,
            "averages": avg,
        }


def _invoke(env: dict[str, Any], *args: str) -> Any:
    return CliRunner().invoke(cli, ["--config", str(env["file"]), "backfill", *args])


class TestArguments:
    def test_source_is_required(self, env: dict[str, Any]) -> None:
        result = _invoke(env)
        assert result.exit_code == 2
        assert "--source" in result.output

    def test_unknown_source(self, env: dict[str, Any]) -> None:
        result = _invoke(env, "--source", "nope")
        assert result.exit_code == 1
        assert "test-email" in result.output
        env["run"].assert_not_called()

    def test_until_before_since(self, env: dict[str, Any]) -> None:
        result = _invoke(
            env,
            "--source",
            "test-email",
            "--since",
            "2025-02-01",
            "--until",
            "2025-01-01",
        )
        assert result.exit_code == 2

    def test_archive_is_not_offered(self, env: dict[str, Any]) -> None:
        assert _invoke(env, "--source", "test-email", "--archive").exit_code == 2

    def test_passes_days_and_limit(self, env: dict[str, Any]) -> None:
        from datetime import date

        result = _invoke(
            env,
            "--source",
            "test-email",
            "--since",
            "2025-01-01",
            "--until",
            "2025-01-31",
            "--limit",
            "5",
        )
        assert result.exit_code == 0, result.output
        kw = env["run"].call_args.kwargs
        assert (kw["since"], kw["until"], kw["limit"]) == (
            date(2025, 1, 1),
            date(2025, 1, 31),
            5,
        )
        assert kw["source_type"] == "imap"
        assert "Processed: 2" in result.output


class TestFullMode:
    def test_dry_run_prints_counts_and_builds_no_agent(
        self, env: dict[str, Any]
    ) -> None:
        env["run"].return_value = BackfillResult(
            listed=5, unseen=4, originals_missing=1
        )
        result = _invoke(env, "--source", "test-email", "--dry-run")
        assert result.exit_code == 0, result.output
        assert "Unseen: 4" in result.output
        assert "Originals missing: 1" in result.output
        assert "no measured token figures" in result.output
        assert env["run"].call_args.kwargs["agent"] is None
        env["agent"].assert_not_called()

    def test_dry_run_estimates_the_cost(self, env: dict[str, Any]) -> None:
        env["config"].llm.price_per_mtok_input = Decimal(1)
        env["config"].llm.price_per_mtok_output = Decimal(5)
        env["averages"].return_value = {
            "runs": 20,
            "avg_input": 2000,
            "avg_output": 200,
            "basis": "ingest_log",
        }
        env["run"].return_value = BackfillResult(unseen=10)
        result = _invoke(env, "--source", "test-email", "--dry-run")
        # 10 x (2000 x $1 + 200 x $5) per million tokens.
        assert "Estimated cost: $0.0300" in result.output

    def test_failures_exit_1(self, env: dict[str, Any]) -> None:
        env["run"].return_value = BackfillResult(unseen=1, failed=1)
        assert _invoke(env, "--source", "test-email").exit_code == 1

    def test_source_error_exits_1(self, env: dict[str, Any]) -> None:
        env["run"].return_value = BackfillResult(source_error="listing failed")
        result = _invoke(env, "--source", "test-email")
        assert result.exit_code == 1
        assert "listing failed" in result.output


class TestOriginalsOnly:
    def test_runs_without_an_agent(self, env: dict[str, Any]) -> None:
        result = _invoke(env, "--source", "test-email", "--originals-only")
        assert result.exit_code == 0, result.output
        assert "Stored: 1" in result.output
        env["agent"].assert_not_called()
        env["run"].assert_not_called()

    def test_dry_run_reports_what_stays_pending(self, env: dict[str, Any]) -> None:
        env["originals"].return_value = OriginalsResult(
            to_fetch=3, unsearchable=1, drive_pending=2
        )
        result = _invoke(env, "--source", "test-email", "--originals-only", "--dry-run")
        assert result.exit_code == 0, result.output
        assert "Originals to fetch: 3" in result.output
        assert "1 with no Message-ID" in result.output
        assert "2 Drive originals" in result.output
        assert "No LLM calls" in result.output

    def test_source_down_exits_1(self, env: dict[str, Any]) -> None:
        env["originals"].return_value = OriginalsResult(source_error="unavailable")
        result = _invoke(env, "--source", "test-email", "--originals-only")
        assert result.exit_code == 1
