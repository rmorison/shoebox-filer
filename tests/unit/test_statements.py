"""Unit tests for the statement pipeline's error handling (plan U5)."""

from __future__ import annotations

import psycopg
import pytest

from receipt_index.statement_extraction import CallUsage, StatementCallError
from receipt_index.statements import (
    StatementItemError,
    _error_class,
    _safe_message,
    masked_file_name,
)


def _call_error(kind: str, error_class: str) -> StatementCallError:
    return StatementCallError("account call", kind, error_class, CallUsage())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kind", "error_class", "expected"),
    [
        # A request timeout counts as an attempt, whatever classify said.
        ("timeout", "transient", "content"),
        ("provider", "transient", "transient"),
        ("provider", "config", "config"),
        ("schema", "content", "content"),
        ("refused", "content", "content"),
    ],
)
def test_a_call_error_is_retried_by_its_kind_and_class(
    kind: str, error_class: str, expected: str
) -> None:
    assert _error_class(_call_error(kind, error_class)) == expected


def test_the_pipelines_own_errors_are_item_errors() -> None:
    assert _error_class(StatementItemError("the read stopped: timeout")) == "content"


def test_a_database_error_is_recorded_without_its_detail() -> None:
    error = psycopg.errors.CheckViolation(
        "new row violates check constraint\n"
        "DETAIL: Failing row contains (5274910386254173)"
    )
    message = _safe_message(error)
    assert "5274910386254173" not in message
    assert message.startswith("CheckViolation")


def test_an_unknown_error_is_recorded_by_its_type_alone() -> None:
    assert _safe_message(KeyError("Bank 5274910386254173")) == "KeyError"


def test_a_call_error_keeps_its_own_message() -> None:
    assert _safe_message(_call_error("schema", "content")) == (
        "statement account call failed: schema (content)"
    )


@pytest.mark.parametrize(
    ("name", "masked"),
    [
        ("Statement 4111 1111 1111 1234.pdf", "Statement 1234.pdf"),
        ("estatement_2027-01.pdf", "estatement_2027-01.pdf"),
        (None, None),
    ],
)
def test_a_file_name_is_masked(name: str | None, masked: str | None) -> None:
    assert masked_file_name(name) == masked


# ---------------------------------------------------------------------------
# Ingest types: statement items are never offered to a receipt source
# ---------------------------------------------------------------------------


def _source(name: str, kind: str, documents: str | None = None) -> object:
    from types import SimpleNamespace

    fields = {"name": name, "type": kind}
    if documents is not None:
        fields["documents"] = documents
    return SimpleNamespace(**fields)


def test_a_statement_drive_source_logs_its_own_ingest_type() -> None:
    from receipt_index.fetching import STATEMENT_INGEST_TYPE, ingest_type

    statements = _source("statements-drive", "gdrive", "statements")
    receipts = _source("receipts-drive", "gdrive")
    mail = _source("mail", "imap")
    assert ingest_type(statements) == STATEMENT_INGEST_TYPE  # type: ignore[arg-type]
    assert ingest_type(receipts) == "gdrive"  # type: ignore[arg-type]
    assert ingest_type(mail) == "imap"  # type: ignore[arg-type]


def test_an_item_whose_source_is_gone_is_looked_for_in_sources_of_its_type() -> None:
    from contextlib import ExitStack

    from receipt_index.fetching import STATEMENT_INGEST_TYPE, SourcePool

    sources = [
        _source("receipts-drive", "gdrive"),
        _source("statements-drive", "gdrive", "statements"),
        _source("other-statements", "gdrive", "statements"),
    ]
    with ExitStack() as stack:
        pool = SourcePool(sources, lambda _: None, stack)  # type: ignore[arg-type]
        assert pool.candidates("removed", STATEMENT_INGEST_TYPE) == [
            "statements-drive",
            "other-statements",
        ]
        assert pool.candidates("removed", "gdrive") == ["receipts-drive"]
        # Its own source, while configured, whatever the type.
        assert pool.candidates("statements-drive", STATEMENT_INGEST_TYPE) == [
            "statements-drive"
        ]


def test_warm_mask_builds_the_patterns() -> None:
    from receipt_index import statement_extraction as se

    se._masked_run.cache_clear()
    se._dashes.cache_clear()
    se.warm_mask()
    assert se._masked_run.cache_info().currsize == 1
    assert se._dashes.cache_info().currsize == 1
