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
        ("Statement 4111 1111 1111 1234.pdf", "Statement [number].pdf"),
        ("estatement_2027-01.pdf", "estatement_2027-01.pdf"),
        (None, None),
        # Joined by marks a description keeps apart: one run in a name. A
        # cut run keeps no digits: its last four are as often a date's tail.
        ("Statement_4111_1111_1111_1234.pdf", "Statement_[number].pdf"),
        ("4111#1111#1111#1234.pdf", "[number].pdf"),
        ("Stmt(4111)(1111)(1111)(1234).pdf", "Stmt([number]).pdf"),
        ("4111.1111.1111.1234.pdf", "[number].pdf"),
        ("Statement_12345678_2026-01-31.pdf", "Statement_[number].pdf"),
        # A name has no amounts to keep: a long date run is cut too.
        ("Statement 2027-01-31.pdf", "Statement [number].pdf"),
        ("Statement 01/2027.pdf", "Statement 01/2027.pdf"),
        # A word of five or more cased letters ends a run; a shorter one
        # between two groups joins them, so a short word's neighbors can be
        # over-cut (the name is display-only).
        ("Statement 2026-01 Checking.pdf", "Statement 2026-01 Checking.pdf"),
        ("4111 Statement 2027-01.pdf", "4111 Statement 2027-01.pdf"),
        ("4111x2222x3333x4444.pdf", "[number].pdf"),
        ("4111a2222b3333c4444.pdf", "[number].pdf"),
        ("4111-visa-1111-1111-1234.pdf", "[number].pdf"),
        ("acct 4111 2222 then 3333 4444.pdf", "acct [number].pdf"),
        ("41110000OOOO4444.pdf", "[number].pdf"),
        ("Acct 1234 Stmt 2027-01.pdf", "Acct [number].pdf"),
        # A modifier letter NFKC folds to a plain one is a short word.
        ("4111\u02b02222\u02b03333\u02b04444.pdf", "[number].pdf"),
        # Percent-encoded marks are decoded, to a fixed point, first.
        ("Statement%5F4111%5F2222%5F3333%5F4444.pdf", "Statement_[number].pdf"),
        ("4111%2D1111%2D1111%2D1234.pdf", "[number].pdf"),
        ("4111%2E1111%2E1111%2E1234.pdf", "[number].pdf"),
        ("4111%252D1111%252D1111%252D1234.pdf", "[number].pdf"),
        ("4111%252525252D2222%252525252D3333%252525252D4444.pdf", "[number].pdf"),
        ("Statement%2001%2F2027.pdf", "Statement 01/2027.pdf"),
        ("100% of 2027.pdf", "100% of 2027.pdf"),
        ("%ZZ4111%2G2222.pdf", "%ZZ[number].pdf"),
        # Letters that look like marks (tatweel, katakana long-vowel mark,
        # modifier apostrophe, dental click, a CJK character) join a run.
        ("4111\u06401111\u06401111\u06401234.pdf", "[number].pdf"),
        ("4111\u30fc1111\u30fc1111\u30fc1234.pdf", "[number].pdf"),
        ("4111\u02bc1111\u02bc1111\u02bc1234.pdf", "[number].pdf"),
        ("4111\u01c01111\u01c01111\u01c01234.pdf", "[number].pdf"),
        (
            "\u660e\u7d304111\u660e1111\u660e1111\u660e1234.pdf",
            "\u660e\u7d30[number].pdf",
        ),
        # Digits that aren't ASCII are normalized and count as digits.
        (
            "\u2074\u00b9\u00b9\u00b9 \u00b9\u00b9\u00b9\u00b9 "
            "\u00b9\u00b9\u00b9\u00b9 \u00b9\u00b2\u00b3\u2074.pdf",
            "[number].pdf",
        ),
        (
            "\u2463\u2460\u2460\u2460\u2460\u2460\u2460\u2460"
            "\u2460\u2460\u2460\u2460\u2460\u2461\u2462\u2463.pdf",
            "[number].pdf",
        ),
        (
            "\uff14\uff11\uff11\uff11-1111-\uff11\uff11\uff11\uff11-1234.pdf",
            "[number].pdf",
        ),
        ("4111\u2080\u2081\u2081\u2081.1111.1234.pdf", "[number].pdf"),
        # Nothing that can't be stored: a NUL is dropped (an encoded one
        # too), a lone surrogate drops the name, bad UTF-8 is replaced.
        ("a\x00b.pdf", "ab.pdf"),
        ("a%00b.pdf", "ab.pdf"),
        ("bad\ud800.pdf", None),
        ("bad%ED%A0%80.pdf", "bad\ufffd\ufffd\ufffd.pdf"),
        ("", None),
    ],
)
def test_a_file_name_is_masked(name: str | None, masked: str | None) -> None:
    assert masked_file_name(name) == masked


def test_no_masked_file_name_holds_eight_digits_in_a_row() -> None:
    # The database's CHECK on statement_sources.file_name refuses eight
    # digits in a row, which would fail the item on every attempt. Random
    # synthetic names of digit groups and marks never reach it.
    import random
    import re
    import unicodedata

    rng = random.Random(7319)
    marks = [
        *" _-.#()/,;:+=[]{}'~!@$%^&*",
        *"\u0640\u30fc\u02bc\u02c8\u01c0\u3164\u2800\u200b\u660e",
        *("x", "ab", "visa", "Stmt", "then", "OOOO", "\u02b0"),
        *("%2D", "%5F", "%2e", "%20", "%252D", "%00"),
        "",
        "  ",
        "\u2013",
        "\u00b7",
    ]
    digits = "0123456789\u00b9\u00b2\u2460\uff11\u0661"
    for _ in range(3000):
        parts = []
        for _ in range(rng.randint(1, 8)):
            parts.append("".join(rng.choice(digits) for _ in range(rng.randint(1, 6))))
            parts.append(rng.choice(marks))
        name = (
            rng.choice(["", "Stmt ", "x"]) + "".join(parts) + rng.choice([".pdf", ""])
        )
        masked = masked_file_name(name)
        assert masked is None or not re.search(r"[0-9]{8}", masked), name
        assert masked_file_name(masked) == masked, name  # idempotent
        # Nor eight digits, of any script, joined by anything but a word of
        # five or more letters.
        normalized = unicodedata.normalize("NFKC", masked or "")
        for run in re.split(r"[^\W\d_]{5,}", normalized):
            assert sum(char.isdecimal() for char in run) < 8, name


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
        pool = SourcePool(sources, lambda _: None, stack)  # type: ignore[arg-type, return-value]
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
