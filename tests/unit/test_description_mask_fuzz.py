"""The mask's card-length bound, fuzzed, and its backstop.

A span of 13 counted digits or more never shows more than 12 digits once
masked: its last four and at most one kept amount (six whole digits and
cents). A seeded stdlib fuzz checks that bound on spans built from digit
groups, amounts, currency symbols and codes, invisible characters, spaces
and joiners. Raise ``MASK_FUZZ_ITERATIONS`` (and change ``MASK_FUZZ_SEED``)
for a longer run.
"""

from __future__ import annotations

import os
import random
from typing import TYPE_CHECKING

from receipt_index import statement_extraction as se
from receipt_index.statement_extraction import mask_digits

if TYPE_CHECKING:
    import pytest

_ITERATIONS = int(os.environ.get("MASK_FUZZ_ITERATIONS", "3000"))
_SEED = int(os.environ.get("MASK_FUZZ_SEED", "20261115"))
_MAX_SHOWN = 4 + 8

# Currency symbols (Sc), with fullwidth and small forms.
_SYMBOLS = ["$", "\u20ac", "\u00a3", "\u00a5", "\uff04", "\ufe69", "\uffe5", "\u20b9"]
_INVISIBLES = ["\u200b", "\u00ad", "\u2060", "\u200d", "\ufeff"]
_MARKS = ["-", ".", ",", "/", "*", "#", ":", "|", "+", "x", "(", ")", "'"]
_CODES = ["USD", "EUR", "ALL", "CAD", "GBP"]


def _group(rng: random.Random, digits: str) -> str:
    """One group of digits, sometimes written as an amount with cents."""
    if len(digits) >= 3 and rng.random() < 0.5:
        point = rng.choice([".", ","])
        whole = digits[:-2]
        if len(whole) > 3 and rng.random() < 0.3:
            sep = "," if point == "." else "."
            head = len(whole) % 3 or 3
            parts = [whole[:head]] + [
                whole[i : i + 3] for i in range(head, len(whole), 3)
            ]
            whole = sep.join(parts)
        return whole + point + digits[-2:]
    return digits


def _separator(rng: random.Random) -> str:
    pieces = rng.choices([" ", *_SYMBOLS, *_INVISIBLES, *_MARKS], k=rng.randint(1, 3))
    return "".join(pieces)


def _card_length_text(rng: random.Random) -> str:
    total = rng.randint(13, 19)
    digits = "".join(rng.choice("0123456789") for _ in range(total))
    groups: list[str] = []
    at = 0
    while at < total:
        size = min(rng.randint(1, 9), total - at)
        groups.append(_group(rng, digits[at : at + size]))
        at += size
    body = groups[0]
    for group in groups[1:]:
        body += _separator(rng) + group
    # A symbol, a code or nothing on either side; letters only out here.
    left = rng.choice(["", *_SYMBOLS, *(c + " " for c in _CODES), "PAID "])
    right = rng.choice(["", *_SYMBOLS, *(" " + c for c in _CODES), " " + "REF"])
    return left + body + right


def _counted(text: str) -> int:
    return sum(ch.isdigit() for ch in se._prepare(text))


def test_a_card_length_span_never_shows_more_than_twelve_digits() -> None:
    rng = random.Random(_SEED)
    for _ in range(_ITERATIONS):
        text = _card_length_text(rng)
        spans = [s for s in se._span().finditer(se._prepare(text)) if s.group()]
        if len(spans) != 1 or _counted(text) < se._MIN_CARD_DIGITS:
            continue
        masked, _ = mask_digits(text)
        assert masked is not None
        shown = sum(ch.isdigit() for ch in masked)
        assert shown <= _MAX_SHOWN, ascii(text)


def test_the_backstop_cuts_a_span_the_rules_left_too_long(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With the rules switched off, the backstop alone still cuts.
    monkeypatch.setattr(se, "_cut_tokens", lambda span, *_: (span, 0))
    assert mask_digits("Card 4111 1111 1111 1234") == ("Card 1234", 1)
    assert mask_digits("Card 4111 1111 1234") == ("Card 4111 1111 1234", 0)


def test_the_backstop_leaves_a_span_within_the_bound() -> None:
    assert se._backstop("4111 1111 1111 1234", "1234 $901234.56", 1) == (
        "1234 $901234.56",
        1,
    )
    assert se._backstop("4111 1111 1111 1234", "1111 1111 1111 1234", 0) == (
        "1234",
        1,
    )
