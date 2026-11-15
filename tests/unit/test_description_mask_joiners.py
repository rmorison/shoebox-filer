"""The description mask joins digit groups across any punctuation or symbol.

A card number whose groups are split by a mark institutions don't print
inside numbers (``# : * | + x``, a lookalike, a fullwidth form) is still a
card number: the mask joins its groups and cuts it to the last four. Non-ASCII
characters are written as escapes so this file stays plain ASCII.
"""

from __future__ import annotations

import sys
import unicodedata

import pytest

from receipt_index.statement_extraction import mask_digits

_GROUPS = ["4111", "2222", "3333", "4444"]

# The issue's table: each left a card number whole.
_ISSUE_MARKS = ["#", ":", "*", "|", "+", "x"]

# The same marks as fullwidth characters, and as other characters whose
# NFKC form is the mark (small forms, a mathematical x).
_FULLWIDTH_MARKS = [
    "\uff03",  # fullwidth number sign
    "\uff1a",  # fullwidth colon
    "\uff0a",  # fullwidth asterisk
    "\uff5c",  # fullwidth vertical line
    "\uff0b",  # fullwidth plus
    "\uff58",  # fullwidth small x
    "\uff38",  # fullwidth capital X
]
_NFKC_MARKS = [
    "\ufe5f",  # small number sign
    "\ufe55",  # small colon
    "\ufe61",  # small asterisk
    "\ufe62",  # small plus
    "\U0001d431",  # mathematical bold small x
    "\u2093",  # subscript small x
    "\u02e3",  # modifier small x
]

# Still whole on main, found in review: the multiplication sign and the
# semicolon.
_STILL_WHOLE = ["\u00d7", ";"]

# The lookalike set from the review, and Cyrillic ha.
_LOOKALIKES = [
    "\u00d7",  # multiplication sign
    "\u2217",  # asterisk operator
    "\u204e",  # low asterisk
    "\u30fb",  # katakana middle dot
    "\u00b7",  # middle dot
    "\u2022",  # bullet
    "\u2027",  # hyphenation point
    "\u00a6",  # broken bar
    "\u3001",  # ideographic comma
    "\u201a",  # single low-9 quotation mark
    "\u060c",  # Arabic comma
    "\u066b",  # Arabic decimal separator
    "\u2044",  # fraction slash
    "\u2215",  # division slash
    ";",
    "~",
    "=",
    "\\",
    "'",
    "\u0445",  # Cyrillic small ha
    "\u0425",  # Cyrillic capital ha
    "X",
]


def _number(mark: str) -> str:
    return mark.join(_GROUPS)


@pytest.mark.parametrize("mark", _ISSUE_MARKS)
def test_each_mark_in_the_issue_joins_a_card_number(mark: str) -> None:
    assert mask_digits(_number(mark)) == ("4444", 1)
    assert mask_digits(f"Card {_number(mark)} ok") == ("Card 4444 ok", 1)


@pytest.mark.parametrize("mark", _FULLWIDTH_MARKS + _NFKC_MARKS)
def test_a_fullwidth_or_compatibility_form_of_a_mark_joins(mark: str) -> None:
    folded = unicodedata.normalize("NFKC", mark)
    assert folded in _ISSUE_MARKS or folded == "X", repr(mark)
    assert mask_digits(f"Card {_number(mark)}") == ("Card 4444", 1)


@pytest.mark.parametrize("mark", _STILL_WHOLE)
def test_the_review_marks_still_whole_on_main_join(mark: str) -> None:
    assert mask_digits(f"Card {_number(mark)}") == ("Card 4444", 1)


@pytest.mark.parametrize("mark", _LOOKALIKES)
def test_each_lookalike_joins(mark: str) -> None:
    assert mask_digits(f"Card {_number(mark)}") == ("Card 4444", 1)


@pytest.mark.parametrize(
    "number",
    [
        "4111\u200b2222\u200b3333\u200b4444",  # zero-width space
        "4111\u00ad2222\u00ad3333\u00ad4444",  # soft hyphen
        "4111,,,,2222,,,,3333,,,,4444",
        "4111----2222----3333----4444",
        "4111 . . 2222 . . 3333 . . 4444",
    ],
)
def test_what_was_already_masked_stays_masked(number: str) -> None:
    assert mask_digits(f"Card {number}") == ("Card 4444", 1)


@pytest.mark.parametrize(
    "number",
    [
        "4111#2222*3333 4444",
        "4111 # 2222 :: 3333|+4444",
        "4111-2222#3333.4444",
        "4111x2222\u00d73333\u04454444",
        "4111\uff032222:3333;4444",
        "4111#:*2222|+x3333#4444",
        "4111 #2222# 3333 # 4444",
    ],
)
def test_mixed_separators_within_one_number_join(number: str) -> None:
    assert mask_digits(f"Card {number}") == ("Card 4444", 1)


@pytest.mark.parametrize("length", [1, 2, 3, 4, 5, 6, 7, 8, 50, 500])
@pytest.mark.parametrize("mark", ["#", "*", "x", "\u00d7", "|:"])
def test_a_separator_run_of_any_length_joins(mark: str, length: int) -> None:
    run = mark * length
    assert mask_digits(f"Card {run.join(_GROUPS)}") == ("Card 4444", 1)


def test_a_combining_mark_after_a_new_joiner_cannot_split_a_number() -> None:
    # An invisible character sitting next to a joiner is dropped, as it is
    # next to a digit, a space or a dash.
    for invisible in ["\u0301", "\u180b", "\U000110bd", "\u20dd"]:
        for mark in ["#", "*", "x", "\u00d7"]:
            number = (mark + invisible).join(_GROUPS)
            assert mask_digits(f"Card {number}") == ("Card 4444", 1), repr(
                (mark, invisible)
            )
            number = (invisible + mark).join(_GROUPS)
            assert mask_digits(f"Card {number}") == ("Card 4444", 1), repr(
                (mark, invisible)
            )


@pytest.mark.parametrize(
    "text",
    [
        "4111q2222q3333q4444",  # a letter that isn't an x form
        "4111\u04302222\u04303333\u04304444",  # Cyrillic small a
        "4111\u30422222\u30423333\u30424444",  # hiragana a
        "Ref 1234 Box 5678",  # a word of letters between groups
        "Ref 1234 xyz 5678",  # x is a joiner, a word with it isn't
    ],
)
def test_a_letter_that_is_not_an_x_still_ends_a_run(text: str) -> None:
    assert mask_digits(text) == (text, 0)


@pytest.mark.parametrize(
    "text",
    [
        "STORE #0412 $45.99",
        "STORE 0412*45.99",
        "STORE #0412 | 11/09 | $45.99",
        "Fees 45.99+12.00",
        "2 x $12.50 = $25.00",
        "Qty 3 x 1,234.56",
        "3x 19.99 = 59.97",
        "STORE #0412: $1,250.00",
        "Ref: 0412 Amt: $45.99",
        "Paid \uff0412.50 at \uff030412",  # fullwidth marks, kept as printed
    ],
)
def test_an_amount_beside_a_store_number_stays_readable(text: str) -> None:
    assert mask_digits(text) == (text, 0)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # No cents: the store number and the figure can't be told apart
        # from a number's groups, so they are cut together.
        ("STORE 0412*4599", "STORE 4599"),
        ("STORE #0412 #4599", "STORE #4599"),
        # A number whose last group runs into cents keeps that group, as it
        # does when the groups are spaced.
        ("Card 4111*1111*1111*1234.50", "Card 1111*1234.50"),
    ],
)
def test_where_an_amount_and_a_number_cant_be_told_apart_the_mask_cuts(
    text: str, expected: str
) -> None:
    assert mask_digits(text) == (expected, 1)


def test_three_decimal_figures_beside_a_store_number_are_cut() -> None:
    masked, runs = mask_digits("STORE 0412#12.345")
    assert masked is not None
    assert runs == 1
    assert len([ch for ch in masked if ch.isdigit()]) == 4


def _is_named_joiner(ch: str) -> bool:
    """The rule as stated: punctuation, symbol, modifier letter, spacing
    mark, digit outside ASCII, or an x letter (or one built on it), after
    NFKC."""
    x_letters = "xX\u0445\u0425\u03c7\u03a7"

    def direct(c: str) -> bool:
        category = unicodedata.category(c)
        return (
            c in x_letters
            or category[0] in "PS"
            or category in {"Lm", "Mc"}
            or (category in {"Nd", "No", "Nl"} and not c.isascii())
        )

    if direct(ch):
        return True
    decomposed = unicodedata.normalize("NFKD", ch)
    if (
        len(decomposed) > 1
        and decomposed[0] in x_letters
        and all(unicodedata.category(c) in {"Mn", "Me"} for c in decomposed[1:])
    ):
        return True
    folded = unicodedata.normalize("NFKC", ch)
    return bool(folded) and all(direct(c) for c in folded)


def _joiners() -> list[str]:
    return [
        chr(code)
        for code in range(sys.maxunicode + 1)
        if not 0xD800 <= code <= 0xDFFF and _is_named_joiner(chr(code))
    ]


def test_every_punctuation_or_symbol_character_joins_a_number() -> None:
    # Every joiner the rule names sits between two digits of a 16-digit
    # number, 15 to a number, each number fenced by a letter that doesn't
    # join: only the last four digits of each may survive.
    characters = _joiners()
    assert len(characters) > 5000
    digits = "4111111111113456"
    entries = []
    for start in range(0, len(characters), 15):
        between = characters[start : start + 15]
        between += [" "] * (15 - len(between))
        entries.append(
            "".join(d + c for d, c in zip(digits, [*between, ""], strict=True))
        )
    for batch_start in range(0, len(entries), 2000):
        batch = entries[batch_start : batch_start + 2000]
        masked, runs = mask_digits(" q ".join(batch))
        assert masked is not None
        assert runs == len(batch)
        pieces = masked.split(" q ")
        assert len(pieces) == len(batch)
        for piece, entry in zip(pieces, batch, strict=True):
            kept = "".join(ch for ch in piece if "0" <= ch <= "9")
            assert kept == "3456", repr(entry)


def test_the_mask_classifies_by_nfkc_but_leaves_the_text_alone() -> None:
    # A fullwidth number sign joins, and the text outside the cut keeps it.
    text = "Ref \uff03A \uff03" + _number("\uff03")
    assert mask_digits(text) == ("Ref \uff03A \uff034444", 1)


# Found in the pre-PR review, and signed off as joiners too: modifier
# letters (Lm), spacing marks (Mc), digits outside ASCII (Nd, No, Nl) and the
# chi and x forms. None of them counts toward the eight digits.
_WIDENED = [
    "\u02bc",  # Lm, looks like an apostrophe
    "\u02d0",  # Lm, looks like a colon
    "\u02c8",  # Lm, looks like a bar
    "\u02b9",  # Lm, looks like a prime
    "\u037a",  # Lm
    "\ua4f8",  # Lm, looks like a dot
    "\ua4f9",  # Lm, looks like a comma
    "\ua4fd",  # Lm, looks like a colon
    "\u30fc",  # Lm, looks like a dash
    "\u0903",  # Mc
    "\u0661",  # Nd outside ASCII
    "\uff14",  # Nd, fullwidth
    "\u00b2",  # No, superscript
    "\u2460",  # No, circled
    "\u2167",  # Nl, a numeral letter
    "\u03a7",  # chi, capital
    "\u03c7",  # chi, small
    "\u1e8b",  # x with a dot above
    "\u1e8d",  # x with a diaeresis
]


@pytest.mark.parametrize("mark", _WIDENED)
def test_a_widened_joiner_joins_a_card_number(mark: str) -> None:
    assert mask_digits(f"Card {_number(mark)}") == ("Card 4444", 1)


def test_a_digit_outside_ascii_never_counts_toward_the_eight() -> None:
    # Seven ASCII digits joined by digits outside ASCII: not a run.
    text = "Ref 123\uff14\uff14\uff144567"
    assert mask_digits(text) == (text, 0)


# Known limits, signed off: other letters (Lo) that look like a mark still
# end a run, since joining across every Lo letter would join the numbers
# either side of any word in most non-Latin scripts. Each is pinned: a
# change here must update the plan's known limits too.
_LO_LOOKALIKES = [
    "\u01c0",  # looks like a bar
    "\u01c3",  # looks like an exclamation mark
    "\ua78f",  # looks like a middle dot
    "\u1427",  # looks like a middle dot
    "\u4e00",  # looks like a dash
]


@pytest.mark.parametrize("mark", _LO_LOOKALIKES)
def test_an_other_letter_lookalike_is_a_known_limit(mark: str) -> None:
    text = f"Card {_number(mark)}"
    assert mask_digits(text) == (text, 0)
