"""``store backfill-hashes``: its output and exit code."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch
from uuid import UUID

from receipt_index.hash_backfill import BackfillReport

from .test_cli_order_payment import _invoke, config_file  # noqa: F401

if TYPE_CHECKING:
    from pathlib import Path

_MISSING = UUID("019572a0-0000-7000-8000-000000000009")


@patch("receipt_index.hash_backfill.backfill_rendition_hashes")
@patch("receipt_index.db.get_connection")
def test_reports_counts_and_exits_zero(
    mock_conn: MagicMock,
    mock_backfill: MagicMock,
    config_file: Path,  # noqa: F811
) -> None:
    mock_backfill.return_value = BackfillReport(hashed=3, shared=1)
    result = _invoke(config_file, "store", "backfill-hashes")
    assert result.exit_code == 0, result.output
    assert "Recorded 3 rendition(s); 1 share bytes" in result.output
    assert mock_backfill.call_args.kwargs == {"dry_run": False}


@patch("receipt_index.hash_backfill.backfill_rendition_hashes")
@patch("receipt_index.db.get_connection")
def test_a_missing_file_exits_one_and_names_the_receipt(
    mock_conn: MagicMock,
    mock_backfill: MagicMock,
    config_file: Path,  # noqa: F811
) -> None:
    mock_backfill.return_value = BackfillReport(
        hashed=1, missing=[(_MISSING, "local:2026/01/a.pdf: FileNotFoundError")]
    )
    result = _invoke(config_file, "store", "backfill-hashes")
    assert result.exit_code == 1
    assert f"Not recorded: {_MISSING}" in result.output
    assert "1 receipt(s) not recorded" in result.output


@patch("receipt_index.hash_backfill.backfill_rendition_hashes")
@patch("receipt_index.db.get_connection")
def test_dry_run_says_would(
    mock_conn: MagicMock,
    mock_backfill: MagicMock,
    config_file: Path,  # noqa: F811
) -> None:
    mock_backfill.return_value = BackfillReport(hashed=2)
    result = _invoke(config_file, "store", "backfill-hashes", "--dry-run")
    assert result.exit_code == 0, result.output
    assert result.output.startswith("Would record 2 rendition(s)")
    assert mock_backfill.call_args.kwargs == {"dry_run": True}
