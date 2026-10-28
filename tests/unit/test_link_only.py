"""Link-only receipts: the extraction field and its host helper."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from receipt_index.models import FieldConfidence, ReceiptMetadata, link_host

_FC = FieldConfidence(vendor=0.5, amount=0.1, paid_amount=0.1, date=0.5)


def _metadata(link: object) -> ReceiptMetadata:
    return ReceiptMetadata.model_validate(
        {
            "vendor": "Acme Parking (U.S.), LLC",
            "amount": Decimal("0"),
            "date": date(2025, 6, 15),
            "confidence": 0.2,
            "field_confidence": _FC,
            "receipt_link": link,
        }
    )


class TestReceiptLinkField:
    def test_defaults_to_none(self) -> None:
        metadata = ReceiptMetadata(
            vendor="Acme Parking (U.S.), LLC",
            amount=Decimal("1"),
            date=date(2025, 6, 15),
            confidence=0.9,
            field_confidence=_FC,
        )
        assert metadata.receipt_link is None

    @pytest.mark.parametrize(
        "url",
        [
            "https://receipts.example.com/r/abc123",
            "http://receipts.example.com/r/abc123?t=x",
            "HTTPS://Receipts.Example.com/r/1",
        ],
    )
    def test_keeps_http_links(self, url: str) -> None:
        assert _metadata(url).receipt_link == url

    def test_strips_whitespace(self) -> None:
        link = _metadata("  https://receipts.example.com/r/abc123\n").receipt_link
        assert link == "https://receipts.example.com/r/abc123"

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "   ",
            "receipt",
            "javascript:alert(1)",
            "ftp://receipts.example.com/r/1",
            "https://",
            "https://receipts.example.com/" + "a" * 2030,
            42,
        ],
        ids=[
            "empty",
            "blank",
            "bare word",
            "javascript",
            "ftp",
            "no host",
            "too long",
            "not a string",
        ],
    )
    def test_drops_anything_else(self, value: object) -> None:
        # A bad link must not fail the whole extraction: it becomes a plain skip.
        assert _metadata(value).receipt_link is None

    def test_2048_characters_is_kept(self) -> None:
        url = "https://receipts.example.com/"
        url += "a" * (2048 - len(url))
        assert _metadata(url).receipt_link == url


class TestLinkHost:
    def test_host_only(self) -> None:
        url = "https://receipts.example.com:8443/r/abc123?token=secret#x"
        assert link_host(url) == "receipts.example.com"

    def test_userinfo_is_dropped(self) -> None:
        url = "https://user:pw@receipts.example.com/r/1"  # pragma: allowlist secret
        assert link_host(url) == "receipts.example.com"

    def test_no_host(self) -> None:
        assert link_host("not a url") == "unknown host"


class TestLinkStaysOutOfTheHistory:
    def test_model_dump_leaves_the_link_out(self) -> None:
        # extractions.output is append-only: a token stored there can't be
        # removed, so the link lives only on the ingest_log row.
        dumped = _metadata("https://receipts.example.com/r/abc123").model_dump(
            mode="json"
        )
        assert "receipt_link" not in dumped

    def test_the_output_schema_still_asks_for_it(self) -> None:
        assert "receipt_link" in ReceiptMetadata.model_json_schema()["properties"]
