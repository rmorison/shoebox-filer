"""The mask count script (scripts/mask_counts.py): counts, and nothing else.

Synthetic statements only. The script is an owner step: the operator runs
it on their own machine against real statements, so its output must hold
fixed labels and counts, never text, names or digits from the input.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.pdfs import make_pdf

if TYPE_CHECKING:
    from types import ModuleType

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "mask_counts.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mask_counts", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mask_counts = _load()


def _count(*lines: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    mask = mask_counts._Mask()
    for line in lines:
        mask_counts.count_field(mask, line, counts)
    return counts


# One synthetic description per shape the plan names, with the counts it
# must add. Each also checks itself against mask_digits (no mismatch).
_CASES: list[tuple[str, dict[str, int]]] = [
    (
        "AMZN 11/09 $45.99",
        {"set_aside.amount_with_cents": 1, "set_aside.short_date": 1, "cuts": 0},
    ),
    ("HOTEL 11/09-11/12", {"set_aside.short_date_range": 1, "cuts": 0}),
    ("TUITION 2025-2026", {"set_aside.year_range_alone": 1, "cuts": 0}),
    (
        "SEATTLE WA 98101-1234 11/09 45.99",
        {"set_aside.zip4_alone": 1, "set_aside.short_date": 1, "cuts": 0},
    ),
    ("ACCT 12345-6789 0123", {"neighbour.zip4_cut": 1, "cuts": 1}),
    ("4111 2025-2026 1234", {"neighbour.year_range_cut": 1, "cuts": 1}),
    (
        "CARD 4111-1111-1111-1234 $12.50",
        {"cuts": 1, "set_aside.amount_with_cents": 1},
    ),
    ("CALL 555-123-4567", {"effect.phone_shape_cut": 1}),
    (
        "REF 20261109",
        {
            "effect.date_without_separators_cut": 1,
            "effect.plain_number_8_plus_digits_cut": 1,
        },
    ),
    ("CONF 7318204417", {"effect.plain_number_8_plus_digits_cut": 1}),
    (
        "PERIOD 2026-11-09 4412",
        {"effect.dashed_date_cut": 1, "effect.adjacent_tokens_joined_cut": 1},
    ),
    ("POS 11/09-4111", {"effect.date_dash_joined_to_number_cut": 1}),
    (
        "DEBIT CARD PURCHASE 11/09 0412 1234",
        {
            "effect.short_tokens_after_a_date_cut": 1,
            "limit.date_shaped_group_before_a_cut": 1,
        },
    ),
    # A dotted date reads as an amount first; still date-shaped.
    (
        "DEBIT 11.09 0412 1234",
        {
            "effect.short_tokens_after_a_date_cut": 1,
            "set_aside.amount_with_cents": 1,
        },
    ),
    ("1,234,567,890 units", {"effect.bare_thousands_over_nine_digits_cut": 1}),
    ("FY 2026 2027", {"overcut.two_years_side_by_side": 1}),
    ("FY 2025 - 2026", {"overcut.spaced_year_range": 1}),
    ("Invoices 1001-1004", {"overcut.two_group_dash_number": 1}),
    ("Fees 1,200 1,300", {"overcut.two_bare_thousands_side_by_side": 1}),
    (
        "TUITION 2025-2026. 3 CREDITS",
        {
            "overcut.punctuation_after_year_range_or_zip4": 1,
            "neighbour.year_range_cut": 1,
        },
    ),
    ("Total 123,456,789", {"limit.lone_bare_thousands_kept": 1, "cuts": 0}),
    ("12.11 3456 7890 1234", {"limit.date_shaped_group_before_a_cut": 1}),
    ("PMT 4111 1111 1111 1234.50", {"limit.amount_right_after_a_cut": 1}),
]


@pytest.mark.parametrize(("line", "expected"), _CASES)
def test_each_shape_adds_its_counts(line: str, expected: dict[str, int]) -> None:
    counts = _count(line)
    assert {label: counts[label] for label in expected} == expected
    assert counts["check.cut_count_mismatch"] == 0


_KINDS = ("pdf", "txt", "json")


@pytest.mark.parametrize("kind", _KINDS)
@pytest.mark.parametrize(("line", "expected"), _CASES)
def test_each_set_aside_is_also_counted_under_its_input_kind(
    line: str, expected: dict[str, int], kind: str
) -> None:
    counts: Counter[str] = Counter()
    mask_counts.count_field(mask_counts._Mask(), line, counts, kind)
    for label, total in expected.items():
        if not label.startswith("set_aside."):
            continue
        assert counts[label] == total
        assert counts[f"{label}.from_{kind}"] == total
        for other in set(_KINDS) - {kind}:
            assert counts[f"{label}.from_{other}"] == 0


def test_every_mask_label_has_a_case() -> None:
    watched = {
        label
        for label in mask_counts.LABELS
        if label.split(".")[0]
        in {"set_aside", "neighbour", "effect", "overcut", "limit"}
    }
    covered = {label for _, expected in _CASES for label in expected}
    # The per-kind rows, checked by the test above for each case's totals.
    covered |= {
        f"{label}.from_{kind}"
        for label in covered
        if label.startswith("set_aside.")
        for kind in _KINDS
    }
    assert watched - covered == set()
    # Every set-aside total has its per-kind rows.
    for label in watched:
        if label.startswith("set_aside.") and label.count(".") == 1:
            for kind in _KINDS:
                assert f"{label}.from_{kind}" in mask_counts.LABELS


def test_field_counts_and_the_self_check() -> None:
    counts = _count(
        "AMZN 11/09 $45.99", "No digits here", "CARD 4111 1111 1111 1234", "11/09"
    )
    assert counts["fields"] == 4
    assert counts["fields.with_a_digit_span"] == 3
    assert counts["fields.with_a_cut"] == 1
    assert counts["fields.with_set_aside.short_date"] == 2
    assert counts["check.cut_count_mismatch"] == 0


def test_the_self_check_counts_a_drift_from_the_mask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from receipt_index import statement_extraction as se

    monkeypatch.setattr(se, "mask_digits", lambda text: (text, 7))
    assert _count("CARD 4111 1111 1111 1234")["check.cut_count_mismatch"] == 1


def test_render_refuses_a_label_it_doesnt_list() -> None:
    with pytest.raises(RuntimeError):
        mask_counts.render(Counter({"Synthetic Savings Bank": 1}))


# ---------------------------------------------------------------------------
# End to end: the output holds nothing from the input
# ---------------------------------------------------------------------------

_CARD = "4929 3817 6620 7319"
_MERCHANT = "Quillfeather Stationers"
_BANK = "Synthetic Savings Bank"
# A statement's address line: its ZIP+4 is set aside on a page or text line.
_ADDRESS = "1 MAIN ST SEATTLE WA 98101-1234"


def _windows(number: str) -> set[str]:
    digits = number.replace(" ", "")
    return {digits[i : i + 4] for i in range(len(digits) - 3)}


def _corpus(root: Path) -> Path:
    folder = root / f"{_BANK} statements"
    folder.mkdir()
    (folder / f"{_BANK} 2027-01 card {_CARD[-4:]}.txt").write_text(
        f"{_BANK}\n{_ADDRESS}\nCARD {_CARD} 01/05 {_MERCHANT} $45.99\n"
        f"PAYMENT THANK YOU 01/20 -120.00\nACCT 12345-6789 0123\n",
        encoding="utf-8",
    )
    (folder / f"{_BANK} extraction {_CARD[-4:]}.json").write_text(
        json.dumps(
            {
                "institution": _BANK,
                "lines": [
                    {"description": f"{_MERCHANT} {_CARD}", "section": "Purchases"},
                    {"description": "REF 20261109", "section": None},
                ],
            }
        ),
        encoding="utf-8",
    )
    (folder / f"{_BANK} {_CARD[-4:]}.pdf").write_bytes(
        make_pdf(f"{_MERCHANT} CARD {_CARD} 01/05 45.99", pages=2)
    )
    (folder / f"{_BANK} address {_CARD[-4:]}.pdf").write_bytes(make_pdf(_ADDRESS))
    (folder / f"{_BANK} notes {_CARD[-4:]}.csv").write_text(_CARD, encoding="utf-8")
    (folder / f"{_BANK} broken {_CARD[-4:]}.json").write_text(
        f"{{ {_CARD}", encoding="utf-8"
    )
    return folder


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def test_the_output_is_fixed_labels_and_counts_and_holds_no_input(
    tmp_path: Path,
) -> None:
    folder = _corpus(tmp_path)
    result = _run(str(folder))
    assert result.returncode == 0, "the count failed"
    out = result.stdout + result.stderr

    # Nothing from the input: no digits of the card, no names, no path.
    for window in _windows(_CARD):
        assert window not in out
    for text in (_MERCHANT, _BANK, "statements", "extraction", str(tmp_path)):
        assert text not in out
    assert result.stderr == ""

    # Every line is a fixed header line or a listed label and a count.
    lines = result.stdout.splitlines()
    assert lines[: len(mask_counts.HEADER)] == list(mask_counts.HEADER)
    body = lines[len(mask_counts.HEADER) :]
    assert [line.split("\t")[0] for line in body] == list(mask_counts.LABELS)
    for line in body:
        assert re.fullmatch(r"[a-z0-9_.]+\t[0-9]+", line), "an unexpected line"

    counts = {k: int(v) for k, v in (line.split("\t") for line in body)}
    assert counts["files.txt"] == 1
    # The broken JSON counts only as failed, with nothing else of it.
    assert counts["files.json"] == 1
    assert counts["files.pdf"] == 2
    assert counts["files.failed"] == 1  # the broken JSON: counted, not named
    assert counts["files.other_type_skipped"] == 1
    assert counts["files.unreadable_folder"] == 0
    assert counts["neighbour.zip4_cut"] == 1
    # The address line's ZIP+4, set aside in the text file and the PDF; the
    # extracted descriptions hold none, so the decision's count is 0.
    assert counts["set_aside.zip4_alone"] == 2
    assert counts["set_aside.zip4_alone.from_txt"] == 1
    assert counts["set_aside.zip4_alone.from_pdf"] == 1
    assert counts["set_aside.zip4_alone.from_json"] == 0
    # Two dates and an amount in the text file; a date and an amount on
    # each card PDF page; none in the JSON.
    assert counts["set_aside.short_date.from_txt"] == 2
    assert counts["set_aside.short_date.from_pdf"] == 2
    assert counts["set_aside.short_date.from_json"] == 0
    # Each total is the sum of its per-kind rows.
    for shape in mask_counts._SET_ASIDE_SHAPES:
        assert counts[f"set_aside.{shape}"] == sum(
            counts[f"set_aside.{shape}.from_{kind}"] for kind in ("pdf", "txt", "json")
        )
    assert counts["effect.date_without_separators_cut"] == 1
    # The card, in the text file, the extraction and both PDF pages.
    assert counts["cuts"] >= 4
    assert counts["check.cut_count_mismatch"] == 0


def _assert_nothing_echoed(
    result: subprocess.CompletedProcess[str], *paths: Path
) -> None:
    out = result.stdout + result.stderr
    assert "Traceback" not in out
    assert _BANK not in out
    for window in _windows(_CARD):
        assert window not in out
    for path in paths:
        assert str(path) not in out
        assert path.name not in out


_ROOT_ERROR = "error: the path given doesn't exist or can't be read\n"


def test_a_missing_path_is_not_echoed(tmp_path: Path) -> None:
    missing = tmp_path / f"{_BANK} {_CARD}"
    result = _run(str(missing))
    assert result.returncode == 2
    _assert_nothing_echoed(result, missing)
    assert result.stderr == _ROOT_ERROR
    assert result.stdout == ""


def test_a_path_too_long_for_the_os_is_not_echoed(tmp_path: Path) -> None:
    # One component over 255 bytes: ENAMETOOLONG on any touch.
    too_long = tmp_path / f"{_BANK} {_CARD} {'x' * 260}"
    result = _run(str(too_long))
    assert result.returncode == 2
    _assert_nothing_echoed(result, too_long)
    assert result.stderr == _ROOT_ERROR
    assert result.stdout == ""


_AS_ROOT = sys.platform != "win32" and os.geteuid() == 0


@pytest.mark.skipif(_AS_ROOT, reason="root reads a folder with no permissions")
@pytest.mark.parametrize("inside", [False, True], ids=["root", "root_inside"])
def test_an_unreadable_path_is_not_echoed(tmp_path: Path, inside: bool) -> None:
    locked = tmp_path / f"{_BANK} {_CARD}"
    locked.mkdir()
    (locked / f"{_BANK} {_CARD[-4:]}.txt").write_text(_CARD, encoding="utf-8")
    root = locked / f"{_BANK} 2027" if inside else locked
    if inside:
        root.mkdir()
    locked.chmod(0)
    try:
        result = _run(str(root))
    finally:
        locked.chmod(0o700)
    assert result.returncode == 2
    _assert_nothing_echoed(result, root)
    assert result.stderr == _ROOT_ERROR
    assert result.stdout == ""


@pytest.mark.skipif(_AS_ROOT, reason="root reads a folder with no permissions")
def test_an_unreadable_subfolder_is_counted_not_named(tmp_path: Path) -> None:
    folder = tmp_path / "statements"
    folder.mkdir()
    (folder / "a.txt").write_text("AMZN 11/09 $45.99\n", encoding="utf-8")
    locked = folder / f"{_BANK} {_CARD}"
    locked.mkdir()
    (locked / f"{_BANK} {_CARD[-4:]}.txt").write_text(_CARD, encoding="utf-8")
    locked.chmod(0)
    try:
        result = _run(str(folder))
    finally:
        locked.chmod(0o700)
    assert result.returncode == 0, "the count failed"
    _assert_nothing_echoed(result, locked)
    assert result.stderr == ""
    body = result.stdout.splitlines()[len(mask_counts.HEADER) :]
    counts = {k: int(v) for k, v in (line.split("\t") for line in body)}
    assert counts["files.unreadable_folder"] == 1
    assert counts["files.txt"] == 1
    assert counts["files.failed"] == 0


def test_usage_without_a_path() -> None:
    result = _run()
    assert result.returncode == 2
    assert result.stdout == ""
