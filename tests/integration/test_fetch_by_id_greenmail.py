"""IMAP ``fetch_by_id`` against GreenMail: a real server's SEARCH and quoting."""

from __future__ import annotations

import uuid

import pytest

from receipt_index.adapters.imap import ImapAdapter
from receipt_index.config import ImapSourceConfig

from .conftest import GREENMAIL_IMAP_PORT, seed_email

pytestmark = pytest.mark.integration


def _adapter() -> tuple[ImapAdapter, str]:
    user = f"refetch-{uuid.uuid4().hex[:8]}@localhost"
    config = ImapSourceConfig(
        name="greenmail-refetch",
        host="localhost",
        username=user,
        password="any",  # pragma: allowlist secret
        port=GREENMAIL_IMAP_PORT,
        folder="INBOX",
        use_ssl=False,
    )
    return ImapAdapter(config), user


@pytest.mark.parametrize(
    "message_id",
    [
        "<plain-{}@example.com>",
        '<"quoted.local"-{}@example.com>',
        "<paren(1)-back\\slash-{}@example.com>",
    ],
)
def test_finds_one_message_by_its_id(
    greenmail_available: bool, message_id: str
) -> None:
    adapter, user = _adapter()
    wanted = message_id.format(uuid.uuid4().hex[:6])
    seed_email(
        to_addr=user, subject="Wanted", text_body="Total 12.00", message_id=wanted
    )
    seed_email(
        to_addr=user, subject="Other", text_body="x", message_id="<o@example.com>"
    )
    with adapter:
        raw = adapter.fetch_by_id(wanted)
    assert raw is not None
    assert raw.subject == "Wanted"
    assert raw.source_id == wanted


def test_shared_message_id_is_none(greenmail_available: bool) -> None:
    adapter, user = _adapter()
    shared = f"<dup-{uuid.uuid4().hex[:6]}@example.com>"
    seed_email(to_addr=user, subject="One", text_body="x", message_id=shared)
    seed_email(to_addr=user, subject="Two", text_body="y", message_id=shared)
    with adapter:
        assert adapter.fetch_by_id(shared) is None


def test_missing_and_substring_ids_are_none(greenmail_available: bool) -> None:
    adapter, user = _adapter()
    stored = f"<long-{uuid.uuid4().hex[:6]}@example.com>"
    seed_email(to_addr=user, subject="Long", text_body="x", message_id=stored)
    with adapter:
        assert adapter.fetch_by_id("<gone@example.com>") is None
        # A search for part of the stored ID finds it; the exact check refuses.
        assert adapter.fetch_by_id(stored[:-1]) is None
