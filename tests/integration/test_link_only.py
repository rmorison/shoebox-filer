"""Link-only receipts, end to end: GreenMail, the pipeline and Postgres.

The acceptance case: an email whose body is only a link to a hosted receipt
is not silently skipped. It is listed by ``failures --status link-only`` with
its link, and ``retry`` can requeue it. The extraction agent is mocked.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from receipt_index.models import FieldConfidence, ReceiptMetadata
from receipt_index.repository import (
    RetryFilter,
    count_ingest_failures,
    get_ingest_failures,
    select_retry_candidates,
)
from receipt_index.requeue import apply_requeue
from receipt_index.retries import RetryPolicy
from receipt_index.store import LocalEvidenceStore

from .conftest import seed_email
from .test_retry import _config, _ingest, _statuses

if TYPE_CHECKING:
    from pathlib import Path

    import psycopg

pytestmark = pytest.mark.integration

_LINK = "https://receipts.example.com/r/abc123"


def _low_confidence(link: str | None) -> Any:
    usage = MagicMock(input_tokens=100, output_tokens=50, cache_read_tokens=0)
    usage.requests = 1
    return MagicMock(
        output=ReceiptMetadata(
            vendor="Acme Parking (U.S.), LLC",
            amount=Decimal("0"),
            date=date(2026, 10, 1),
            confidence=0.2,
            field_confidence=FieldConfidence(
                vendor=0.6, amount=0.0, paid_amount=0.0, date=0.4
            ),
            receipt_link=link,
        ),
        usage=usage,
    )


def _seed(user: str, subject: str, body: str) -> str:
    message_id = f"<{uuid.uuid4().hex}@example.com>"
    seed_email(
        to_addr=user,
        from_addr="billing@example.com",
        subject=subject,
        text_body=body,
        message_id=message_id,
    )
    return message_id


def test_a_link_only_email_is_listed_and_can_be_requeued(
    greenmail_available: bool,
    pg_conn: psycopg.Connection[dict[str, Any]],
    tmp_path: Path,
) -> None:
    user = f"link-{uuid.uuid4().hex[:8]}@localhost"
    link_only = _seed(user, "Your receipt", _LINK)
    newsletter = _seed(user, "Newsletter", "Our autumn sale starts now.")
    config = _config(user)
    store = LocalEvidenceStore(tmp_path)
    policy = RetryPolicy()

    agent = MagicMock()
    agent.run_sync.side_effect = [_low_confidence(_LINK), _low_confidence(None)]
    first, _ = _ingest(pg_conn, config, store, agent, policy)
    assert first.skipped == 2
    # The link reached the model in the prompt.
    assert _LINK in agent.run_sync.call_args_list[0].args[0]

    # Listed for review with its link; the plain skip is not.
    listed = get_ingest_failures(pg_conn, status="link-only")
    pg_conn.commit()
    assert [e.source_id for e in listed] == [link_only]
    assert listed[0].status == "skipped"
    assert listed[0].receipt_link == _LINK
    assert listed[0].error_message == (
        "Skipped: link-only receipt (receipts.example.com)"
    )
    assert count_ingest_failures(pg_conn, status="link-only") == 1
    skipped = get_ingest_failures(pg_conn, status="skipped")
    pg_conn.commit()
    assert {e.source_id for e in skipped} == {link_only, newsletter}
    # Nothing was rendered or indexed for it.
    assert list(tmp_path.rglob("*.pdf")) == []

    # `retry --status link-only` selects only it, and requeues it.
    spec = RetryFilter(status="link-only")
    candidates = select_retry_candidates(pg_conn, spec)
    assert [c.source_id for c in candidates] == [link_only]
    outcome = apply_requeue(pg_conn, spec, candidates)
    assert outcome.requeued == [link_only]

    # Until link-following exists, the requeued item is a link-only skip again.
    agent = MagicMock()
    agent.run_sync.return_value = _low_confidence(_LINK)
    _, retried = _ingest(pg_conn, config, store, agent, policy)
    assert retried.skipped == 1
    assert _statuses(pg_conn, link_only) == [
        "started",
        "skipped",
        "queued",
        "started",
        "skipped",
    ]
    assert [e.source_id for e in get_ingest_failures(pg_conn, status="link-only")] == [
        link_only
    ]
    pg_conn.commit()
