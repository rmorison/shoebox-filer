"""The mask's bound, as the MCP statement tools check it at read (plan U7).

``within_mask_bound`` is the one implementation of the bound: no span shows
more than 12 digits, and no eight digits stand in a row, reading digits as
the mask does (any script's, folded). Synthetic numbers only.
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
        "Chapter 12 then 4111 1111 then 1234 5678 9012",
    ],
)
def test_text_within_the_bound_passes(text: str | None) -> None:
    assert within_mask_bound(text)


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
    ],
)
def test_text_past_the_bound_fails(label: str, text: str) -> None:
    assert not within_mask_bound(text), label


def test_the_mask_output_of_a_card_number_passes() -> None:
    masked, runs = mask_digits("CARD 4111-1111-1111-1234 $12.50")
    assert runs == 1
    assert within_mask_bound(masked)
