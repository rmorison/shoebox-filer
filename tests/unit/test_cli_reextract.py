"""The ``reextract`` command, ``search --needs-review`` and hand-edited fields."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from click.testing import CliRunner

from receipt_index.cli import cli
from receipt_index.extraction import EXTRACTION_PROMPT_VERSION
from receipt_index.models import Receipt
from receipt_index.reextract import ReceiptOutcome, ReextractResult
from tests.unit.test_cli import _MINIMAL_CONFIG_YAML, _RECEIPT_ROW, _make_config

if TYPE_CHECKING:
    from pathlib import Path

    from receipt_index.config import AppConfig

_RECEIPT = Receipt.model_validate(_RECEIPT_ROW)
_MODEL = "claude-haiku-4-5-20251001"


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "receipt-index.yaml"
    path.write_text(_MINIMAL_CONFIG_YAML)
    return path


@pytest.fixture
def env(config_file: Path) -> Any:
    """Patch the config, the database and the run; yield the mocks."""
    config = _make_config()
    with (
        patch("receipt_index.cli.load_config", return_value=config),
        patch("receipt_index.db.get_connection") as conn,
        patch(
            "receipt_index.repository.select_for_reextract", return_value=[_RECEIPT]
        ) as select,
        patch("receipt_index.repository.token_averages", return_value=None) as avg,
        patch("receipt_index.reextract.run_reextract") as run,
        patch("receipt_index.extraction.create_extraction_agent") as agent,
        patch("receipt_index.store.open_store"),
    ):
        run.return_value = ReextractResult(
            outcomes=[
                ReceiptOutcome(
                    receipt_id=_RECEIPT.id,
                    source_id=_RECEIPT.source_id,
                    vendor=_RECEIPT.vendor,
                    receipt_date=_RECEIPT.receipt_date,
                    status="applied",
                    input_kind="source",
                    changes={"paid_amount": (None, Decimal("40.00"))},
                    extracted_paid_amount=Decimal("40.00"),
                    paid_amount_confidence=0.85,
                )
            ]
        )
        yield {
            "config": config,
            "conn": conn,
            "select": select,
            "averages": avg,
            "run": run,
            "agent": agent,
            "file": config_file,
        }


def _invoke(env: dict[str, Any], *args: str) -> Any:
    return CliRunner().invoke(cli, ["--config", str(env["file"]), "reextract", *args])


class TestSelection:
    def test_requires_a_selector(self, env: dict[str, Any]) -> None:
        result = _invoke(env)
        assert result.exit_code == 2
        assert "--all" in result.output
        env["select"].assert_not_called()

    def test_all_selects_everything_stale(self, env: dict[str, Any]) -> None:
        result = _invoke(env, "--all")
        assert result.exit_code == 0, result.output
        _, kw = env["select"].call_args
        assert kw["prompt_version"] == EXTRACTION_PROMPT_VERSION
        assert kw["llm_model"] == _MODEL
        assert kw["force"] is False
        assert kw["ids"] == []

    def test_filters_are_passed_through(self, env: dict[str, Any]) -> None:
        rid = "019572a0-0000-7000-8000-000000000001"
        result = _invoke(
            env,
            "--date-from", "2026-01-01",
            "--date-to", "2026-03-31",
            "--source", "personal-email",
            "--id", rid,
            "--model-not", "claude-haiku-4-5-20251001",
            "--confidence-below", "paid_amount=0.6",
            "--force",
            "--limit", "30",
        )  # fmt: skip
        assert result.exit_code == 0, result.output
        _, kw = env["select"].call_args
        assert kw["date_from"] == date(2026, 1, 1)
        assert kw["date_to"] == date(2026, 3, 31)
        assert kw["source_name"] == "personal-email"
        assert kw["ids"] == [UUID(rid)]
        assert kw["model_not"] == "claude-haiku-4-5-20251001"
        assert kw["confidence_below"] == ("paid_amount", 0.6)
        assert kw["force"] is True
        assert kw["limit"] == 30

    def test_overall_confidence_below(self, env: dict[str, Any]) -> None:
        assert _invoke(env, "--confidence-below", "0.7").exit_code == 0
        assert env["select"].call_args[1]["confidence_below"] == (None, 0.7)

    @pytest.mark.parametrize("value", ["total=0.5", "paid_amount=x", "1.5"])
    def test_bad_confidence_below(self, env: dict[str, Any], value: str) -> None:
        result = _invoke(env, "--confidence-below", value)
        assert result.exit_code == 2
        env["select"].assert_not_called()


class TestDryRun:
    def test_writes_nothing_and_calls_no_llm(self, env: dict[str, Any]) -> None:
        result = _invoke(env, "--all", "--dry-run")
        assert result.exit_code == 0, result.output
        env["run"].assert_not_called()
        env["agent"].assert_not_called()
        assert "source, else rendition" in result.output
        assert "no measured token figures" in result.output

    def test_estimates_cost_from_measured_tokens(self, env: dict[str, Any]) -> None:
        config: AppConfig = env["config"]
        config.llm.price_per_mtok_input = Decimal("1")
        config.llm.price_per_mtok_output = Decimal("5")
        env["averages"].return_value = {
            "runs": 40,
            "avg_input": Decimal("2700"),
            "avg_output": Decimal("200"),
            "basis": "ingest_log",
        }
        result = _invoke(env, "--all", "--dry-run", "--output", "json")
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        estimate = data["estimate"]
        assert estimate["basis"] == "ingest_log"
        assert (estimate["input_tokens"], estimate["output_tokens"]) == (2700, 200)
        assert estimate["cost"] == "0.0037"  # 2,700 x $1 + 200 x $5 per million
        assert data["receipts"][0]["input"] == "source, else rendition"

    def test_rendition_input_is_planned_as_rendition(self, env: dict[str, Any]) -> None:
        result = _invoke(env, "--all", "--dry-run", "--input", "rendition")
        assert "[rendition]" in result.output


class TestRun:
    def test_reports_changes_and_the_extracted_paid_amount(
        self, env: dict[str, Any]
    ) -> None:
        result = _invoke(env, "--all", "--output", "json")
        assert result.exit_code == 0, result.output
        (row,) = json.loads(result.output)["receipts"]
        assert row["changes"] == {"paid_amount": {"old": None, "new": "40.00"}}
        assert row["extracted_paid_amount"] == "40.00"
        assert row["paid_amount_confidence"] == 0.85

    def test_rendition_input_passes_no_source_pool(self, env: dict[str, Any]) -> None:
        _invoke(env, "--all", "--input", "rendition")
        assert env["run"].call_args[1]["pool"] is None

    def test_auto_input_passes_the_source_pool(self, env: dict[str, Any]) -> None:
        _invoke(env, "--all")
        assert env["run"].call_args[1]["pool"] is not None

    def test_overwrite_and_model_are_passed(self, env: dict[str, Any]) -> None:
        _invoke(env, "--all", "--overwrite")
        kw = env["run"].call_args[1]
        assert kw["overwrite"] is True
        assert kw["llm_model"] == _MODEL

    @pytest.mark.parametrize("status", ["failed", "gated", "unavailable", "locked"])
    def test_exits_1_when_anything_needs_attention(
        self, env: dict[str, Any], status: str
    ) -> None:
        env["run"].return_value.outcomes[0].status = status
        assert _invoke(env, "--all").exit_code == 1

    def test_exits_1_when_the_run_stopped(self, env: dict[str, Any]) -> None:
        env["run"].return_value.stopped = "stopped after 5 failures in a row"
        assert _invoke(env, "--all").exit_code == 1


class TestNeedsReview:
    def test_search_passes_needs_review(self, config_file: Path) -> None:
        with (
            patch("receipt_index.cli.load_config", return_value=_make_config()),
            patch("receipt_index.db.get_connection"),
            patch(
                "receipt_index.repository.search_receipts", return_value=[_RECEIPT]
            ) as search,
        ):
            result = CliRunner().invoke(
                cli, ["--config", str(config_file), "search", "--needs-review"]
            )
        assert result.exit_code == 0, result.output
        assert search.call_args[1]["needs_review"] is True


class TestShowHandEdited:
    def test_lists_hand_edited_fields(self, config_file: Path) -> None:
        receipt = _RECEIPT.model_copy(update={"manual_fields": ["paid_amount"]})
        conn = MagicMock()
        with (
            patch("receipt_index.cli.load_config", return_value=_make_config()),
            patch("receipt_index.db.get_connection", return_value=conn),
            patch("receipt_index.repository.get_receipt_by_id", return_value=receipt),
        ):
            result = CliRunner().invoke(
                cli, ["--config", str(config_file), "show", str(receipt.id)]
            )
        assert result.exit_code == 0, result.output
        assert "Hand-edited:  paid_amount" in result.output
