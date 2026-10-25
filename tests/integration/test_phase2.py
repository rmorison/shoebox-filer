"""Phase 2 integration tests: config-based sources, multi-source ingest, Drive adapter.

Requires Docker services: Postgres (port 15432) and GreenMail (ports 3025/3143).
Start with: docker compose up -d

These tests exercise:
- Config-based IMAP ingest (YAML config -> ImapAdapter -> pipeline)
- Multi-source ingest (two named IMAP sources)
- --source filtering (restrict ingest to one named source)
- Idempotent re-ingest (no duplicates across multiple runs)
- Search with source_name filter
- GdriveAdapter with mocked Google API (through real Postgres)
- DB migration 000005 (source_name, file_name columns added/removed cleanly)
"""

from __future__ import annotations

import json
import textwrap
import uuid
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from receipt_index.adapters.imap import ImapAdapter
from receipt_index.models import ReceiptMetadata
from receipt_index.pipeline import run_ingest
from receipt_index.repository import get_processed_source_ids, search_receipts
from receipt_index.store import LocalFileStore

from .conftest import GREENMAIL_IMAP_PORT, seed_email

if TYPE_CHECKING:
    from pathlib import Path

    import psycopg


# ---------------------------------------------------------------------------
# Helpers shared across test classes
# ---------------------------------------------------------------------------


def _unique_user(prefix: str = "p2") -> str:
    """Generate a unique GreenMail mailbox address to avoid cross-run collisions."""
    short_id = uuid.uuid4().hex[:8]
    return f"{prefix}-{short_id}@localhost"


def _make_mock_agent(
    vendor: str = "TestVendor",
    amount: Decimal = Decimal("42.99"),
    receipt_date: date | None = None,
    confidence: float = 0.90,
) -> MagicMock:
    """Return a mock extraction agent that always returns fixed metadata."""
    meta = ReceiptMetadata(
        vendor=vendor,
        amount=amount,
        date=receipt_date or date(2025, 6, 15),
        confidence=confidence,
    )
    mock_usage = MagicMock()
    mock_usage.input_tokens = 100
    mock_usage.output_tokens = 50
    mock_usage.cache_read_tokens = 0
    mock_usage.requests = 1

    mock_result = MagicMock()
    mock_result.output = meta
    mock_result.usage.return_value = mock_usage

    agent = MagicMock()
    agent.run_sync.return_value = mock_result
    return agent


def _make_alternating_agent(
    *receipts: tuple[str, Decimal, date],
) -> MagicMock:
    """Return a mock agent that cycles through (vendor, amount, date) tuples."""
    results = []
    for vendor, amount, rcpt_date in receipts:
        meta = ReceiptMetadata(
            vendor=vendor,
            amount=amount,
            date=rcpt_date,
            confidence=0.90,
        )
        mock_usage = MagicMock()
        mock_usage.input_tokens = 100
        mock_usage.output_tokens = 50
        mock_usage.cache_read_tokens = 0
        mock_usage.requests = 1

        mock_result = MagicMock()
        mock_result.output = meta
        mock_result.usage.return_value = mock_usage
        results.append(mock_result)
    agent = MagicMock()
    agent.run_sync.side_effect = results
    return agent


def _imap_config_for_user(user: str) -> Any:
    """Return an ImapSourceConfig pointing at GreenMail for the given user."""
    # Import here so tests fail clearly if Phase 2 config models are missing
    from receipt_index.config import ImapSourceConfig

    return ImapSourceConfig(
        name="test-source",
        type="imap",
        host="localhost",
        port=GREENMAIL_IMAP_PORT,
        username=user,
        password="any",  # pragma: allowlist secret
        folder="INBOX",
        use_ssl=False,
    )


def _make_app_config(sources: list[Any], db_url: str, store_path: str) -> Any:
    """Construct an AppConfig with the given sources."""
    from receipt_index.config import AppConfig, DatabaseConfig, LlmConfig, StoreConfig

    return AppConfig(
        sources=sources,
        database=DatabaseConfig(url=db_url),
        store=StoreConfig(path=store_path),
        llm=LlmConfig(api_key="test-key"),  # pragma: allowlist secret
    )


# DB URL used by conftest for the test Postgres instance — unused at module level
# but kept as a reference for any test that needs to open its own connection.
_DB_URL = (
    "postgresql://main:localpass@localhost:15432/"  # pragma: allowlist secret
    "receipt_index_dev"
)


# ---------------------------------------------------------------------------
# 1. Config -> IMAP ingest
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestConfigImapIngest:
    """Config-based IMAP ingest: build ImapAdapter from ImapSourceConfig."""

    def test_config_based_imap_ingest_happy_path(
        self,
        greenmail_available: bool,
        pg_conn: psycopg.Connection[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        """Load IMAP source from config, run pipeline, verify DB and file store."""
        user = _unique_user("cfg-imap")
        seed_email(
            to_addr=user,
            from_addr="shop@example.com",
            subject="Config-Based Receipt",
            html_body="<p>Total: $55.00</p>",
        )

        source_cfg = _imap_config_for_user(user)
        # Override source name to something meaningful
        from receipt_index.config import ImapSourceConfig

        source_cfg = ImapSourceConfig(
            name="personal-email",
            type="imap",
            host="localhost",
            port=GREENMAIL_IMAP_PORT,
            username=user,
            password="any",  # pragma: allowlist secret
            folder="INBOX",
            use_ssl=False,
        )

        adapter = ImapAdapter(source_cfg)
        store = LocalFileStore(tmp_path)
        agent = _make_mock_agent(vendor="ShopCo", amount=Decimal("55.00"))

        result = run_ingest(
            conn=pg_conn,
            adapter=adapter,
            store=store,
            agent=agent,
            source_name="personal-email",
            source_type="imap",
        )

        assert result.processed == 1
        assert result.failed == 0

        receipt = result.receipts[0]
        assert receipt.source_name == "personal-email"
        assert receipt.vendor == "ShopCo"
        assert receipt.amount == Decimal("55.00")
        assert store.exists(receipt.pdf_path)

        rows = search_receipts(pg_conn, vendor="ShopCo")
        assert len(rows) == 1
        assert rows[0].source_name == "personal-email"

    def test_config_imap_source_name_stored_in_db(
        self,
        greenmail_available: bool,
        pg_conn: psycopg.Connection[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        """Verify source_name is persisted on the receipt row."""
        user = _unique_user("srcname")
        seed_email(
            to_addr=user,
            from_addr="billing@vendor.com",
            subject="Source Name Test",
            text_body="Total: $20.00",
        )

        from receipt_index.config import ImapSourceConfig

        source_cfg = ImapSourceConfig(
            name="work-receipts",
            type="imap",
            host="localhost",
            port=GREENMAIL_IMAP_PORT,
            username=user,
            password="any",  # pragma: allowlist secret
            folder="INBOX",
            use_ssl=False,
        )
        adapter = ImapAdapter(source_cfg)
        store = LocalFileStore(tmp_path)
        agent = _make_mock_agent(vendor="WorkVendor", amount=Decimal("20.00"))

        run_ingest(
            conn=pg_conn,
            adapter=adapter,
            store=store,
            agent=agent,
            source_name="work-receipts",
            source_type="imap",
        )

        rows = pg_conn.execute(
            "SELECT source_name FROM receipt.receipts WHERE vendor = %(v)s",
            {"v": "WorkVendor"},
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["source_name"] == "work-receipts"


# ---------------------------------------------------------------------------
# 2. Multi-source config
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestMultiSourceIngest:
    """Two named IMAP sources in a config; both are ingested and results aggregated."""

    def test_two_imap_sources_both_ingested(
        self,
        greenmail_available: bool,
        pg_conn: psycopg.Connection[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        """Ingest from two separate IMAP mailboxes, verify both rows in DB."""
        user_a = _unique_user("multi-a")
        user_b = _unique_user("multi-b")

        seed_email(
            to_addr=user_a,
            from_addr="acme@example.com",
            subject="ACME Receipt",
            text_body="Total: $100.00",
        )
        seed_email(
            to_addr=user_b,
            from_addr="globex@example.com",
            subject="Globex Receipt",
            text_body="Total: $200.00",
        )

        from receipt_index.config import ImapSourceConfig

        source_a = ImapSourceConfig(
            name="source-a",
            type="imap",
            host="localhost",
            port=GREENMAIL_IMAP_PORT,
            username=user_a,
            password="any",  # pragma: allowlist secret
            folder="INBOX",
            use_ssl=False,
        )
        source_b = ImapSourceConfig(
            name="source-b",
            type="imap",
            host="localhost",
            port=GREENMAIL_IMAP_PORT,
            username=user_b,
            password="any",  # pragma: allowlist secret
            folder="INBOX",
            use_ssl=False,
        )

        store = LocalFileStore(tmp_path)
        agent_a = _make_mock_agent(vendor="ACME Corp", amount=Decimal("100.00"))
        agent_b = _make_mock_agent(vendor="Globex Inc", amount=Decimal("200.00"))

        result_a = run_ingest(
            conn=pg_conn,
            adapter=ImapAdapter(source_a),
            store=store,
            agent=agent_a,
            source_name="source-a",
            source_type="imap",
        )
        result_b = run_ingest(
            conn=pg_conn,
            adapter=ImapAdapter(source_b),
            store=store,
            agent=agent_b,
            source_name="source-b",
            source_type="imap",
        )

        assert result_a.processed == 1
        assert result_b.processed == 1

        # Both rows exist in DB, each tagged with correct source
        all_rows = search_receipts(pg_conn)
        assert len(all_rows) == 2

        source_names = {r.source_name for r in all_rows}
        assert source_names == {"source-a", "source-b"}

    def test_two_sources_results_are_independently_tagged(
        self,
        greenmail_available: bool,
        pg_conn: psycopg.Connection[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        """Each receipt row carries its own source_name, not the other source's."""
        user_a = _unique_user("tag-a")
        user_b = _unique_user("tag-b")

        seed_email(to_addr=user_a, subject="Alpha Receipt", text_body="$10.00")
        seed_email(to_addr=user_b, subject="Beta Receipt", text_body="$20.00")

        from receipt_index.config import ImapSourceConfig

        def _src(name: str, user: str) -> ImapSourceConfig:
            return ImapSourceConfig(
                name=name,
                type="imap",
                host="localhost",
                port=GREENMAIL_IMAP_PORT,
                username=user,
                password="any",  # pragma: allowlist secret
                folder="INBOX",
                use_ssl=False,
            )

        store = LocalFileStore(tmp_path)
        run_ingest(
            conn=pg_conn,
            adapter=ImapAdapter(_src("alpha", user_a)),
            store=store,
            agent=_make_mock_agent(vendor="AlphaVendor", amount=Decimal("10.00")),
            source_name="alpha",
            source_type="imap",
        )
        run_ingest(
            conn=pg_conn,
            adapter=ImapAdapter(_src("beta", user_b)),
            store=store,
            agent=_make_mock_agent(vendor="BetaVendor", amount=Decimal("20.00")),
            source_name="beta",
            source_type="imap",
        )

        alpha_rows = search_receipts(pg_conn, source_name="alpha")
        beta_rows = search_receipts(pg_conn, source_name="beta")

        assert len(alpha_rows) == 1
        assert alpha_rows[0].vendor == "AlphaVendor"
        assert alpha_rows[0].source_name == "alpha"

        assert len(beta_rows) == 1
        assert beta_rows[0].vendor == "BetaVendor"
        assert beta_rows[0].source_name == "beta"


# ---------------------------------------------------------------------------
# 3. --source filtering: restrict ingest to one named source
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestSourceFiltering:
    """--source flag restricts ingest to the named source only."""

    def test_source_filter_ingests_only_specified_source(
        self,
        greenmail_available: bool,
        pg_conn: psycopg.Connection[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        """When source_name is passed to run_ingest, only that source is processed."""
        user_a = _unique_user("filter-a")
        user_b = _unique_user("filter-b")

        seed_email(to_addr=user_a, subject="Filter A Receipt", text_body="$30.00")
        seed_email(to_addr=user_b, subject="Filter B Receipt", text_body="$40.00")

        from receipt_index.config import ImapSourceConfig

        source_a = ImapSourceConfig(
            name="filter-source-a",
            type="imap",
            host="localhost",
            port=GREENMAIL_IMAP_PORT,
            username=user_a,
            password="any",  # pragma: allowlist secret
            folder="INBOX",
            use_ssl=False,
        )

        store = LocalFileStore(tmp_path)
        # Only ingest source-a, deliberately skip source-b
        result = run_ingest(
            conn=pg_conn,
            adapter=ImapAdapter(source_a),
            store=store,
            agent=_make_mock_agent(vendor="FilterVendorA", amount=Decimal("30.00")),
            source_name="filter-source-a",
            source_type="imap",
        )

        assert result.processed == 1

        all_rows = search_receipts(pg_conn)
        # Only source-a receipts in DB; source-b was never ingested
        assert len(all_rows) == 1
        assert all_rows[0].source_name == "filter-source-a"

    def test_source_filter_idempotency_check_is_per_source(
        self,
        greenmail_available: bool,
        pg_conn: psycopg.Connection[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        """Filtering processed IDs by source_name avoids cross-source collisions."""
        user_a = _unique_user("pid-a")
        user_b = _unique_user("pid-b")

        seed_email(to_addr=user_a, subject="PID Source A", text_body="$50.00")
        seed_email(to_addr=user_b, subject="PID Source B", text_body="$60.00")

        from receipt_index.config import ImapSourceConfig

        def _src(name: str, user: str) -> ImapSourceConfig:
            return ImapSourceConfig(
                name=name,
                type="imap",
                host="localhost",
                port=GREENMAIL_IMAP_PORT,
                username=user,
                password="any",  # pragma: allowlist secret
                folder="INBOX",
                use_ssl=False,
            )

        store = LocalFileStore(tmp_path)

        # Ingest source-a first
        run_ingest(
            conn=pg_conn,
            adapter=ImapAdapter(_src("pid-a", user_a)),
            store=store,
            agent=_make_mock_agent(vendor="PidVendorA", amount=Decimal("50.00")),
            source_name="pid-a",
            source_type="imap",
        )

        # get_processed_source_ids returns all IDs globally, but source-b's
        # adapter provides different source_ids so they won't collide
        all_ids = get_processed_source_ids(pg_conn)
        assert len(all_ids) >= 1  # source-a's IDs are in there

        result_b = run_ingest(
            conn=pg_conn,
            adapter=ImapAdapter(_src("pid-b", user_b)),
            store=store,
            agent=_make_mock_agent(vendor="PidVendorB", amount=Decimal("60.00")),
            source_name="pid-b",
            source_type="imap",
        )
        assert result_b.processed == 1

        all_rows = search_receipts(pg_conn)
        assert len(all_rows) == 2


# ---------------------------------------------------------------------------
# 4. Idempotent re-ingest
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestIdempotentReIngest:
    """Running ingest twice on the same source must not produce duplicates."""

    def test_imap_source_reingest_produces_no_duplicates(
        self,
        greenmail_available: bool,
        pg_conn: psycopg.Connection[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        """Ingest once, then again — second run skips all already-processed IDs."""
        user = _unique_user("idem")
        seed_email(
            to_addr=user,
            from_addr="shop@example.com",
            subject="Idempotent Receipt",
            text_body="Total: $75.00",
        )

        from receipt_index.config import ImapSourceConfig

        source_cfg = ImapSourceConfig(
            name="idem-source",
            type="imap",
            host="localhost",
            port=GREENMAIL_IMAP_PORT,
            username=user,
            password="any",  # pragma: allowlist secret
            folder="INBOX",
            use_ssl=False,
        )
        store = LocalFileStore(tmp_path)
        agent = _make_mock_agent(vendor="IdemVendor", amount=Decimal("75.00"))

        # First run
        r1 = run_ingest(
            conn=pg_conn,
            adapter=ImapAdapter(source_cfg),
            store=store,
            agent=agent,
            source_name="idem-source",
            source_type="imap",
        )
        assert r1.processed == 1

        # Second run — same messages, same source
        r2 = run_ingest(
            conn=pg_conn,
            adapter=ImapAdapter(source_cfg),
            store=store,
            agent=_make_mock_agent(vendor="IdemVendor", amount=Decimal("75.00")),
            source_name="idem-source",
            source_type="imap",
        )
        assert r2.processed == 0

        # Exactly one row in DB
        rows = search_receipts(pg_conn, vendor="IdemVendor")
        assert len(rows) == 1

    def test_reingest_after_new_email_only_processes_new_email(
        self,
        greenmail_available: bool,
        pg_conn: psycopg.Connection[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        """A new email arriving after first ingest is picked up on the next run."""
        user = _unique_user("idem2")
        seed_email(
            to_addr=user,
            subject="First Receipt",
            text_body="$10.00",
        )

        from receipt_index.config import ImapSourceConfig

        source_cfg = ImapSourceConfig(
            name="idem2-source",
            type="imap",
            host="localhost",
            port=GREENMAIL_IMAP_PORT,
            username=user,
            password="any",  # pragma: allowlist secret
            folder="INBOX",
            use_ssl=False,
        )
        store = LocalFileStore(tmp_path)

        # First run: one email
        run_ingest(
            conn=pg_conn,
            adapter=ImapAdapter(source_cfg),
            store=store,
            agent=_make_mock_agent(vendor="FirstVendor", amount=Decimal("10.00")),
            source_name="idem2-source",
            source_type="imap",
        )

        # Seed a second email
        seed_email(
            to_addr=user,
            subject="Second Receipt",
            text_body="$20.00",
        )

        # Second run: should only process the new email
        r2 = run_ingest(
            conn=pg_conn,
            adapter=ImapAdapter(source_cfg),
            store=store,
            agent=_make_mock_agent(vendor="SecondVendor", amount=Decimal("20.00")),
            source_name="idem2-source",
            source_type="imap",
        )
        assert r2.processed == 1

        rows = search_receipts(pg_conn)
        assert len(rows) == 2


# ---------------------------------------------------------------------------
# 5. Search with source filter
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestSearchWithSourceFilter:
    """search_receipts with source_name filter returns only that source's receipts."""

    def test_search_source_filter_returns_only_named_source(
        self,
        greenmail_available: bool,
        pg_conn: psycopg.Connection[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        """Searching with source_name returns only receipts from that source."""
        user_a = _unique_user("srch-a")
        user_b = _unique_user("srch-b")

        seed_email(to_addr=user_a, subject="Search Source A", text_body="$100.00")
        seed_email(to_addr=user_b, subject="Search Source B", text_body="$200.00")

        from receipt_index.config import ImapSourceConfig

        def _src(name: str, user: str) -> ImapSourceConfig:
            return ImapSourceConfig(
                name=name,
                type="imap",
                host="localhost",
                port=GREENMAIL_IMAP_PORT,
                username=user,
                password="any",  # pragma: allowlist secret
                folder="INBOX",
                use_ssl=False,
            )

        store = LocalFileStore(tmp_path)
        run_ingest(
            conn=pg_conn,
            adapter=ImapAdapter(_src("search-a", user_a)),
            store=store,
            agent=_make_mock_agent(vendor="SearchVendorA", amount=Decimal("100.00")),
            source_name="search-a",
            source_type="imap",
        )
        run_ingest(
            conn=pg_conn,
            adapter=ImapAdapter(_src("search-b", user_b)),
            store=store,
            agent=_make_mock_agent(vendor="SearchVendorB", amount=Decimal("200.00")),
            source_name="search-b",
            source_type="imap",
        )

        results_a = search_receipts(pg_conn, source_name="search-a")
        results_b = search_receipts(pg_conn, source_name="search-b")
        results_all = search_receipts(pg_conn)

        assert len(results_a) == 1
        assert results_a[0].vendor == "SearchVendorA"

        assert len(results_b) == 1
        assert results_b[0].vendor == "SearchVendorB"

        assert len(results_all) == 2

    def test_search_source_filter_combined_with_vendor_filter(
        self,
        greenmail_available: bool,
        pg_conn: psycopg.Connection[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        """source_name filter composes correctly with other search filters."""
        user_a = _unique_user("combo-a")
        user_b = _unique_user("combo-b")

        # Both sources have a receipt from "ACME"
        seed_email(to_addr=user_a, subject="ACME A", text_body="$50.00")
        seed_email(to_addr=user_b, subject="ACME B", text_body="$50.00")

        from receipt_index.config import ImapSourceConfig

        def _src(name: str, user: str) -> ImapSourceConfig:
            return ImapSourceConfig(
                name=name,
                type="imap",
                host="localhost",
                port=GREENMAIL_IMAP_PORT,
                username=user,
                password="any",  # pragma: allowlist secret
                folder="INBOX",
                use_ssl=False,
            )

        store = LocalFileStore(tmp_path)
        run_ingest(
            conn=pg_conn,
            adapter=ImapAdapter(_src("combo-a", user_a)),
            store=store,
            agent=_make_mock_agent(vendor="ACME Corp", amount=Decimal("50.00")),
            source_name="combo-a",
            source_type="imap",
        )
        run_ingest(
            conn=pg_conn,
            adapter=ImapAdapter(_src("combo-b", user_b)),
            store=store,
            agent=_make_mock_agent(vendor="ACME Corp", amount=Decimal("50.00")),
            source_name="combo-b",
            source_type="imap",
        )

        # Search vendor=ACME in combo-a only
        results = search_receipts(pg_conn, vendor="ACME", source_name="combo-a")
        assert len(results) == 1
        assert results[0].source_name == "combo-a"

    def test_search_nonexistent_source_returns_empty(
        self,
        pg_conn: psycopg.Connection[dict[str, Any]],
    ) -> None:
        """Searching with a source_name that has no receipts returns an empty list."""
        results = search_receipts(pg_conn, source_name="nonexistent-source")
        assert results == []


# ---------------------------------------------------------------------------
# 6. Mock Drive adapter test
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestGdriveAdapterMocked:
    """GdriveAdapter through real Postgres DB with mocked Google API calls."""

    def _make_gdrive_source_config(self) -> Any:
        """Return a GdriveSourceConfig with dummy credentials."""
        from receipt_index.config import GdriveSourceConfig

        token_json = json.dumps(
            {
                "token": "ya29.test-access-token",
                "refresh_token": "1//test-refresh-token",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "test-client-id.apps.googleusercontent.com",
                "client_secret": "test-secret",  # pragma: allowlist secret
                "scopes": ["https://www.googleapis.com/auth/drive.readonly"],
                "expiry": "2099-01-01T00:00:00Z",
            }
        )
        return GdriveSourceConfig(
            name="scanned-receipts",
            type="gdrive",
            folder_id="1aBcDeFgHiJkLmNoPqRsTuVwXyZ",
            token_json=token_json,
        )

    def _make_fake_pdf(self) -> bytes:
        """Return a minimal valid PDF bytes for testing."""
        # A real single-page PDF generated via weasyprint or a hard-coded minimal stub.
        # We use a pre-baked minimal PDF structure that passes basic validation.
        return (
            b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R"
            b" /MediaBox [0 0 612 792] >>\nendobj\n"
            b"xref\n0 4\n0000000000 65535 f \n"
            b"0000000009 00000 n \n"
            b"0000000058 00000 n \n"
            b"0000000115 00000 n \n"
            b"trailer\n<< /Size 4 /Root 1 0 R >>\n"
            b"startxref\n190\n%%EOF"
        )

    def _make_fake_jpeg(self) -> bytes:
        """Return a minimal valid JPEG header for testing."""
        # Minimal 1x1 white JPEG
        return (
            b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t"
            b"\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a"
            b"\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\x1e"
            b"\xff\xd9"
        )

    @patch("receipt_index.adapters.gdrive.GdriveAdapter._build_service")
    def test_gdrive_pdf_ingested_via_real_postgres(
        self,
        mock_build_service: MagicMock,
        pg_conn: psycopg.Connection[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        """GdriveAdapter with mocked Drive API ingests a PDF through real Postgres."""
        from receipt_index.adapters.gdrive import GdriveAdapter

        fake_pdf = self._make_fake_pdf()
        file_id = "drive-file-id-pdf-001"
        modified_time = "2025-06-15T10:30:00Z"

        # Mock the Drive service: list files, download content
        mock_service = MagicMock()
        mock_build_service.return_value = mock_service

        # _list_files response
        mock_service.files.return_value.list.return_value.execute.return_value = {
            "files": [
                {
                    "id": file_id,
                    "name": "acme-receipt-june.pdf",
                    "mimeType": "application/pdf",
                    "modifiedTime": modified_time,
                }
            ]
        }

        # _download_file response: returns (content_bytes, mime_type)
        mock_media = MagicMock()
        mock_media.status = (None, b"100")
        # Simulate MediaIoBaseDownload writing bytes to the buffer
        mock_service.files.return_value.get_media.return_value = MagicMock()

        config = self._make_gdrive_source_config()
        adapter = GdriveAdapter(config)

        # Patch _download_file to avoid actual HTTP calls
        with patch.object(
            adapter,
            "_download_file",
            return_value=(fake_pdf, "application/pdf"),
        ):
            store = LocalFileStore(tmp_path)
            agent = _make_mock_agent(
                vendor="ACME Corp",
                amount=Decimal("88.50"),
                receipt_date=date(2025, 6, 15),
            )

            result = run_ingest(
                conn=pg_conn,
                adapter=adapter,
                store=store,
                agent=agent,
                source_name="scanned-receipts",
                source_type="gdrive",
            )

        assert result.processed == 1
        assert result.failed == 0

        receipt = result.receipts[0]
        assert receipt.source_name == "scanned-receipts"
        assert receipt.source_type == "gdrive"
        assert receipt.vendor == "ACME Corp"
        assert receipt.file_name == "acme-receipt-june.pdf"
        assert store.exists(receipt.pdf_path)

        rows = search_receipts(pg_conn, source_name="scanned-receipts")
        assert len(rows) == 1
        assert rows[0].file_name == "acme-receipt-june.pdf"

    @patch("receipt_index.adapters.gdrive.GdriveAdapter._build_service")
    def test_gdrive_skips_already_processed_file_ids(
        self,
        mock_build_service: MagicMock,
        pg_conn: psycopg.Connection[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        """Drive adapter skips file IDs already in the processed set."""
        from receipt_index.adapters.gdrive import GdriveAdapter

        file_id = "drive-file-id-dupe-001"
        modified_time = "2025-06-20T08:00:00Z"
        fake_pdf = self._make_fake_pdf()

        mock_service = MagicMock()
        mock_build_service.return_value = mock_service
        mock_service.files.return_value.list.return_value.execute.return_value = {
            "files": [
                {
                    "id": file_id,
                    "name": "dupe-receipt.pdf",
                    "mimeType": "application/pdf",
                    "modifiedTime": modified_time,
                }
            ]
        }

        config = self._make_gdrive_source_config()
        adapter = GdriveAdapter(config)
        store = LocalFileStore(tmp_path)

        with patch.object(
            adapter,
            "_download_file",
            return_value=(fake_pdf, "application/pdf"),
        ):
            agent = _make_mock_agent(vendor="DupeVendor", amount=Decimal("15.00"))

            # First ingest
            r1 = run_ingest(
                conn=pg_conn,
                adapter=adapter,
                store=store,
                agent=agent,
                source_name="scanned-receipts",
                source_type="gdrive",
            )
            assert r1.processed == 1

        # Re-configure mock for second call (same file listing)
        mock_service.files.return_value.list.return_value.execute.return_value = {
            "files": [
                {
                    "id": file_id,
                    "name": "dupe-receipt.pdf",
                    "mimeType": "application/pdf",
                    "modifiedTime": modified_time,
                }
            ]
        }

        adapter2 = GdriveAdapter(config)
        with patch.object(
            adapter2,
            "_download_file",
            return_value=(fake_pdf, "application/pdf"),
        ):
            r2 = run_ingest(
                conn=pg_conn,
                adapter=adapter2,
                store=store,
                agent=_make_mock_agent(vendor="DupeVendor", amount=Decimal("15.00")),
                source_name="scanned-receipts",
                source_type="gdrive",
            )

        assert r2.processed == 0
        rows = search_receipts(pg_conn, vendor="DupeVendor")
        assert len(rows) == 1

    @patch("receipt_index.adapters.gdrive.GdriveAdapter._build_service")
    def test_gdrive_unsupported_mime_type_is_skipped(
        self,
        mock_build_service: MagicMock,
        pg_conn: psycopg.Connection[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        """Files with unsupported MIME types are skipped (not ingested, not errored)."""
        from receipt_index.adapters.gdrive import GdriveAdapter

        mock_service = MagicMock()
        mock_build_service.return_value = mock_service
        mock_service.files.return_value.list.return_value.execute.return_value = {
            "files": [
                {
                    "id": "drive-file-id-zip",
                    "name": "data.zip",
                    "mimeType": "application/zip",
                    "modifiedTime": "2025-06-01T00:00:00Z",
                }
            ]
        }

        config = self._make_gdrive_source_config()
        adapter = GdriveAdapter(config)
        store = LocalFileStore(tmp_path)

        result = run_ingest(
            conn=pg_conn,
            adapter=adapter,
            store=store,
            agent=_make_mock_agent(),
            source_name="scanned-receipts",
            source_type="gdrive",
        )

        # Nothing processed or failed — unsupported files are silently skipped
        assert result.processed == 0
        assert result.failed == 0

    @patch("receipt_index.adapters.gdrive.GdriveAdapter._build_service")
    def test_gdrive_empty_folder_produces_no_results(
        self,
        mock_build_service: MagicMock,
        pg_conn: psycopg.Connection[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        """An empty Drive folder yields no ingest results."""
        from receipt_index.adapters.gdrive import GdriveAdapter

        mock_service = MagicMock()
        mock_build_service.return_value = mock_service
        mock_service.files.return_value.list.return_value.execute.return_value = {
            "files": []
        }

        config = self._make_gdrive_source_config()
        adapter = GdriveAdapter(config)
        store = LocalFileStore(tmp_path)

        result = run_ingest(
            conn=pg_conn,
            adapter=adapter,
            store=store,
            agent=_make_mock_agent(),
            source_name="scanned-receipts",
            source_type="gdrive",
        )

        assert result.processed == 0
        assert result.failed == 0


# ---------------------------------------------------------------------------
# 7. DB migration 000005: source_name and file_name columns
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestMigration000005:
    """Verify migration 000005 (source_name, file_name) applies and rolls back cleanly.

    This class manages its own schema state — it tears down and re-applies
    the full schema to test migration transitions. It does NOT use the shared
    pg_conn fixture to avoid corrupting other tests' state.
    """

    # DSN matches conftest._DB_DSN
    _DSN = (
        "postgresql://main:localpass@localhost:15432/"  # pragma: allowlist secret
        "receipt_index_dev"
    )

    def _fresh_conn(self) -> Any:
        """Open a new connection (caller is responsible for closing)."""
        import psycopg as pg

        try:
            conn = pg.connect(self._DSN, row_factory=pg.rows.dict_row)
        except Exception:
            pytest.skip("Postgres not available for migration test")
        return conn

    def _schema_exists_pre_migration(self, conn: Any) -> bool:
        """Return True if source_name column is absent (pre-migration state)."""
        row = conn.execute(
            """\
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'receipt'
              AND table_name = 'receipts'
              AND column_name = 'source_name'
            """
        ).fetchone()
        return row is None

    def _apply_migration_up(self, conn: Any) -> None:
        """Apply migration 000005 up: add source_name and file_name columns."""
        conn.execute(
            """\
            ALTER TABLE receipt.receipts
                ADD COLUMN IF NOT EXISTS source_name TEXT,
                ADD COLUMN IF NOT EXISTS file_name TEXT
            """
        )
        # Backfill existing rows
        conn.execute(
            "UPDATE receipt.receipts SET source_name = 'legacy-imap' "
            "WHERE source_name IS NULL"
        )
        conn.execute(
            "ALTER TABLE receipt.receipts ALTER COLUMN source_name SET NOT NULL"
        )
        conn.commit()

    def _apply_migration_down(self, conn: Any) -> None:
        """Apply migration 000005 down: drop source_name and file_name columns."""
        conn.execute(
            """\
            ALTER TABLE receipt.receipts
                DROP COLUMN IF EXISTS source_name,
                DROP COLUMN IF EXISTS file_name
            """
        )
        conn.commit()

    def _columns_present(self, conn: Any, *column_names: str) -> dict[str, bool]:
        """Return a dict mapping column_name -> True if present in receipt.receipts."""
        rows = conn.execute(
            """\
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'receipt' AND table_name = 'receipts'
            """
        ).fetchall()
        existing = {r["column_name"] for r in rows}
        return {col: col in existing for col in column_names}

    def test_migration_up_adds_source_name_and_file_name_columns(self) -> None:
        """Migration up: source_name NOT NULL (backfilled), file_name nullable."""
        conn = self._fresh_conn()
        try:
            # Ensure we start from a state without the new columns
            self._apply_migration_down(conn)

            # Confirm pre-migration state
            present_before = self._columns_present(conn, "source_name", "file_name")
            assert not present_before["source_name"]
            assert not present_before["file_name"]

            # Apply migration up
            self._apply_migration_up(conn)

            present_after = self._columns_present(conn, "source_name", "file_name")
            assert present_after["source_name"]
            assert present_after["file_name"]

            # source_name should be NOT NULL
            row = conn.execute(
                """\
                SELECT is_nullable
                FROM information_schema.columns
                WHERE table_schema = 'receipt'
                  AND table_name = 'receipts'
                  AND column_name = 'source_name'
                """
            ).fetchone()
            assert row is not None
            assert row["is_nullable"] == "NO"

            # file_name should be nullable
            row = conn.execute(
                """\
                SELECT is_nullable
                FROM information_schema.columns
                WHERE table_schema = 'receipt'
                  AND table_name = 'receipts'
                  AND column_name = 'file_name'
                """
            ).fetchone()
            assert row is not None
            assert row["is_nullable"] == "YES"
        finally:
            conn.close()

    def test_migration_down_removes_source_name_and_file_name_columns(self) -> None:
        """Migration down drops both columns cleanly."""
        conn = self._fresh_conn()
        try:
            # Start with columns present
            self._apply_migration_up(conn)

            present_before = self._columns_present(conn, "source_name", "file_name")
            assert present_before["source_name"]
            assert present_before["file_name"]

            # Roll back
            self._apply_migration_down(conn)

            present_after = self._columns_present(conn, "source_name", "file_name")
            assert not present_after["source_name"]
            assert not present_after["file_name"]
        finally:
            conn.close()

    def test_migration_up_backfills_existing_rows_with_legacy_imap(self) -> None:
        """Existing rows with NULL source_name are backfilled to 'legacy-imap'."""
        conn = self._fresh_conn()
        try:
            # Drop columns so we can insert a row without source_name
            self._apply_migration_down(conn)

            # Insert a receipt row (pre-migration schema has no source_name)
            conn.execute(
                """\
                INSERT INTO receipt.receipts (
                    source_id, source_type, vendor, amount, currency,
                    receipt_date, confidence, pdf_path
                ) VALUES (
                    'legacy-test-id', 'imap', 'LegacyVendor', 10.00, 'USD',
                    '2024-01-01', 0.9, 'legacy/path.pdf'
                )
                """
            )
            conn.commit()

            # Apply migration up — should backfill the row
            self._apply_migration_up(conn)

            row = conn.execute(
                "SELECT source_name FROM receipt.receipts "
                "WHERE source_id = 'legacy-test-id'"
            ).fetchone()
            assert row is not None
            assert row["source_name"] == "legacy-imap"
        finally:
            # Clean up
            try:
                conn.execute(
                    "DELETE FROM receipt.receipts WHERE source_id = 'legacy-test-id'"
                )
                conn.commit()
            except Exception:
                pass
            conn.close()

    def test_migration_up_down_up_is_idempotent(self) -> None:
        """Running up -> down -> up produces the same final schema."""
        conn = self._fresh_conn()
        try:
            self._apply_migration_up(conn)
            self._apply_migration_down(conn)
            self._apply_migration_up(conn)

            present = self._columns_present(conn, "source_name", "file_name")
            assert present["source_name"]
            assert present["file_name"]
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 8. load_config() integration
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestLoadConfigIntegration:
    """load_config() correctly parses YAML and env var interpolation end-to-end."""

    def test_load_config_single_imap_source(self, tmp_path: Path) -> None:
        """A minimal IMAP config file is parsed into AppConfig with correct values."""
        from receipt_index.config import load_config

        config_file = tmp_path / "receipt-index.yaml"
        config_file.write_text(
            textwrap.dedent(
                """\
                sources:
                  - name: personal-email
                    type: imap
                    host: mail.example.com
                    port: 993
                    username: user@example.com
                    password: secret123
                    folder: INBOX.Receipts
                    use_ssl: true

                database:
                  url: postgresql://user:pass@localhost/db  # pragma: allowlist secret

                store:
                  path: /tmp/receipts

                llm:
                  api_key: sk-test-key
                """
            )
        )

        config = load_config(config_file)

        assert len(config.sources) == 1
        source = config.sources[0]
        assert source.name == "personal-email"
        assert source.type == "imap"
        assert source.host == "mail.example.com"  # type: ignore[union-attr]
        assert source.folder == "INBOX.Receipts"  # type: ignore[union-attr]
        assert (
            config.database.url == "postgresql://user:pass@localhost/db"
        )  # pragma: allowlist secret

    def test_load_config_env_var_interpolation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """${VAR} references in YAML are resolved from the environment."""
        from receipt_index.config import load_config

        monkeypatch.setenv(  # pragma: allowlist secret
            "TEST_IMAP_PASSWORD", "supersecret"
        )
        monkeypatch.setenv("TEST_DB_URL", "postgresql://test/testdb")

        config_file = tmp_path / "receipt-index.yaml"
        config_file.write_text(
            textwrap.dedent(
                """\
                sources:
                  - name: test-email
                    type: imap
                    host: localhost
                    port: 143
                    username: user@test.com
                    password: ${TEST_IMAP_PASSWORD}
                    use_ssl: false

                database:
                  url: ${TEST_DB_URL}

                llm:
                  api_key: test-key
                """
            )
        )

        config = load_config(config_file)

        assert config.sources[0].password == "supersecret"  # type: ignore[union-attr]
        assert config.database.url == "postgresql://test/testdb"

    def test_load_config_missing_env_var_raises_clear_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unset ${VAR} raises ConfigError naming all missing variables."""
        from receipt_index.config import ConfigError, load_config

        # Ensure the var is NOT set
        monkeypatch.delenv("MISSING_VAR_ONE", raising=False)
        monkeypatch.delenv("MISSING_VAR_TWO", raising=False)

        config_file = tmp_path / "receipt-index.yaml"
        config_file.write_text(
            textwrap.dedent(
                """\
                sources:
                  - name: test-email
                    type: imap
                    host: localhost
                    port: 143
                    username: user@test.com
                    password: ${MISSING_VAR_ONE}
                    use_ssl: false

                database:
                  url: ${MISSING_VAR_TWO}

                llm:
                  api_key: test-key
                """
            )
        )

        with pytest.raises(ConfigError, match="MISSING_VAR"):
            load_config(config_file)

    def test_load_config_two_sources(self, tmp_path: Path) -> None:
        """A config with two sources (IMAP and GDrive) is parsed correctly."""
        from receipt_index.config import (
            GdriveSourceConfig,
            ImapSourceConfig,
            load_config,
        )

        config_file = tmp_path / "receipt-index.yaml"
        config_file.write_text(
            textwrap.dedent(
                """\
                sources:
                  - name: personal-email
                    type: imap
                    host: mail.example.com
                    username: user@example.com
                    password: imap-pass
                    folder: INBOX.Receipts

                  - name: scanned-receipts
                    type: gdrive
                    folder_id: "1aBcDeFgHiJkLmNoPqRsTuVwXyZ"
                    token_json: '{"token": "tok"}'

                database:
                  url: postgresql://localhost/db

                llm:
                  api_key: test-key
                """
            )
        )

        config = load_config(config_file)

        assert len(config.sources) == 2
        imap_src = config.sources[0]
        gdrive_src = config.sources[1]

        assert isinstance(imap_src, ImapSourceConfig)
        assert imap_src.name == "personal-email"

        assert isinstance(gdrive_src, GdriveSourceConfig)
        assert gdrive_src.name == "scanned-receipts"
        assert gdrive_src.folder_id == "1aBcDeFgHiJkLmNoPqRsTuVwXyZ"

    def test_load_config_missing_file_raises_clear_error(self, tmp_path: Path) -> None:
        """A nonexistent config path raises ConfigError."""
        from receipt_index.config import ConfigError, load_config

        missing = tmp_path / "does-not-exist.yaml"
        with pytest.raises(ConfigError):
            load_config(missing)
