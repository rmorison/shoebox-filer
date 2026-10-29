"""The page envelope the CLI shares with the MCP server."""

from __future__ import annotations

import pytest

from receipt_index.paging import next_offset, page_envelope


@pytest.mark.parametrize(
    ("offset", "page_len", "total", "expected"),
    [
        (0, 5, 12, 5),  # first page
        (5, 5, 12, 10),  # middle page
        (10, 2, 12, None),  # last, short page
        (5, 5, 10, None),  # last page, exactly full
        (40, 0, 12, None),  # past the end
        (0, 0, 0, None),  # nothing matches
    ],
)
def test_next_offset(
    offset: int, page_len: int, total: int, expected: int | None
) -> None:
    assert next_offset(offset, page_len, total) == expected


def test_page_envelope_paged() -> None:
    assert page_envelope("receipts", [1, 2], total=5, limit=2, offset=2) == {
        "receipts": [1, 2],
        "total": 5,
        "limit": 2,
        "offset": 2,
        "next_offset": 4,
    }


def test_page_envelope_unpaged_has_no_next_page() -> None:
    # No limit: the page runs to the end, whatever the arithmetic says.
    env = page_envelope("failures", [1, 2], total=5, limit=None, offset=1)
    assert env == {
        "failures": [1, 2],
        "total": 5,
        "limit": None,
        "offset": 1,
        "next_offset": None,
    }
