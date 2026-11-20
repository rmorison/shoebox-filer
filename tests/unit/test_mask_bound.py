"""The mask's bound, as the MCP statement tools check it at read (plan U7).

``within_mask_bound`` is the one implementation of the bound: no run the
mask would cut (eight digits or more across a span's number tokens, as
``_cut_runs`` reads them), no span showing more than 12 digits, and no
eight digits in a row, reading digits as the mask does (any script's,
folded). Synthetic numbers only.
"""

from __future__ import annotations

import pytest

from receipt_index.statement_extraction import mask_digits, within_mask_bound


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "Synthetic grocery purchase",
        "Card 1234",
        "AMZN 11/09 $45.99",
        "STORE #0412 11/09",
        "1234 $901234.56",
        "Wire 1234567.89",
        "Fees 1,234,567,890.12 total",
        "SEATTLE WA 98101-1234",
        "Chapter 12 then 4111 then 9012",
        "Lot 123,456,789",
        "Wire $1,234,567.89",
        "HOTEL 11/09-11/12",
        "2 x $12.50 = $25.00",
    ],
)
def test_text_within_the_bound_passes(text: str | None) -> None:
    assert within_mask_bound(text)
    # And the mask leaves it as it is: the guard refuses only what the mask
    # would cut.
    if text is not None:
        assert mask_digits(text) == (text, 0)


@pytest.mark.parametrize(
    ("label", "text"),
    [
        ("eight ASCII digits in a row", "REF 12345678"),
        ("a grouped card-length number", "CARD 4111 1111 1111 1234"),
        ("groups split by marks", "4111#2222#3333#4444"),
        ("thirteen digits in one span", "1234 5678 9012 3"),
        (
            "eight fullwidth digits",
            "REF \uff11\uff12\uff13\uff14\uff15\uff16\uff17\uff18",
        ),
        (
            "Arabic-Indic digits in groups",
            "\u0664\u0661\u0661\u0661 \u0661\u0661\u0661\u0661 "
            "\u0661\u0661\u0661\u0661 \u0661\u0662\u0663\u0664",
        ),
        ("an invisible character inside a run", "REF 1234\u200b5678"),
        ("three groups of four, by spaces", "1234 5678 9012"),
        ("three groups of four, by dashes", "1234-5678-9012"),
        ("three groups of four, zero-led", "0012 3456 7890"),
        ("two groups of four after a word", "ACCT 1234 5678"),
        ("three groups of three", "REF 123 456 789"),
        ("five and three", "12345 678"),
        ("a run beside a kept amount", "PMT 1234 5678 $12.50"),
        (
            "groups of four on both sides of a letter",
            "Synthetic 1234 5678 9012 Q 3456",
        ),
        # A run split by a mark that isn't a letter: the mask reads it as
        # one run, so the guard must too.
        ("three groups of four, by middle dots (U+00B7)", "1234\u00b75678\u00b79012"),
        ("three groups of four, by bullets (U+2022)", "1234\u20225678\u20229012"),
        (
            "three groups of four, by zero-width spaces (U+200B)",
            "1234\u200b5678\u200b9012",
        ),
        (
            "a run between short words",
            "Chapter 12 then 4111 1111 then 1234 5678 9012",
        ),
    ],
)
def test_text_past_the_bound_fails(label: str, text: str) -> None:
    assert not within_mask_bound(text), label
    # The mask cuts it too: the guard is no stricter than the mask here.
    assert mask_digits(text)[1] >= 1, label


def test_the_mask_output_of_a_card_number_passes() -> None:
    masked, runs = mask_digits("CARD 4111-1111-1111-1234 $12.50")
    assert runs == 1
    assert within_mask_bound(masked)
