"""Where the MCP server's access log lives, and what ``status`` reads from it.

The server (``mcp_server``) appends one JSON line per file fetch and per
statement read it withholds. ``status`` counts the withheld ones, so the
operator sees them without reading the log. This module imports nothing
from MCP, so ``status`` doesn't load the SDK.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

ACCESS_LOG_ENV = "RECEIPT_INDEX_MCP_ACCESS_LOG"
WITHHELD = "withheld"


def default_access_log_path() -> Path:
    """Return ``$XDG_STATE_HOME/receipt-index/mcp-access.jsonl``.

    ``XDG_STATE_HOME`` defaults to ``~/.local/state``.
    """
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "receipt-index" / "mcp-access.jsonl"


def configured_access_log_path() -> Path:
    """The log ``receipt-index mcp`` writes without ``--access-log``."""
    configured = os.environ.get(ACCESS_LOG_ENV)
    return Path(configured) if configured else default_access_log_path()


@dataclass(frozen=True)
class Withheld:
    """Statement reads the MCP server refused (``withheld``), from its log."""

    count: int
    latest_at: str | None


def count_withheld(path: Path) -> Withheld | None:
    """Count the log's ``withheld`` entries; None when there is no log to read.

    A line that isn't a JSON object is skipped: the count is what the log
    shows, not a check of it.
    """
    count = 0
    latest: str | None = None
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or entry.get("outcome") != WITHHELD:
                    continue
                count += 1
                ts = entry.get("ts")
                if isinstance(ts, str) and (latest is None or ts > latest):
                    latest = ts
    except OSError:
        return None
    return Withheld(count=count, latest_at=latest)
