"""PDF validation at ingest: strict parse, repair, or unparseable (design §9)."""

from __future__ import annotations

import hashlib
import io
import time
from unittest.mock import patch

import pikepdf
import pytest

from receipt_index.pdf_validate import PdfCheck, check_pdf
from tests.pdfs import (
    broken_header_pdf,
    garbage_pdf,
    make_pdf,
    truncated_pdf,
    wrong_startxref_pdf,
)


@pytest.mark.parametrize(
    "data",
    [make_pdf("A clean receipt"), make_pdf(), make_pdf("Two", pages=2)],
    ids=["text", "blank", "two-pages"],
)
def test_a_clean_pdf_is_ok(data: bytes) -> None:
    assert check_pdf(data) == PdfCheck(status="ok")


def test_a_pdf_with_only_an_owner_password_is_ok() -> None:
    assert check_pdf(make_pdf("Owner only", encrypted=True)).status == "ok"


@pytest.mark.parametrize(
    "damaged",
    [truncated_pdf, wrong_startxref_pdf, broken_header_pdf],
    ids=["truncated", "wrong-startxref", "broken-header"],
)
def test_a_damaged_pdf_that_qpdf_rewrites_is_repaired(damaged: object) -> None:
    data = damaged()  # type: ignore[operator]
    before = hashlib.sha256(data).digest()

    result = check_pdf(data)

    assert result.status == "repaired"
    assert result.repaired is not None
    assert result.repaired != data
    # The repair parses strictly in its turn, and the input is untouched.
    assert check_pdf(result.repaired) == PdfCheck(status="ok")
    assert hashlib.sha256(data).digest() == before


def test_the_same_damaged_bytes_always_give_the_same_repair() -> None:
    data = truncated_pdf("Twice")
    first = check_pdf(data).repaired
    # qpdf's default file ID is seeded from the clock: let it move on.
    time.sleep(1.1)
    assert check_pdf(data).repaired == first


def test_a_repaired_pdf_keeps_its_pages_and_text() -> None:
    result = check_pdf(truncated_pdf("Kept text"))
    assert result.repaired is not None
    with pikepdf.open(io.BytesIO(result.repaired)) as pdf:
        assert len(pdf.pages) == 1
        assert b"Kept text" in pdf.pages[0].Contents.read_bytes()


@pytest.mark.parametrize(
    "data", [garbage_pdf(), b"", b"%PDF-1.4\n"], ids=["garbage", "empty", "header-only"]
)
def test_bytes_that_cannot_be_repaired_are_unparseable(data: bytes) -> None:
    assert check_pdf(data) == PdfCheck(status="unparseable")


def test_a_pdf_that_needs_a_user_password_is_unparseable() -> None:
    with patch(
        "receipt_index.pdf_validate.pikepdf.open",
        side_effect=pikepdf.PasswordError("password needed"),
    ):
        assert check_pdf(make_pdf("Locked")) == PdfCheck(status="unparseable")


def test_a_repair_that_still_fails_strict_mode_is_unparseable() -> None:
    real_open = pikepdf.open
    calls: list[bool] = []

    def open_(*args: object, **kwargs: object) -> pikepdf.Pdf:
        strict = kwargs.get("attempt_recovery") is False
        calls.append(strict)
        if strict:
            raise pikepdf.PdfError("still damaged")
        return real_open(*args, **kwargs)  # type: ignore[arg-type]

    with patch("receipt_index.pdf_validate.pikepdf.open", side_effect=open_):
        assert check_pdf(truncated_pdf()) == PdfCheck(status="unparseable")
    # The strict open, the recovering open, and the strict check of the repair.
    assert calls == [True, False, True]
