"""Tests for receipt_index.db.get_connection's session options."""

from __future__ import annotations

from unittest.mock import patch

from receipt_index.db import get_connection


def _options(url: str, **kwargs: object) -> str:
    with patch("receipt_index.db.psycopg.connect") as connect:
        get_connection(url, **kwargs)  # type: ignore[arg-type]
    return str(connect.call_args.kwargs.get("options", ""))


def test_options_in_the_url_are_kept_beside_the_statement_timeout() -> None:
    url = "postgresql://db/x?options=-c%20search_path%3Dreceipt"
    options = _options(url, statement_timeout_seconds=60)
    assert "-c search_path=receipt" in options
    assert "-c statement_timeout=60000" in options


def test_read_only_and_timeout_combine() -> None:
    options = _options("postgresql://db/x", read_only=True, statement_timeout_seconds=1)
    assert "default_transaction_read_only=on" in options
    assert "statement_timeout=1000" in options


def test_no_options_passes_none() -> None:
    with patch("receipt_index.db.psycopg.connect") as connect:
        get_connection("postgresql://db/x")
    assert "options" not in connect.call_args.kwargs
