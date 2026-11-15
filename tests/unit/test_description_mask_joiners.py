"""The description mask joins digit groups across any punctuation or symbol.

A card number whose groups are split by a mark institutions don't print
inside numbers (``# : * | + x``, a lookalike, a fullwidth form) is still a
card number: the mask joins its groups and cuts it to the last four. Non-ASCII
characters are written as escapes so this file stays plain ASCII.
"""

from __future__ import annotations

import re
import sys
import unicodedata
from typing import TYPE_CHECKING

import pytest

from receipt_index.statement_extraction import mask_digits

if TYPE_CHECKING:
    from collections.abc import Callable

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
        # A number whose last group runs into cents: 16 digits in runs under
        # 13, so the amount-shaped group is cut with the rest.
        ("Card 4111*1111*1111*1234.50", "Card 3450"),
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
    mark, number character outside ASCII, or an x letter (or one built on
    it), after NFKC. Those the digit fold takes are left out below."""
    x_letters = (
        "xX\ua7b3\uab53\u0445\u0425\u04b2\u04b3\u04fc\u04fd\u04fe\u04ff"
        "\u03c7\u03a7\u2cac\u2cad\U00010317\u157d"
        "\u1d8d\uab54\uab55\uab56\uab57\uab58\uab59"
    )

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
    if ch.isspace() or unicodedata.category(ch) in {"Mn", "Me", "Cf", "Co", "Cn"}:
        return False
    folded = unicodedata.normalize("NFKC", ch)
    # A space and marks only (a presentation form of a vowel mark) join too.
    spacing = [c for c in folded if unicodedata.category(c) not in {"Mn", "Me"}]
    return bool(spacing) and all(c == " " or direct(c) for c in spacing)


def _stands_for_digits(ch: str) -> bool:
    # Folded to digits and counted, not a joiner: the digit tests cover it.
    # A digit value, a lone number character with a whole value, or digits in
    # its NFKC or NFKD form; never a vulgar fraction.
    nfkc = unicodedata.normalize("NFKC", ch)
    if "\u2044" in nfkc:
        # A vulgar fraction folds to digits and the fraction slash.
        return nfkc != ch
    if unicodedata.category(ch) in {"No", "Nl"}:
        value = unicodedata.numeric(ch, None)
        if value is not None and value.is_integer() and value >= 0:
            return True
    forms = ch + nfkc + unicodedata.normalize("NFKD", ch)
    return any(unicodedata.digit(c, None) is not None for c in forms)


def _joiners() -> list[str]:
    return [
        chr(code)
        for code in range(sys.maxunicode + 1)
        if not 0xD800 <= code <= 0xDFFF
        and _is_named_joiner(chr(code))
        and not _stands_for_digits(chr(code))
    ]


def test_every_character_the_rule_names_joins_a_number() -> None:
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


def test_a_digit_outside_ascii_counts_toward_the_eight() -> None:
    # Seven ASCII digits and three fullwidth ones: ten digits, cut.
    assert mask_digits("Ref 123\uff14\uff14\uff144567") == ("Ref 4567", 1)


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
    "\ufe73",  # a tail fragment: its NFKC is itself, a letter
]


@pytest.mark.parametrize("mark", _LO_LOOKALIKES)
def test_an_other_letter_lookalike_is_a_known_limit(mark: str) -> None:
    text = f"Card {_number(mark)}"
    assert mask_digits(text) == (text, 0)


# Letters that look like x or chi, cased and other (the last two are
# Lo): x forms too.
_CASED_X_LOOKALIKES = [
    "\u2cac",  # khi, capital
    "\u2cad",  # khi, small
    "\u04b2",  # Cyrillic capital ha with descender
    "\u04b3",  # Cyrillic small ha with descender
    "\u04fc",  # Cyrillic capital ha with hook
    "\u04fd",  # Cyrillic small ha with hook
    "\u04fe",  # Cyrillic capital ha with stroke
    "\u04ff",  # Cyrillic small ha with stroke
    "\ua7b3",  # Latin capital chi
    "\uab53",  # Latin small chi
    "\U00010317",  # an other letter shaped like x
    "\u157d",  # an other letter shaped like x
    "\u1d8d",  # Latin small x with palatal hook
    "\uab54",  # Latin small chi with low right ring
    "\uab55",  # Latin small chi with low left serif
    "\uab56",  # Latin small x with low right ring
    "\uab57",  # Latin small x with long left leg
    "\uab58",  # Latin small x with long left leg and low right ring
    "\uab59",  # Latin small x with long left leg with serif
]


# Other letters (Lo) shaped like x stay the signed-off Lo limit: pinned.
_LO_X_SHAPES = ["\u16ea", "\U000102b4", "\U0001029c", "\U0001bc01"]


@pytest.mark.parametrize("mark", _LO_X_SHAPES)
def test_an_other_letter_shaped_like_x_is_a_known_limit(mark: str) -> None:
    text = f"Card {_number(mark)}"
    assert mask_digits(text) == (text, 0)


@pytest.mark.parametrize("mark", _CASED_X_LOOKALIKES)
def test_a_cased_letter_that_looks_like_x_joins(mark: str) -> None:
    assert mask_digits(f"Card {_number(mark)}") == ("Card 4444", 1)


@pytest.mark.parametrize(
    "text",
    [
        # A spacing vowel sign, then a nasal sign (a combining mark), at the
        # end of a word: with no digit near, nothing is dropped.
        "\u092e\u093e\u0901 STORE",
        "\u0915\u094b\u0902 STORE",
        "Ref \u0915\u094b\u0902 0412",
        "\u0b95\u0bca\u0bcd X",
        # A combining accent between two marks, away from any number.
        "caf# \u0301 #x",
    ],
)
def test_a_combining_mark_outside_a_number_is_kept(text: str) -> None:
    assert mask_digits(text) == (text, 0)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Accepted over-cuts: a joiner beside a store number reads as part
        # of one number when what follows has no cents.
        ("STORE 0412 \u3005 4599", "STORE 4599"),
        ("STORE 0412 $1,250", "STORE 1250"),
        ("STORE 0412 \u00a51,980", "STORE 1980"),
    ],
)
def test_a_whole_amount_beside_a_store_number_is_over_cut(
    text: str, expected: str
) -> None:
    assert mask_digits(text) == (expected, 1)


def _digits_in(base: int, text: str) -> str:
    return "".join(chr(base + int(c)) if c.isdigit() else c for c in text)


_CIRCLED = {str(n): chr(0x2460 + n - 1) for n in range(1, 10)} | {"0": "\u24ea"}
_SUPERSCRIPT = {
    "0": "\u2070",
    "1": "\u00b9",
    "2": "\u00b2",
    "3": "\u00b3",
    **{str(n): chr(0x2070 + n) for n in range(4, 10)},
}


@pytest.mark.parametrize("separator", [" ", "-"])
@pytest.mark.parametrize(
    "digits",
    [
        lambda t: _digits_in(0x0660, t),  # decimal digits outside ASCII
        lambda t: _digits_in(0x06F0, t),
        lambda t: _digits_in(0xFF10, t),  # fullwidth
        lambda t: _digits_in(0x1D7CE, t),  # mathematical bold
        lambda t: "".join(_CIRCLED.get(c, c) for c in t),
        lambda t: "".join(_SUPERSCRIPT.get(c, c) for c in t),
    ],
    ids=["0660", "06f0", "fullwidth", "mathematical", "circled", "superscript"],
)
def test_a_number_written_wholly_in_other_digits_is_cut(
    digits: Callable[[str], str], separator: str
) -> None:
    number = digits(separator.join(_GROUPS))
    assert mask_digits(f"Card {number}") == ("Card 4444", 1)


def test_other_digits_print_as_ascii_and_amounts_and_dates_stay() -> None:
    # A digit fold changes the text beyond a cut: it prints in ASCII.
    text = "Paid \uff04" + _digits_in(0xFF10, "45.99 on 11/09")
    assert mask_digits(text) == ("Paid \uff0445.99 on 11/09", 0)
    assert mask_digits("Item \u2460 ok") == ("Item 1 ok", 0)


@pytest.mark.parametrize(
    "separator",
    [
        "\u0301" * 9,  # nine combining accents
        "\ue000",  # private use
        "\U00100000",  # private use, supplementary plane
        "\U0001000c",  # unassigned
        "\ue000\u0301 \U0001000c",
    ],
)
def test_footings_invisible_separators_join(separator: str) -> None:
    assert mask_digits(f"Card {separator.join(_GROUPS)}") == ("Card 4444", 1)


def _expanding_to_digits() -> list[str]:
    ranges = [(0x32C0, 0x32CB), (0x3358, 0x3370), (0x33E0, 0x33FE)]
    return [chr(code) for low, high in ranges for code in range(low, high + 1)]


@pytest.mark.parametrize("symbol", _expanding_to_digits())
def test_a_symbol_that_expands_to_digits_counts_them(symbol: str) -> None:
    # Seven digits and a symbol whose NFKC form holds digits: eight or more.
    masked, runs = mask_digits(f"Card 1234567{symbol} paid")
    assert runs == 1
    assert masked is not None
    assert re.fullmatch(r"Card [0-9]{4}\S* paid", masked), repr(masked)
    # As a separator its digits count too, and the number is cut.
    masked, runs = mask_digits(f"Card {symbol.join(_GROUPS)}")
    assert runs == 1
    assert masked is not None
    assert re.fullmatch(r"Card [0-9]{4}", masked), repr(masked)


@pytest.mark.parametrize(
    "separator",
    [
        "\u2057",  # quadruple prime, four primes under NFKC
        "\u2026",  # ellipsis, three dots under NFKC
        "\u2057" * 20,
        "\u2026 \u2057 \u2026",
    ],
)
def test_a_separator_that_expands_under_nfkc_does_not_split_a_number(
    separator: str,
) -> None:
    assert mask_digits(f"Card {separator.join(_GROUPS)}") == ("Card 4444", 1)


# Characters that stand for a digit without a decimal value: a digit value
# only (unicodedata.digit), or a single number character (No, Nl) whose value
# is a whole number from 0 to 9. Each is the digit one of its set.
_OTHER_ONES = [
    "\u1369",  # a digit-value-only one, block 1369-1371
    "\u24f5",  # double circled one
    "\u2776",  # dingbat negative circled one
    "\u2780",  # dingbat circled sans-serif one
    "\u278a",  # dingbat negative circled sans-serif one
    "\U00010a40",  # a digit-value-only one, block 10A40-10A43
    "\U00010e60",  # a digit-value-only one, block 10E60-10E68
    "\u19da",  # a digit-value-only one
    "\u3021",  # a number letter (Nl) worth one, block 3021-3029
    "\u2160",  # Roman numeral one
    "\u2170",  # small Roman numeral one
    "\U0001d360",  # counting rod one
]


@pytest.mark.parametrize("one", _OTHER_ONES)
def test_a_number_written_wholly_in_digit_value_characters_is_cut(one: str) -> None:
    number = " ".join([one * 4] * 4)
    assert mask_digits(f"Card {number}") == ("Card 1111", 1)


@pytest.mark.parametrize("one", _OTHER_ONES)
def test_a_digit_value_character_counts_toward_the_eight(one: str) -> None:
    # Five ASCII digits and three that stand for one: eight digits, cut.
    assert mask_digits(f"Ref 123{one * 3}45") == ("Ref 1145", 1)


@pytest.mark.parametrize("zero", ["\u24ff", "\u3007"])
def test_a_digit_value_zero_counts_toward_the_eight(zero: str) -> None:
    assert mask_digits(f"Ref 123{zero * 3}45") == ("Ref 0045", 1)


# Vulgar fractions: each folds to its NFKC form, digits and the fraction
# slash, and its digits count. After a digit it takes a space first, so a
# whole number and a fraction stay apart.
_FRACTIONS = [
    "\u00bc",
    "\u00bd",
    "\u00be",
    *[chr(code) for code in range(0x2150, 0x2160)],
    "\u2189",
]


@pytest.mark.parametrize("fraction", _FRACTIONS)
def test_a_fractions_digits_count(fraction: str) -> None:
    masked, runs = mask_digits(f"Ref 1234{fraction}456")
    assert runs == 1
    assert masked is not None
    assert len([ch for ch in masked if ch.isdigit()]) == 4


def test_a_fraction_one_inside_a_number_counts() -> None:
    assert mask_digits("Acct 123\u215f4567") == ("Acct 4567", 1)


def test_a_fraction_after_a_whole_number_stays_readable() -> None:
    assert mask_digits("Paid 72\u00bd lb") == ("Paid 72 1\u20442 lb", 0)
    assert mask_digits("Paid \u00bd lb") == ("Paid 1\u20442 lb", 0)
    assert mask_digits("\u00bd lb") == ("1\u20442 lb", 0)


@pytest.mark.parametrize("fraction", _FRACTIONS)
def test_a_fraction_between_groups_still_joins(fraction: str) -> None:
    assert mask_digits(f"Card {_number(fraction)}") == ("Card 4444", 1)


# Known limit, signed off: other letters (Lo) with a numeral value are not
# folded, so a number written wholly in them passes unmasked, and one between
# digit groups ends a run like any letter. A sample, pinned.
_LO_NUMERALS = ["\u4e00", "\u4e8c", "\u4e09", "\u56db", "\u4e94", "\u4e5d"]


@pytest.mark.parametrize("numeral", _LO_NUMERALS)
def test_an_other_letter_numeral_is_a_known_limit(numeral: str) -> None:
    whole = "Card " + " ".join([numeral * 4] * 4)
    assert mask_digits(whole) == (whole, 0)
    split = f"Card {_number(numeral)}"
    assert mask_digits(split) == (split, 0)


# Single characters worth a whole number of 10 or more: they fold to that
# number's digits and count (negative circled 11 to 20, double circled
# ten, dingbat circled tens, circled numbers on a black square).
_TENS_AND_MORE = [
    *[chr(code) for code in range(0x24EB, 0x24F5)],
    "\u24fe",
    "\u277f",
    "\u2789",
    "\u2793",
    *[chr(code) for code in range(0x3248, 0x3250)],
]


@pytest.mark.parametrize("number", _TENS_AND_MORE)
def test_a_character_worth_ten_or_more_counts(number: str) -> None:
    value = str(int(unicodedata.numeric(number)))
    digits = "41" + value * 3 + "11"
    assert mask_digits(f"Acct 41{number * 3}11") == (f"Acct {digits[-4:]}", 1)


def test_three_negative_circled_elevens_count() -> None:
    assert mask_digits("Acct 41\u24eb\u24eb\u24eb11") == ("Acct 1111", 1)


# Presentation forms (Lo) whose NFKC form is only a space, marks or
# joiners: they render as a mark, so they join as one.
_PRESENTATION_MARKS = [
    *[
        chr(code)
        for code in range(0xFE70, 0xFE80)
        if unicodedata.category(chr(code)) == "Lo" and code != 0xFE73
    ],
    *[chr(code) for code in range(0xFCF2, 0xFCF5)],
    *[chr(code) for code in range(0xFC5E, 0xFC64)],
]


def test_the_presentation_marks_are_other_letters() -> None:
    assert len(_PRESENTATION_MARKS) == 23
    assert {unicodedata.category(ch) for ch in _PRESENTATION_MARKS} == {"Lo"}


@pytest.mark.parametrize("mark", _PRESENTATION_MARKS)
def test_a_presentation_form_of_a_mark_joins(mark: str) -> None:
    assert mask_digits(f"Card {_number(mark)}") == ("Card 4444", 1)


# An amount or a date is set aside only in a span of fewer than 13 digits,
# its own included: the shortest card number. A card number's groups that
# look like an amount or a date are cut with the rest.
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("4111 1111 11.11 1111", "1111"),
        ("4111 11/09 1111 1234", "1234"),
        ("4111*2222*3333*44.55", "4455"),
        ("Card 4111 2222 33.44 5", "Card 3445"),  # 13 digits: the boundary
    ],
)
def test_an_amount_or_date_in_a_card_length_span_is_cut(
    text: str, expected: str
) -> None:
    assert mask_digits(text) == (expected, 1)


def test_an_amount_in_a_span_of_twelve_digits_is_kept() -> None:
    assert mask_digits("Card 4111 2222 33.44") == ("Card 2222 33.44", 1)


@pytest.mark.parametrize("text", ["STORE #0412 $45.99", "STORE 0412*45.99"])
def test_an_amount_beside_a_store_number_is_still_kept(text: str) -> None:
    assert mask_digits(text) == (text, 0)


@pytest.mark.parametrize(
    "text",
    [
        "PAYMENT 4111 1111 1111 1234 12.50",  # a whole run of 13 or more
        "RENT 11/01-11/30 1,200.00",
        "$12,345.67 $23,456.78",
        "Fees 1.11 1/11 2.22 3.33",  # four groups, under 13 digits
    ],
)
def test_amounts_and_dates_beside_a_whole_run_or_alone_are_kept(text: str) -> None:
    masked, _ = mask_digits(text)
    assert masked is not None
    assert masked.endswith(text.split()[-1])
    if not text.startswith("PAYMENT"):
        assert mask_digits(text) == (text, 0)


def test_a_card_written_only_in_amount_and_date_shapes_is_cut() -> None:
    # Four or more groups, 13 digits or more, every one set aside.
    assert mask_digits("Ref 41.11 11/11 11.11 12.34") == ("Ref 1234", 1)


# The categories, the digit fold and the joiner set follow the running
# Python's Unicode (14.0 on 3.11, 15.0 on 3.12, 15.1 on 3.13). The suite was
# written against 14.0; counts tied to a version are checked there exactly,
# and on later versions only as a floor (Unicode adds characters, it does
# not remove them).
_WRITTEN_AGAINST = "14.0.0"
_LO_NUMERALS_UNDER_14 = 58


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def _lo_numerals() -> int:
    return sum(
        1
        for code in range(sys.maxunicode + 1)
        if not 0xD800 <= code <= 0xDFFF
        and unicodedata.category(chr(code)) == "Lo"
        and unicodedata.numeric(chr(code), -1.0) in range(10)
    )


def test_the_lo_numeral_limit_counts_against_the_unicode_version() -> None:
    count = _lo_numerals()
    if unicodedata.unidata_version == _WRITTEN_AGAINST:
        assert count == _LO_NUMERALS_UNDER_14
    else:
        assert _version(unicodedata.unidata_version) > _version(_WRITTEN_AGAINST)
        assert count >= _LO_NUMERALS_UNDER_14
