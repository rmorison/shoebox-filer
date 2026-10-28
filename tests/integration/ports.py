"""The devtest stack's ports, read from the environment with no fallback.

The integration suite rebuilds and truncates tables. A default port could
belong to another stack on the same host, so the suite never guesses one:
``POSTGRES_HOST_PORT``, ``GREENMAIL_SMTP_PORT`` and ``GREENMAIL_IMAP_PORT``
must all be set, and when any is missing the conftest skips every
integration test before a fixture runs. This module only reads and parses
the variables; importing it never connects.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

PORT_VARS = ("POSTGRES_HOST_PORT", "GREENMAIL_SMTP_PORT", "GREENMAIL_IMAP_PORT")

# Where setting the ports is described; the skip message points here.
SETUP_DOC = '"Integration tests" in docs/user/getting-started.md'

# Stands in for a missing port so the module-level names keep their types.
# The conftest skips before anything uses it. If the conftest is turned off
# (--noconftest), test bodies can run, so the value must not be a port any
# client would use: smtplib reads 0 as port 25, while socket, libpq and
# IMAPClient all refuse -1 before connecting.
UNSET_PORT = -1


def parse_port(value: str | None) -> int | None:
    """The port in ``value``, or None if it is unset, empty or not a port."""
    if value is None or not value.strip().isdecimal():
        return None
    port = int(value)
    return port if 0 < port < 65536 else None


def missing_ports(env: Mapping[str, str] | None = None) -> list[str]:
    """The names in ``PORT_VARS`` that don't hold a usable port."""
    env = os.environ if env is None else env
    return [var for var in PORT_VARS if parse_port(env.get(var)) is None]


def skip_reason(missing: list[str]) -> str:
    return (
        f"{', '.join(missing)} not set to a port: set POSTGRES_HOST_PORT, "
        "GREENMAIL_SMTP_PORT and GREENMAIL_IMAP_PORT to your devtest "
        f"stack's ports (there is no default); see {SETUP_DOC}"
    )


def _port(var: str) -> int:
    return parse_port(os.environ.get(var)) or UNSET_PORT


# Read once, when the conftest imports this module and before any test module
# is imported. Importing the CLI runs load_dotenv(), which can add a .env's
# variables to the environment later; the gate and the ports below must agree.
MISSING = missing_ports()
POSTGRES_HOST_PORT = _port("POSTGRES_HOST_PORT")
GREENMAIL_SMTP_PORT = _port("GREENMAIL_SMTP_PORT")
GREENMAIL_IMAP_PORT = _port("GREENMAIL_IMAP_PORT")


def dsn(role: str, password: str, database: str = "receipt_index_dev") -> str:
    """A DSN for ``role`` on the devtest Postgres."""
    return f"postgresql://{role}:{password}@localhost:{POSTGRES_HOST_PORT}/{database}"
