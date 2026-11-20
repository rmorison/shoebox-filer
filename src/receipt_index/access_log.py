"""The access log: where it lives, how it is written, what ``status`` reads.

The MCP server (``mcp_server``) appends one JSON line per file fetch and per
statement read it withholds. ``status`` counts the withheld ones, so the
operator sees them without reading the log. This module imports nothing
from MCP, so ``status`` doesn't load the SDK.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

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


# ---------------------------------------------------------------------------
# Writing the log
# ---------------------------------------------------------------------------


class AccessLogError(Exception):
    """The access log can't be written."""


class AccessLog:
    """Append-only JSON-lines log of file fetches and withheld statement reads."""

    def __init__(self, path: Path, *, echo: logging.Logger | None = None) -> None:
        self.path = path
        self._lock = threading.Lock()
        # The logger each entry is echoed to at INFO; the MCP server passes
        # its own, so its stderr shows the line under its name.
        self._echo = echo or logger

    def check_writable(self) -> None:
        """Create the log's directory and open it for append, or raise."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8"):
                pass
        except OSError as exc:
            msg = f"access log {self.path} is not writable: {exc}"
            raise AccessLogError(msg) from exc

    def record(self, entry: dict[str, Any]) -> None:
        """Append one entry and echo it to the application log."""
        line = json.dumps(entry, sort_keys=True, default=str)
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except OSError as exc:
            msg = f"access log {self.path} is not writable: {exc}"
            raise AccessLogError(msg) from exc
        self._echo.info("access: %s", line)


def withheld_entry(
    *,
    tool: str,
    statement_id: str,
    revision: int,
    account_ordinal: int | None,
    fields: list[str],
    client: dict[str, str | None],
) -> dict[str, Any]:
    """One withheld read's log line: IDs and field names, never the text."""
    return {
        "ts": datetime.now(UTC).isoformat(),
        "tool": tool,
        "statement_id": statement_id,
        "revision": revision,
        "account_ordinal": account_ordinal,
        "fields": fields,
        "outcome": WITHHELD,
        "client": client,
    }
