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
