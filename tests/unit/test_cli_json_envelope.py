"""`search` and `failures` JSON: the MCP page envelope, and `failures` paging.

The drift tests tie the CLI's JSON to the MCP output models, so the two
can't move apart: the envelope keys, each item's keys, and (where the model
allows it) a round trip through the model.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from receipt_index.cli import cli
from receipt_index.mcp_server import (
    FailureOut,
    FailuresResult,
    ReceiptOut,
    SearchResult,
)

from .test_cli import (
    _MINIMAL_CONFIG_YAML,
    _SAMPLE_FAILURE,
    _SAMPLE_RECEIPT,
    _make_config,
)

if TYPE_CHECKING:
    from pathlib import Path

# The one field MCP receipts carry that the CLI's don't: the PDF's host path
# and whether it exists, which needs the store opened.
_MCP_ONLY_RECEIPT_FIELDS = {"file"}


def _json(config_file: Path, *args: str) -> Any:
    result = CliRunner().invoke(
        cli, ["--config", str(config_file), *args, "--output", "json"]
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


@pytest.fixture()
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "receipt-index.yaml"
    path.write_text(_MINIMAL_CONFIG_YAML)
    return path


@pytest.fixture(autouse=True)
def _config() -> Any:
    with patch("receipt_index.cli.load_config", return_value=_make_config()):
        yield


@pytest.fixture()
def search_page() -> Any:
    with (
        patch("receipt_index.db.get_connection"),
        patch(
            "receipt_index.repository.search_receipts",
            return_value=[_SAMPLE_RECEIPT] * 2,
        ) as search,
        patch("receipt_index.repository.count_receipts", return_value=7) as count,
    ):
        yield search, count


@pytest.fixture()
def failures_page() -> Any:
    with (
        patch("receipt_index.db.get_connection"),
        patch(
            "receipt_index.repository.get_ingest_failures",
            return_value=[_SAMPLE_FAILURE] * 2,
        ) as get,
        patch(
            "receipt_index.repository.count_ingest_failures", return_value=7
        ) as count,
    ):
        yield get, count


class TestDriftFromMcp:
    """The CLI envelope and items match the MCP models field for field."""

    @pytest.mark.usefixtures("search_page")
    @pytest.mark.parametrize("paging", [(), ("--limit", "2", "--offset", "2")])
    def test_search_keys_match_search_result(
        self, config_file: Path, paging: tuple[str, ...]
    ) -> None:
        data = _json(config_file, "search", *paging)
        assert set(data) == set(SearchResult.model_fields)
        for item in data["receipts"]:
            assert set(item) == set(ReceiptOut.model_fields) - _MCP_ONLY_RECEIPT_FIELDS

    @pytest.mark.usefixtures("search_page")
    def test_search_page_round_trips_through_search_result(
        self, config_file: Path
    ) -> None:
        data = _json(config_file, "search", "--limit", "2", "--offset", "2")
        placeholder = {"path": "/unused", "exists": False}
        with_file = {
            **data,
            "receipts": [{**r, "file": placeholder} for r in data["receipts"]],
        }
        dumped = SearchResult.model_validate(with_file).model_dump(mode="json")
        for r in dumped["receipts"]:
            del r["file"]
        assert dumped == data

    @pytest.mark.usefixtures("failures_page")
    @pytest.mark.parametrize("paging", [(), ("--limit", "2", "--offset", "2")])
    def test_failures_keys_match_failures_result(
        self, config_file: Path, paging: tuple[str, ...]
    ) -> None:
        data = _json(config_file, "failures", *paging)
        assert set(data) == set(FailuresResult.model_fields)
        for item in data["failures"]:
            assert set(item) == set(FailureOut.model_fields)

    @pytest.mark.usefixtures("failures_page")
    def test_failures_page_round_trips_through_failures_result(
        self, config_file: Path
    ) -> None:
        data = _json(config_file, "failures", "--limit", "2", "--offset", "2")
        dumped = FailuresResult.model_validate(data).model_dump(mode="json")
        assert dumped == data


class TestSearchEnvelope:
    def test_unpaged(self, config_file: Path, search_page: Any) -> None:
        search, count = search_page
        data = _json(config_file, "search", "--vendor", "amazon")
        assert len(data["receipts"]) == 2
        assert (data["total"], data["limit"], data["offset"]) == (7, None, 0)
        assert data["next_offset"] is None
        assert search.call_args.kwargs["limit"] is None
        assert count.call_args.kwargs["vendor"] == "amazon"

    @pytest.mark.parametrize(
        ("offset", "expected"),
        [("0", 2), ("3", 5), ("5", None)],
    )
    def test_next_offset(
        self,
        config_file: Path,
        search_page: Any,
        offset: str,
        expected: int | None,
    ) -> None:
        data = _json(config_file, "search", "--limit", "2", "--offset", offset)
        assert (data["limit"], data["offset"]) == (2, int(offset))
        assert data["next_offset"] == expected

    def test_past_the_end(self, config_file: Path, search_page: Any) -> None:
        search, _ = search_page
        search.return_value = []
        data = _json(config_file, "search", "--limit", "2", "--offset", "40")
        assert data["receipts"] == []
        assert (data["total"], data["next_offset"]) == (7, None)


class TestFailuresEnvelope:
    def test_unpaged(self, config_file: Path, failures_page: Any) -> None:
        get, count = failures_page
        data = _json(
            config_file, "failures", "--status", "retrying", "--source", "test-email"
        )
        assert len(data["failures"]) == 2
        assert (data["total"], data["limit"], data["offset"]) == (7, None, 0)
        assert data["next_offset"] is None
        # The total counts the same filters, unpaged.
        assert count.call_args.kwargs == {
            "status": "retrying",
            "since": None,
            "source_name": "test-email",
        }
        assert (get.call_args.kwargs["limit"], get.call_args.kwargs["offset"]) == (
            None,
            None,
        )

    def test_paged(self, config_file: Path, failures_page: Any) -> None:
        get, _ = failures_page
        data = _json(config_file, "failures", "--limit", "2", "--offset", "3")
        assert (get.call_args.kwargs["limit"], get.call_args.kwargs["offset"]) == (
            2,
            3,
        )
        assert (data["total"], data["limit"], data["offset"]) == (7, 2, 3)
        assert data["next_offset"] == 5


class TestFailuresPaging:
    def _text(self, config_file: Path, *args: str) -> str:
        result = CliRunner().invoke(
            cli, ["--config", str(config_file), "failures", *args]
        )
        assert result.exit_code == 0, result.output
        return result.output

    def test_unpaged_text_footer_unchanged(
        self, config_file: Path, failures_page: Any
    ) -> None:
        _, count = failures_page
        assert "\n2 failed ingest(s).\n" in self._text(config_file)
        count.assert_not_called()

    def test_paged_text_footer_places_the_page(
        self, config_file: Path, failures_page: Any
    ) -> None:
        get, count = failures_page
        out = self._text(config_file, "--limit", "2", "--offset", "2")
        assert "2 failed ingest(s) shown (3-4 of 7)." in out
        out = self._text(
            config_file, "--status", "queued", "--limit", "2", "--offset", "2"
        )
        assert "2 queued item(s) shown (3-4 of 7)." in out

        get.return_value = []
        out = self._text(config_file, "--offset", "40")
        assert "None on this page (7 match)." in out

        count.return_value = 0
        assert "No failed ingests." in self._text(config_file, "--limit", "2")

    @pytest.mark.parametrize(
        "args",
        [["--limit", "0"], ["--limit", "-1"], ["--offset", "-1"]],
    )
    def test_out_of_range_is_a_usage_error(
        self, config_file: Path, failures_page: Any, args: list[str]
    ) -> None:
        get, _ = failures_page
        result = CliRunner().invoke(
            cli, ["--config", str(config_file), "failures", *args]
        )
        assert result.exit_code == 2, result.output
        assert f"Invalid value for '{args[0]}'" in result.output
        get.assert_not_called()

    @pytest.mark.parametrize("args", [["--limit", "2"], ["--offset", "1"]])
    def test_orphans_refuses_paging(
        self, config_file: Path, failures_page: Any, args: list[str]
    ) -> None:
        result = CliRunner().invoke(
            cli, ["--config", str(config_file), "failures", "--orphans", *args]
        )
        assert result.exit_code == 2, result.output
        assert "--limit or --offset" in result.output

    def test_orphans_json_stays_a_list(self, config_file: Path) -> None:
        with (
            patch("receipt_index.db.get_connection"),
            patch("receipt_index.store.open_store"),
            patch(
                "receipt_index.repository.orphaned_refs",
                return_value={"<a@example.com>": ["2025/06/a.pdf"]},
            ),
            patch("receipt_index.requeue.stored_file_exists", return_value=True),
        ):
            data = _json(config_file, "failures", "--orphans")
        assert data == [
            {
                "source_id": "<a@example.com>",
                "stored_ref": "2025/06/a.pdf",
                "exists": True,
            }
        ]
