"""The CLI for refunds, credits and statements (data-model design, §6).

``search --kind``, the kind and signed amount in the output, ``edit --kind``,
``reextract --refund-candidates`` and the ``refund`` commands.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest

from receipt_index.models import Receipt, ReceiptEvent
from receipt_index.refunds import RefundError

from .test_cli_order_payment import _ROW, _invoke, config_file  # noqa: F401

if TYPE_CHECKING:
    from pathlib import Path

_PURCHASE = Receipt.model_validate(
    {**_ROW, "refunded": Decimal("10.00"), "net_paid": Decimal("20.00")}
)
_REFUND = Receipt.model_validate(
    {
        **_ROW,
        "id": UUID("019572a0-0000-7000-8000-000000000002"),
        "kind": "refund",
        "paid_amount": Decimal("10.00"),
        "refund_of": _ROW["id"],
    }
)


def _event(event_type: str = "refund_linked") -> ReceiptEvent:
    return ReceiptEvent(
        id=uuid4(),
        receipt_id=_REFUND.id,
        event_type=event_type,  # type: ignore[arg-type]
        payload={"purchase_id": str(_PURCHASE.id)},
        actor="operator",
        actor_kind="person",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
    )


class TestSearch:
    @patch("receipt_index.repository.search_receipts", return_value=[_PURCHASE])
    @patch("receipt_index.db.get_connection")
    def test_kind_is_repeatable(
        self,
        mock_conn: MagicMock,
        mock_search: MagicMock,
        config_file: Path,  # noqa: F811
    ) -> None:
        result = _invoke(
            config_file, "search", "--kind", "purchase", "--kind", "credit"
        )
        assert result.exit_code == 0, result.output
        assert mock_search.call_args.kwargs["kinds"] == ["purchase", "credit"]

    @patch("receipt_index.repository.search_receipts", return_value=[])
    @patch("receipt_index.db.get_connection")
    def test_every_kind_by_default(
        self,
        mock_conn: MagicMock,
        mock_search: MagicMock,
        config_file: Path,  # noqa: F811
    ) -> None:
        result = _invoke(config_file, "search")
        assert result.exit_code == 0, result.output
        assert mock_search.call_args.kwargs["kinds"] is None

    @patch("receipt_index.repository.search_receipts")
    @patch("receipt_index.db.get_connection")
    def test_unknown_kind_refused(
        self,
        mock_conn: MagicMock,
        mock_search: MagicMock,
        config_file: Path,  # noqa: F811
    ) -> None:
        result = _invoke(config_file, "search", "--kind", "other")
        assert result.exit_code == 2
        mock_search.assert_not_called()

    @patch(
        "receipt_index.repository.search_receipts", return_value=[_PURCHASE, _REFUND]
    )
    @patch("receipt_index.db.get_connection")
    def test_json_has_kind_signed_amount_and_net(
        self,
        mock_conn: MagicMock,
        mock_search: MagicMock,
        config_file: Path,  # noqa: F811
    ) -> None:
        result = _invoke(config_file, "search", "--output", "json")
        assert result.exit_code == 0, result.output
        purchase, refund = json.loads(result.output)["receipts"]
        assert (purchase["kind"], purchase["signed_amount"]) == ("purchase", "30.00")
        assert (purchase["refunded"], purchase["net_paid"]) == ("10.00", "20.00")
        assert (refund["kind"], refund["signed_amount"]) == ("refund", "-10.00")
        assert refund["refund_of"] == str(_PURCHASE.id)

    @patch(
        "receipt_index.repository.search_receipts", return_value=[_PURCHASE, _REFUND]
    )
    @patch("receipt_index.db.get_connection")
    def test_text_has_a_kind_column(
        self,
        mock_conn: MagicMock,
        mock_search: MagicMock,
        config_file: Path,  # noqa: F811
    ) -> None:
        result = _invoke(config_file, "search")
        assert result.exit_code == 0, result.output
        header, _, purchase, refund = result.output.splitlines()[:4]
        assert "Kind" in header
        assert " purchase " in purchase
        assert " refund " in refund


class TestShow:
    @pytest.mark.parametrize(
        ("receipt", "lines"),
        [
            (
                _PURCHASE,
                ["Kind:         purchase", "Net paid:     20.00 (refunded 10.00)"],
            ),
            (
                _REFUND,
                [
                    "Kind:         refund (counts -10.00)",
                    f"Refund of:    {_PURCHASE.id}",
                ],
            ),
        ],
    )
    @patch("receipt_index.repository.get_receipt_by_id")
    @patch("receipt_index.db.get_connection")
    def test_text(
        self,
        mock_conn: MagicMock,
        mock_get: MagicMock,
        config_file: Path,  # noqa: F811
        receipt: Receipt,
        lines: list[str],
    ) -> None:
        mock_get.return_value = receipt
        result = _invoke(config_file, "show", str(receipt.id))
        assert result.exit_code == 0, result.output
        for line in lines:
            assert line in result.output


class TestEditKind:
    @patch("receipt_index.edits.edit_receipt")
    @patch("receipt_index.db.get_connection")
    def test_passes_kind(
        self,
        mock_conn: MagicMock,
        mock_edit: MagicMock,
        config_file: Path,  # noqa: F811
    ) -> None:
        mock_edit.return_value = MagicMock(changes=[], events=[], receipt=_REFUND)
        result = _invoke(config_file, "edit", str(_REFUND.id), "--kind", "refund")
        assert result.exit_code == 0, result.output
        assert mock_edit.call_args.args[2] == {"kind": "refund"}

    def test_unknown_kind_refused(self, config_file: Path) -> None:  # noqa: F811
        result = _invoke(config_file, "edit", str(_REFUND.id), "--kind", "other")
        assert result.exit_code == 2


class TestReextractCandidates:
    @patch("receipt_index.repository.token_averages", return_value=None)
    @patch("receipt_index.repository.select_for_reextract", return_value=[])
    @patch("receipt_index.db.get_connection")
    def test_is_a_selector(
        self,
        mock_conn: MagicMock,
        mock_select: MagicMock,
        mock_averages: MagicMock,
        config_file: Path,  # noqa: F811
    ) -> None:
        result = _invoke(config_file, "reextract", "--refund-candidates", "--dry-run")
        assert result.exit_code == 0, result.output
        assert mock_select.call_args.kwargs["refund_candidates"] is True

    @patch("receipt_index.repository.token_averages", return_value=None)
    @patch("receipt_index.repository.select_for_reextract", return_value=[])
    @patch("receipt_index.db.get_connection")
    def test_off_by_default(
        self,
        mock_conn: MagicMock,
        mock_select: MagicMock,
        mock_averages: MagicMock,
        config_file: Path,  # noqa: F811
    ) -> None:
        result = _invoke(config_file, "reextract", "--all", "--dry-run")
        assert result.exit_code == 0, result.output
        assert mock_select.call_args.kwargs["refund_candidates"] is False


class TestRefundCommands:
    @patch("receipt_index.refunds.link", return_value=_event())
    @patch("receipt_index.db.get_connection")
    def test_link(
        self,
        mock_conn: MagicMock,
        mock_link: MagicMock,
        config_file: Path,  # noqa: F811
    ) -> None:
        result = _invoke(
            config_file,
            "refund",
            "link",
            str(_REFUND.id),
            str(_PURCHASE.id),
            "--reason",
            "same order",
            "--as",
            "operator",
        )
        assert result.exit_code == 0, result.output
        args, kwargs = mock_link.call_args
        assert args[1:] == (_REFUND.id, _PURCHASE.id)
        assert kwargs == {"actor": "operator", "reason": "same order"}
        assert "Linked" in result.output

    @patch(
        "receipt_index.refunds.link",
        side_effect=RefundError("currencies differ: USD, CAD"),
    )
    @patch("receipt_index.db.get_connection")
    def test_link_refused(
        self,
        mock_conn: MagicMock,
        mock_link: MagicMock,
        config_file: Path,  # noqa: F811
    ) -> None:
        result = _invoke(
            config_file, "refund", "link", str(_REFUND.id), str(_PURCHASE.id)
        )
        assert result.exit_code == 1
        assert "currencies differ" in result.output

    @patch("receipt_index.refunds.unlink", return_value=_event("refund_unlinked"))
    @patch("receipt_index.db.get_connection")
    def test_unlink(
        self,
        mock_conn: MagicMock,
        mock_unlink: MagicMock,
        config_file: Path,  # noqa: F811
    ) -> None:
        result = _invoke(config_file, "refund", "unlink", str(_REFUND.id))
        assert result.exit_code == 0, result.output
        assert mock_unlink.call_args.args[1] == _REFUND.id
        assert "Unlinked" in result.output

    @pytest.mark.parametrize("flag", [[], ["--unlinked"]])
    @patch("receipt_index.repository.list_refunds", return_value=[_REFUND])
    @patch("receipt_index.db.get_connection")
    def test_list(
        self,
        mock_conn: MagicMock,
        mock_list: MagicMock,
        config_file: Path,  # noqa: F811
        flag: list[str],
    ) -> None:
        result = _invoke(config_file, "refund", "list", *flag, "--output", "json")
        assert result.exit_code == 0, result.output
        assert mock_list.call_args.kwargs == {"unlinked": bool(flag)}
        (row,) = json.loads(result.output)
        assert row["refund_of"] == str(_PURCHASE.id)

    @patch("receipt_index.repository.list_refunds", return_value=[_REFUND])
    @patch("receipt_index.db.get_connection")
    def test_list_text(
        self,
        mock_conn: MagicMock,
        mock_list: MagicMock,
        config_file: Path,  # noqa: F811
    ) -> None:
        result = _invoke(config_file, "refund", "list")
        assert result.exit_code == 0, result.output
        assert str(_PURCHASE.id) in result.output


class TestDeadlock:
    """Two writers locking a refund and a purchase in opposite orders: Postgres
    aborts one. The CLI says so in one line, not a traceback."""

    @pytest.mark.parametrize(
        ("target", "args"),
        [
            (
                "receipt_index.refunds.link",
                ["refund", "link", str(_REFUND.id), str(_PURCHASE.id)],
            ),
            ("receipt_index.refunds.unlink", ["refund", "unlink", str(_REFUND.id)]),
            (
                "receipt_index.edits.edit_receipt",
                ["edit", str(_REFUND.id), "--kind", "refund"],
            ),
        ],
    )
    @patch("receipt_index.db.get_connection")
    def test_a_deadlock_is_one_line(
        self,
        mock_conn: MagicMock,
        config_file: Path,  # noqa: F811
        target: str,
        args: list[str],
    ) -> None:
        from psycopg import errors

        with patch(target, side_effect=errors.DeadlockDetected("deadlock detected")):
            result = _invoke(config_file, *args)
        assert result.exit_code == 1
        assert "concurrent change" in result.output
        assert "nothing was saved" in result.output
        assert "Traceback" not in result.output
