"""The page envelope shared by the CLI's JSON and the MCP server.

``search --output json`` and ``failures --output json`` return the same
``{<items>, total, limit, offset, next_offset}`` shape as the MCP
``search_receipts`` and ``list_failures`` tools. This module holds the parts
both use. It imports nothing from MCP, so the CLI doesn't load it.
"""

from __future__ import annotations

from typing import Any


def next_offset(offset: int, page_len: int, total: int) -> int | None:
    """Offset of the next page, or ``None`` when this page is the last."""
    end = offset + page_len
    return end if page_len and end < total else None


def page_envelope(
    key: str, items: list[Any], *, total: int, limit: int | None, offset: int
) -> dict[str, Any]:
    """One page under ``key``, with its place among ``total`` matches.

    ``limit`` is ``None`` for an unpaged result, which runs to the end, so
    there is no next page.
    """
    return {
        key: items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "next_offset": (
            None if limit is None else next_offset(offset, len(items), total)
        ),
    }
