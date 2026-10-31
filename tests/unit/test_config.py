"""Tests for receipt_index.config — YAML-based configuration system."""

from __future__ import annotations

import textwrap
from pathlib import Path  # noqa: TC003

import pytest
from pydantic import ValidationError

from receipt_index.config import (
    AppConfig,
    ConfigError,
    ImapSourceConfig,
    StoreConfig,
    load_config,
    load_config_dict,
)


class TestImapSourceConfig:
    """Tests for ImapSourceConfig Pydantic model."""

    def test_valid_config(self) -> None:
        config = ImapSourceConfig(
            name="personal-email",
            host="mail.example.com",
            username="user@example.com",
            password="secret",  # pragma: allowlist secret
        )
        assert config.host == "mail.example.com"
        assert config.port == 993
        assert config.folder == "INBOX"
        assert config.use_ssl is True

    def test_custom_port_and_folder(self) -> None:
        config = ImapSourceConfig(
            name="work-email",
            host="mail.example.com",
            username="user@example.com",
            password="secret",  # pragma: allowlist secret
            port=143,
            folder="Receipts",
            use_ssl=False,
        )
        assert config.port == 143
        assert config.folder == "Receipts"
        assert config.use_ssl is False

    def test_name_validation_rejects_uppercase(self) -> None:
        with pytest.raises(Exception):  # noqa: B017
            ImapSourceConfig(
                name="Personal-Email",
                host="mail.example.com",
                username="user@example.com",
                password="secret",  # pragma: allowlist secret
            )

    def test_name_validation_rejects_spaces(self) -> None:
        with pytest.raises(Exception):  # noqa: B017
            ImapSourceConfig(
                name="my email",
                host="mail.example.com",
                username="user@example.com",
                password="secret",  # pragma: allowlist secret
            )


class TestEnvVarInterpolation:
    """Tests for ${VAR} interpolation in YAML config."""

    def test_interpolates_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_PASSWORD", "s3cret")
        yaml_content = textwrap.dedent("""\
            sources:
              - name: test
                type: imap
                host: mail.example.com
                username: user@example.com
                password: ${TEST_PASSWORD}
                folder: INBOX
            database:
              url: postgresql://localhost/test
            llm:
              api_key: test-key
        """)
        result = load_config_dict(yaml_content)
        assert result["sources"][0]["password"] == "s3cret"  # pragma: allowlist secret

    def test_missing_env_var_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MISSING_VAR", raising=False)
        yaml_content = textwrap.dedent("""\
            sources: []
            database:
              url: ${MISSING_VAR}
            llm:
              api_key: test-key
        """)
        with pytest.raises(ConfigError, match="MISSING_VAR"):
            load_config_dict(yaml_content)

    def test_collects_all_missing_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VAR_A", raising=False)
        monkeypatch.delenv("VAR_B", raising=False)
        yaml_content = textwrap.dedent("""\
            sources: []
            database:
              url: ${VAR_A}
            llm:
              api_key: ${VAR_B}
        """)
        with pytest.raises(ConfigError, match="VAR_A") as exc_info:
            load_config_dict(yaml_content)
        assert "VAR_B" in str(exc_info.value)


class TestLoadConfig:
    """Tests for load_config() file discovery and validation."""

    def test_load_valid_config(self, tmp_path: Path) -> None:
        config_file = tmp_path / "receipt-index.yaml"
        config_file.write_text(
            textwrap.dedent("""\
                sources:
                  - name: test-email
                    type: imap
                    host: mail.example.com
                    username: user@example.com
                    password: secret
                    folder: INBOX
                database:
                  url: postgresql://localhost/test
                llm:
                  api_key: test-key
            """)
        )
        config = load_config(str(config_file))
        assert isinstance(config, AppConfig)
        assert len(config.sources) == 1
        assert isinstance(config.sources[0], ImapSourceConfig)
        assert config.sources[0].name == "test-email"
        assert config.database.url == "postgresql://localhost/test"

    def test_missing_file_raises(self) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load_config("/nonexistent/path/config.yaml")

    def test_invalid_yaml_raises(self, tmp_path: Path) -> None:
        config_file = tmp_path / "bad.yaml"
        config_file.write_text("not: a: valid: yaml: [")
        with pytest.raises(ConfigError):
            load_config(str(config_file))

    def test_validation_error_gives_details(self, tmp_path: Path) -> None:
        config_file = tmp_path / "incomplete.yaml"
        config_file.write_text(
            textwrap.dedent("""\
                sources: []
            """)
        )
        with pytest.raises(ConfigError, match="validation failed"):
            load_config(str(config_file))

    def test_discovery_via_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "my-config.yaml"
        config_file.write_text(
            textwrap.dedent("""\
                sources:
                  - name: test-email
                    type: imap
                    host: mail.example.com
                    username: user@example.com
                    password: secret
                    folder: INBOX
                database:
                  url: postgresql://localhost/test
                llm:
                  api_key: test-key
            """)
        )
        monkeypatch.setenv("RECEIPT_INDEX_CONFIG", str(config_file))
        config = load_config()
        assert len(config.sources) == 1

    def test_no_config_found_shows_example(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("RECEIPT_INDEX_CONFIG", raising=False)
        monkeypatch.chdir(tmp_path)
        with pytest.raises(ConfigError, match="No configuration file found"):
            load_config()

    def test_defaults_for_optional_sections(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            textwrap.dedent("""\
                sources:
                  - name: test-email
                    type: imap
                    host: mail.example.com
                    username: user@example.com
                    password: secret
                    folder: INBOX
                database:
                  url: postgresql://localhost/test
                llm:
                  api_key: test-key
            """)
        )
        config = load_config(str(config_file))
        assert config.store.path == "./data/receipts"
        assert config.store.backend == "local"
        assert config.logging.level == "INFO"
        assert config.llm.model == "claude-haiku-5-5"


class TestIngestConfig:
    def test_defaults_when_block_absent(self) -> None:
        from receipt_index.config import AppConfig

        cfg = AppConfig.model_validate(
            {
                "sources": [],
                "database": {"url": "postgresql://localhost/x"},
                "llm": {"api_key": "k"},  # pragma: allowlist secret
            }
        )
        assert cfg.ingest.drive_settle_seconds == 60

    def test_explicit_value(self) -> None:
        from receipt_index.config import IngestConfig

        assert IngestConfig(drive_settle_seconds=0).drive_settle_seconds == 0

    def test_negative_rejected(self) -> None:
        import pydantic

        from receipt_index.config import IngestConfig

        with pytest.raises(pydantic.ValidationError):
            IngestConfig(drive_settle_seconds=-1)


class TestStoreBackend:
    """store.backend selects the evidence store; only local exists so far."""

    def test_local_is_accepted(self) -> None:
        assert StoreConfig(backend="local").backend == "local"

    @pytest.mark.parametrize("backend", ["drive", "Local", "s3", ""])
    def test_anything_else_is_rejected(self, backend: str) -> None:
        with pytest.raises(ValidationError, match="backend"):
            StoreConfig.model_validate({"backend": backend})


class TestServeKeys:
    """The ``serve`` keys of the ``ingest:`` block."""

    def test_defaults(self) -> None:
        from receipt_index.config import IngestConfig

        config = IngestConfig()
        assert config.idle_reissue_seconds == 600
        assert config.imap_poll_seconds == 240
        assert config.drive_poll_seconds == 120
        assert config.item_timeout_seconds == 1200

    @pytest.mark.parametrize(
        ("key", "floor"),
        [
            ("idle_reissue_seconds", 60),
            ("imap_poll_seconds", 30),
            ("drive_poll_seconds", 30),
            ("item_timeout_seconds", 1200),
        ],
    )
    def test_floors(self, key: str, floor: int) -> None:
        from pydantic import ValidationError

        from receipt_index.config import IngestConfig

        assert getattr(IngestConfig(**{key: floor}), key) == floor
        with pytest.raises(ValidationError, match=key):
            IngestConfig(**{key: floor - 1})

    def test_hourly_polling_is_allowed(self) -> None:
        from receipt_index.config import IngestConfig

        config = IngestConfig(imap_poll_seconds=3600, drive_poll_seconds=3600)
        assert (config.imap_poll_seconds, config.drive_poll_seconds) == (3600, 3600)


_PING_MARKER = "CAPABILITY-MARKER-7f3a"


def _health_config(health: dict[str, object], **ingest: object) -> dict[str, object]:
    return {
        "sources": [
            {
                "name": "mail",
                "type": "imap",
                "host": "mail.example.com",
                "username": "user@example.com",
                "password": "secret",  # pragma: allowlist secret
            }
        ],
        "database": {"url": "postgresql://localhost/test"},
        "llm": {"api_key": "test-key"},  # pragma: allowlist secret
        "ingest": {**ingest, "health": health},
    }


class TestHealthKeys:
    def test_defaults(self) -> None:
        health = AppConfig.model_validate(_health_config({})).ingest.health
        assert health.heartbeat_stale_seconds == 120
        assert health.source_stale_seconds is None
        assert health.terminal_alert_seconds == 86400
        assert health.ping_url is None
        assert health.ping_interval_seconds == 300

    def test_ping_url_is_kept_secret(self) -> None:
        url = f"https://hc-ping.example.com/{_PING_MARKER}/"
        config = AppConfig.model_validate(_health_config({"ping_url": url}))
        assert config.ingest.health.ping_url is not None
        assert config.ingest.health.ping_url.get_secret_value() == url.rstrip("/")
        assert _PING_MARKER not in repr(config)
        assert _PING_MARKER not in str(config.model_dump())

    @pytest.mark.parametrize(
        "url",
        [
            f"ftp://hc-ping.example.com/{_PING_MARKER}",
            f"https://hc-ping.example.com/{_PING_MARKER}?x=1",
            f"https://hc-ping.example.com/{_PING_MARKER}\n/x",
            f"https://hc-ping.example.com/{_PING_MARKER} x",
            "http://",
            f"https:///{_PING_MARKER}",
            12345,
        ],
    )
    def test_a_bad_ping_url_is_refused_without_its_value(self, url: object) -> None:
        with pytest.raises(ValidationError) as excinfo:
            AppConfig.model_validate(_health_config({"ping_url": url}))
        assert "ping_url" in str(excinfo.value)
        assert _PING_MARKER not in str(excinfo.value)
        assert "12345" not in str(excinfo.value)

    def test_another_ingest_error_hides_the_ping_url(self, tmp_path: Path) -> None:
        # A parent model's error would print the whole ingest dict.
        path = tmp_path / "config.yaml"
        path.write_text(
            textwrap.dedent(f"""\
                sources:
                  - name: mail
                    type: imap
                    host: mail.example.com
                    username: user@example.com
                    password: secret
                database:
                  url: postgresql://localhost/test
                llm:
                  api_key: test-key
                ingest:
                  retry_max_attempts: 9
                  health:
                    ping_url: https://hc-ping.example.com/{_PING_MARKER}
            """)
        )
        with pytest.raises(ConfigError) as excinfo:
            load_config(str(path))
        assert "retry_backoff_seconds" in str(excinfo.value)
        assert _PING_MARKER not in str(excinfo.value)

    def test_source_stale_below_a_poll_interval_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="imap_poll_seconds"):
            AppConfig.model_validate(_health_config({"source_stale_seconds": 200}))

    def test_source_stale_needs_two_poll_intervals(self) -> None:
        # One interval flaps: a healthy source's age just before its next
        # read is a whole interval plus the read.
        with pytest.raises(ValidationError, match="twice"):
            AppConfig.model_validate(_health_config({"source_stale_seconds": 300}))
        config = AppConfig.model_validate(_health_config({"source_stale_seconds": 480}))
        assert config.ingest.health.source_stale_seconds == 480

    def test_source_stale_checks_only_the_source_types_present(self) -> None:
        # 240 is below twice the IMAP poll (480), but the only source is
        # Drive (twice 120).
        data = _health_config({"source_stale_seconds": 240})
        data["sources"] = [
            {"name": "scans", "type": "gdrive", "folder_id": "f", "token_json": "{}"}
        ]
        config = AppConfig.model_validate(data)
        assert config.ingest.health.source_stale_seconds == 240

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("heartbeat_stale_seconds", 59),
            ("source_stale_seconds", 59),
            ("terminal_alert_seconds", -1),
            ("ping_interval_seconds", 29),
        ],
    )
    def test_floors(self, key: str, value: int) -> None:
        with pytest.raises(ValidationError, match=key):
            AppConfig.model_validate(_health_config({key: value}))
