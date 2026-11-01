"""The CLI for duplicates (evidence-store design, §8).

``link``, ``unlink``, ``duplicates`` (list, link, dismiss, scan), and
``search --include-merged``. The database work is in duplicates.py, tested
against Postgres in tests/integration/test_duplicates.py; these tests check
what the commands pass, print and refuse.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

from receipt_index.duplicates import (
    Candidate,
    DuplicateError,
    MergeBlockedError,
    ScanResult,
    UnlinkResult,
)
from receipt_index.fetching import Fetched
from receipt_index.models import Provenance, Receipt

from .test_cli_order_payment import _ROW, _invoke, config_file  # noqa: F401

if TYPE_CHECKING:
    from pathlib import Path

REAL_DUPLICATES = True  # the CLI calls duplicates.py, mocked per test

_RECEIPT = Receipt.model_validate(_ROW)
_PROV = UUID("019572a0-0000-7000-8000-0000000000f1")
_OTHER = UUID("019572a0-0000-7000-8000-0000000000f2")


def _provenance(**kw: Any) -> Provenance:
    values: dict[str, Any] = {
        "id": _PROV,
        "receipt_id": _RECEIPT.id,
        "is_primary": False,
        "source_type": "gdrive",
        "source_name": "drive",
        "source_id": "drive-file-1",
        "received_at": datetime(2026, 2, 1, tzinfo=UTC),
        "original_status": "pending",
        "match_method": "document_hash",
        "created_at": datetime(2026, 2, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 2, 1, tzinfo=UTC),
    }
    values.update(kw)
    return Provenance.model_validate(values)


class TestLink:
    @patch("receipt_index.duplicates.link", return_value=_OTHER)
    @patch("receipt_index.db.get_connection")
    def test_links_as_a_person_with_the_reason(
        self,
        mock_conn: MagicMock,
        mock_link: MagicMock,
        config_file: Path,  # noqa: F811
    ) -> None:
        result = _invoke(
            config_file, "link", str(_PROV), str(_OTHER), "--reason", "same scan"
        )
        assert result.exit_code == 0, result.output
        actor = mock_link.call_args.kwargs["actor"]
        assert (actor.name, actor.kind, actor.reason) == (
            "operator",
            "person",
            "same scan",
        )
        assert f"Linked {_PROV} to receipt {_OTHER}" in result.output

    def test_a_reason_is_required(self, config_file: Path) -> None:  # noqa: F811
        result = _invoke(config_file, "link", str(_PROV), str(_OTHER))
        assert result.exit_code == 2
        assert "--reason" in result.output

    def test_a_blank_reason_is_refused(self, config_file: Path) -> None:  # noqa: F811
        result = _invoke(config_file, "link", str(_PROV), str(_OTHER), "--reason", "  ")
        assert result.exit_code == 2

    @patch(
        "receipt_index.duplicates.link",
        side_effect=MergeBlockedError("receipt x has hand-edited fields (vendor)"),
    )
    @patch("receipt_index.db.get_connection")
    def test_a_blocked_merge_says_why(
        self,
        mock_conn: MagicMock,
        mock_link: MagicMock,
        config_file: Path,  # noqa: F811
    ) -> None:
        result = _invoke(config_file, "link", str(_PROV), str(_OTHER), "--reason", "r")
        assert result.exit_code == 1
        assert "hand-edited fields" in result.output


class TestUnlink:
    @patch(
        "receipt_index.duplicates.unlink_home",
        return_value=UnlinkResult(_OTHER, restored=True),
    )
    @patch("receipt_index.db.get_connection")
    def test_restores_the_home_receipt(
        self,
        mock_conn: MagicMock,
        mock_unlink: MagicMock,
        config_file: Path,  # noqa: F811
    ) -> None:
        result = _invoke(config_file, "unlink", str(_PROV), "--reason", "two buys")
        assert result.exit_code == 0, result.output
        assert f"to receipt {_OTHER} (restored)" in result.output

    @patch(
        "receipt_index.duplicates.unlink_home",
        side_effect=DuplicateError("provenance p is on its own receipt r"),
    )
    @patch("receipt_index.db.get_connection")
    def test_a_refusal_exits_one(
        self,
        mock_conn: MagicMock,
        mock_unlink: MagicMock,
        config_file: Path,  # noqa: F811
    ) -> None:
        result = _invoke(config_file, "unlink", str(_PROV), "--reason", "r")
        assert result.exit_code == 1
        assert "own receipt" in result.output

    @patch("receipt_index.pipeline.unlink_to_own_receipt")
    @patch("receipt_index.fetching.fetch_item", return_value=Fetched(None))
    @patch("receipt_index.duplicates.unlink_home", return_value=None)
    @patch("receipt_index.db.get_connection")
    def test_an_item_its_source_lost_changes_nothing(
        self,
        mock_conn: MagicMock,
        mock_unlink: MagicMock,
        mock_fetch: MagicMock,
        mock_own: MagicMock,
        config_file: Path,  # noqa: F811
    ) -> None:
        conn = mock_conn.return_value.__enter__.return_value
        conn.execute.return_value.fetchone.return_value = _provenance().model_dump()
        result = _invoke(config_file, "unlink", str(_PROV), "--reason", "r")
        assert result.exit_code == 1
        assert "can't be fetched" in result.output
        assert "nothing changed" in result.output
        mock_own.assert_not_called()

    @patch("receipt_index.pipeline.unlink_to_own_receipt", return_value=_RECEIPT)
    @patch("receipt_index.fetching.fetch_item")
    @patch("receipt_index.duplicates.unlink_home", return_value=None)
    @patch("receipt_index.db.get_connection")
    def test_an_item_that_joined_at_ingest_gets_its_own_receipt(
        self,
        mock_conn: MagicMock,
        mock_unlink: MagicMock,
        mock_fetch: MagicMock,
        mock_own: MagicMock,
        config_file: Path,  # noqa: F811
    ) -> None:
        conn = mock_conn.return_value.__enter__.return_value
        conn.execute.return_value.fetchone.return_value = _provenance().model_dump()
        raw = MagicMock(source_type="gdrive")
        mock_fetch.return_value = Fetched(raw, "drive")
        with patch("receipt_index.extraction.create_extraction_agent"):
            result = _invoke(config_file, "unlink", str(_PROV), "--reason", "r")
        assert result.exit_code == 0, result.output
        assert mock_fetch.call_args.kwargs == {
            "source_id": "drive-file-1",
            "source_name": "drive",
            "source_type": "gdrive",
        }
        assert mock_own.call_args.args[2] is raw
        assert f"to new receipt {_RECEIPT.id}" in result.output


def _candidate(**kw: Any) -> Candidate:
    values: dict[str, Any] = {
        "id": uuid4(),
        "provenance_id": _PROV,
        "candidate_of_provenance_id": _OTHER,
        "reason": "metadata",
        "status": "open",
        "decided_by": None,
        "decided_at": None,
        "created_at": datetime(2026, 2, 1, tzinfo=UTC),
        "receipt": _RECEIPT,
        "candidate_of_receipt": _RECEIPT,
    }
    values.update(kw)
    return Candidate(**values)


class TestDuplicates:
    @patch("receipt_index.duplicates.list_candidates")
    @patch("receipt_index.db.get_connection")
    def test_lists_open_pairs_by_default(
        self,
        mock_conn: MagicMock,
        mock_list: MagicMock,
        config_file: Path,  # noqa: F811
    ) -> None:
        pair = _candidate()
        mock_list.return_value = [pair]
        result = _invoke(config_file, "duplicates")
        assert result.exit_code == 0, result.output
        assert mock_list.call_args.kwargs["status"] == "open"
        assert str(pair.id) in result.output
        assert "1 pair(s)." in result.output

    @patch("receipt_index.duplicates.list_candidates", return_value=[])
    @patch("receipt_index.db.get_connection")
    def test_all_lists_every_status(
        self,
        mock_conn: MagicMock,
        mock_list: MagicMock,
        config_file: Path,  # noqa: F811
    ) -> None:
        result = _invoke(config_file, "duplicates", "--status", "all")
        assert result.exit_code == 0, result.output
        assert mock_list.call_args.kwargs["status"] is None
        assert "No duplicate pairs." in result.output

    @patch("receipt_index.duplicates.list_candidates")
    @patch("receipt_index.db.get_connection")
    def test_json_gives_both_receipts(
        self,
        mock_conn: MagicMock,
        mock_list: MagicMock,
        config_file: Path,  # noqa: F811
    ) -> None:
        mock_list.return_value = [_candidate()]
        result = _invoke(config_file, "duplicates", "--output", "json")
        (item,) = json.loads(result.output)
        assert item["reason"] == "metadata"
        assert item["receipt"]["id"] == str(_RECEIPT.id)
        assert item["candidate_of_receipt"]["id"] == str(_RECEIPT.id)

    @patch("receipt_index.duplicates.decide_candidate", return_value=None)
    @patch("receipt_index.db.get_connection")
    def test_dismiss(
        self,
        mock_conn: MagicMock,
        mock_decide: MagicMock,
        config_file: Path,  # noqa: F811
    ) -> None:
        pair = uuid4()
        result = _invoke(config_file, "duplicates", "dismiss", str(pair))
        assert result.exit_code == 0, result.output
        assert mock_decide.call_args.args[1:] == (pair, "dismissed")
        assert f"Dismissed pair {pair}" in result.output

    @patch("receipt_index.duplicates.decide_candidate", return_value=_OTHER)
    @patch("receipt_index.db.get_connection")
    def test_link(
        self,
        mock_conn: MagicMock,
        mock_decide: MagicMock,
        config_file: Path,  # noqa: F811
    ) -> None:
        pair = uuid4()
        result = _invoke(
            config_file, "duplicates", "link", str(pair), "--as", "agent-x"
        )
        assert result.exit_code == 0, result.output
        assert mock_decide.call_args.kwargs["actor"].name == "agent-x"
        assert f"into receipt {_OTHER}" in result.output


class TestScan:
    @patch("receipt_index.duplicates.scan")
    @patch("receipt_index.db.get_connection")
    def test_dry_run_rolls_back(
        self,
        mock_conn: MagicMock,
        mock_scan: MagicMock,
        config_file: Path,  # noqa: F811
    ) -> None:
        mock_scan.return_value = ScanResult(
            linked={"order_number": 1},
            flagged={"metadata": 2},
            links=[(_PROV, _OTHER, "order_number")],
        )
        result = _invoke(config_file, "duplicates", "scan", "--dry-run")
        assert result.exit_code == 0, result.output
        conn = mock_conn.return_value.__enter__.return_value
        conn.rollback.assert_called_once()
        conn.commit.assert_not_called()
        assert f"Would link {_PROV} into {_OTHER} (order_number)" in result.output
        assert "Dry run: nothing written." in result.output
        assert "Flagged: metadata 2." in result.output

    @patch("receipt_index.duplicates.scan", return_value=ScanResult())
    @patch("receipt_index.db.get_connection")
    def test_a_scan_commits_with_the_window(
        self,
        mock_conn: MagicMock,
        mock_scan: MagicMock,
        config_file: Path,  # noqa: F811
    ) -> None:
        result = _invoke(config_file, "duplicates", "scan", "--window-days", "7")
        assert result.exit_code == 0, result.output
        assert mock_scan.call_args.kwargs == {"window_days": 7}
        mock_conn.return_value.__enter__.return_value.commit.assert_called_once()
        assert "Linked: none. Flagged: none." in result.output


class TestSearchIncludeMerged:
    @patch("receipt_index.repository.search_receipts", return_value=[])
    @patch("receipt_index.db.get_connection")
    def test_off_by_default(
        self,
        mock_conn: MagicMock,
        mock_search: MagicMock,
        config_file: Path,  # noqa: F811
    ) -> None:
        assert _invoke(config_file, "search").exit_code == 0
        assert mock_search.call_args.kwargs["include_merged"] is False

    @patch("receipt_index.repository.search_receipts", return_value=[])
    @patch("receipt_index.db.get_connection")
    def test_the_flag_passes_through(
        self,
        mock_conn: MagicMock,
        mock_search: MagicMock,
        config_file: Path,  # noqa: F811
    ) -> None:
        assert _invoke(config_file, "search", "--include-merged").exit_code == 0
        assert mock_search.call_args.kwargs["include_merged"] is True


class TestSearchMarksMerged:
    @patch("receipt_index.repository.search_receipts")
    @patch("receipt_index.db.get_connection")
    def test_merged_and_suspected_rows_are_marked(
        self,
        mock_conn: MagicMock,
        mock_search: MagicMock,
        config_file: Path,  # noqa: F811
    ) -> None:
        merged = _RECEIPT.model_copy(
            update={"merged_into_id": _OTHER, "merged_at": datetime.now(UTC)}
        )
        suspect = _RECEIPT.model_copy(
            update={"id": uuid4(), "suspected_duplicate_of": [_OTHER]}
        )
        mock_search.return_value = [merged, suspect]
        with patch(
            "receipt_index.repository.with_provenance", side_effect=lambda c, rs: rs
        ):
            result = _invoke(config_file, "search", "--include-merged")
        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        assert any(str(merged.id) in ln and "merged" in ln for ln in lines)
        assert any(str(suspect.id) in ln and "dup?" in ln for ln in lines)
