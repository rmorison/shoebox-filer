"""Tests for config file discovery, loading, and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from receipt_index.config import (
    AppConfig,
    ConfigError,
    ImapSourceConfig,
    _find_config_file,
    load_config,
    load_config_dict,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_MINIMAL_YAML = """\
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
  api_key: sk-test-key
"""

_YAML_WITH_ENV_VARS = """\
sources:
  - name: test-email
    type: imap
    host: mail.example.com
    username: user@example.com
    password: ${TEST_IMAP_PASSWORD}
    folder: INBOX

database:
  url: ${TEST_DATABASE_URL}

llm:
  api_key: ${TEST_ANTHROPIC_KEY}
"""


def _write_config(path: Path, content: str = _MINIMAL_YAML) -> Path:
    """Write config content to a file and return the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# load_config_dict tests
# ---------------------------------------------------------------------------


class TestLoadConfigDict:
    """Tests for load_config_dict (YAML parsing + env interpolation)."""

    def test_parse_minimal_yaml(self) -> None:
        result = load_config_dict(_MINIMAL_YAML)
        assert result["sources"][0]["name"] == "test-email"
        assert result["database"]["url"] == "postgresql://localhost/test"

    def test_env_var_interpolation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_IMAP_PASSWORD", "s3cret")
        monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://localhost/db")
        monkeypatch.setenv("TEST_ANTHROPIC_KEY", "sk-ant-xxx")

        result = load_config_dict(_YAML_WITH_ENV_VARS)

        assert result["sources"][0]["password"] == "s3cret"  # pragma: allowlist secret
        assert result["database"]["url"] == "postgresql://localhost/db"
        assert result["llm"]["api_key"] == "sk-ant-xxx"  # pragma: allowlist secret

    def test_missing_env_var_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEST_IMAP_PASSWORD", raising=False)
        monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
        monkeypatch.delenv("TEST_ANTHROPIC_KEY", raising=False)

        with pytest.raises(ConfigError, match="TEST_IMAP_PASSWORD"):
            load_config_dict(_YAML_WITH_ENV_VARS)

    def test_all_missing_env_vars_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """All missing env vars should be reported in a single error."""
        monkeypatch.delenv("TEST_IMAP_PASSWORD", raising=False)
        monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
        monkeypatch.delenv("TEST_ANTHROPIC_KEY", raising=False)

        with pytest.raises(ConfigError) as exc_info:
            load_config_dict(_YAML_WITH_ENV_VARS)

        msg = str(exc_info.value)
        assert "TEST_IMAP_PASSWORD" in msg
        assert "TEST_DATABASE_URL" in msg
        assert "TEST_ANTHROPIC_KEY" in msg

    def test_non_mapping_yaml_raises(self) -> None:
        with pytest.raises(ConfigError, match="YAML mapping"):
            load_config_dict("- item1\n- item2\n")

    def test_partial_env_interpolation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Env var in the middle of a string should be interpolated."""
        yaml_content = """\
sources:
  - name: test
    type: imap
    host: mail.example.com
    username: user@example.com
    password: prefix_${TEST_SUFFIX}_end
    folder: INBOX

database:
  url: postgresql://localhost/test

llm:
  api_key: key
"""
        monkeypatch.setenv("TEST_SUFFIX", "middle")
        result = load_config_dict(yaml_content)
        assert (
            result["sources"][0]["password"] == "prefix_middle_end"
        )  # pragma: allowlist secret


# ---------------------------------------------------------------------------
# _find_config_file tests
# ---------------------------------------------------------------------------


class TestFindConfigFile:
    """Tests for _find_config_file discovery logic."""

    def test_env_var_takes_precedence(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        env_config = _write_config(tmp_path / "env-config.yaml")
        _write_config(tmp_path / "receipt-index.yaml")
        monkeypatch.setenv("RECEIPT_INDEX_CONFIG", str(env_config))
        monkeypatch.chdir(tmp_path)

        result = _find_config_file()

        assert result == env_config

    def test_cwd_yaml_found(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path / "receipt-index.yaml")
        monkeypatch.delenv("RECEIPT_INDEX_CONFIG", raising=False)
        monkeypatch.chdir(tmp_path)

        result = _find_config_file()

        assert result is not None
        assert result.name == "receipt-index.yaml"

    def test_xdg_config_found(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        xdg_config = tmp_path / ".config" / "receipt-index" / "config.yaml"
        _write_config(xdg_config)
        monkeypatch.delenv("RECEIPT_INDEX_CONFIG", raising=False)
        # chdir to a directory without receipt-index.yaml
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        monkeypatch.chdir(empty_dir)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        result = _find_config_file()

        assert result == xdg_config

    def test_no_config_found_returns_none(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("RECEIPT_INDEX_CONFIG", raising=False)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        result = _find_config_file()

        assert result is None

    def test_env_var_points_to_missing_file_raises(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("RECEIPT_INDEX_CONFIG", str(tmp_path / "nonexistent.yaml"))

        with pytest.raises(ConfigError, match="does not exist"):
            _find_config_file()


# ---------------------------------------------------------------------------
# load_config tests
# ---------------------------------------------------------------------------


class TestLoadConfig:
    """Tests for load_config (end-to-end loading and validation)."""

    def test_load_with_explicit_path(self, tmp_path: Path) -> None:
        config_file = _write_config(tmp_path / "config.yaml")

        config = load_config(str(config_file))

        assert isinstance(config, AppConfig)
        assert len(config.sources) == 1
        source = config.sources[0]
        assert isinstance(source, ImapSourceConfig)
        assert source.name == "test-email"
        assert source.host == "mail.example.com"

    def test_load_explicit_path_not_found_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="Config file not found"):
            load_config(str(tmp_path / "nonexistent.yaml"))

    def test_load_discovers_cwd_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path / "receipt-index.yaml")
        monkeypatch.delenv("RECEIPT_INDEX_CONFIG", raising=False)
        monkeypatch.chdir(tmp_path)

        config = load_config()

        assert isinstance(config, AppConfig)

    def test_load_no_config_raises_helpful_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("RECEIPT_INDEX_CONFIG", raising=False)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        with pytest.raises(ConfigError) as exc_info:
            load_config()

        msg = str(exc_info.value)
        assert "No configuration file found" in msg
        assert "RECEIPT_INDEX_CONFIG" in msg
        assert "receipt-index.yaml" in msg
        assert "sources:" in msg  # example config is included

    def test_load_with_env_var_interpolation(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path / "config.yaml", _YAML_WITH_ENV_VARS)
        monkeypatch.setenv("TEST_IMAP_PASSWORD", "s3cret")
        monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://localhost/db")
        monkeypatch.setenv("TEST_ANTHROPIC_KEY", "sk-ant-xxx")

        config = load_config(str(tmp_path / "config.yaml"))

        source = config.sources[0]
        assert isinstance(source, ImapSourceConfig)
        assert source.password == "s3cret"  # pragma: allowlist secret
        assert config.database.url == "postgresql://localhost/db"
        assert config.llm.api_key == "sk-ant-xxx"  # pragma: allowlist secret

    def test_load_validation_error_raises_config_error(self, tmp_path: Path) -> None:
        """Invalid Pydantic data should raise ConfigError, not ValidationError."""
        bad_yaml = """\
sources:
  - name: test
    type: imap
    # missing required host, username, password

database:
  url: postgresql://localhost/test

llm:
  api_key: sk-test
"""
        config_file = _write_config(tmp_path / "config.yaml", bad_yaml)

        with pytest.raises(ConfigError, match="Config validation failed"):
            load_config(str(config_file))

    def test_load_defaults_applied(self, tmp_path: Path) -> None:
        config_file = _write_config(tmp_path / "config.yaml")

        config = load_config(str(config_file))

        assert config.store.path == "./data/receipts"
        assert config.logging.level == "INFO"
        assert config.llm.model == "claude-haiku-4-5-20251001"
        source = config.sources[0]
        assert isinstance(source, ImapSourceConfig)
        assert source.port == 993
        assert source.use_ssl is True

    def test_source_name_validation(self, tmp_path: Path) -> None:
        """Source names must match ^[a-z0-9][a-z0-9-]*$."""
        bad_name_yaml = """\
sources:
  - name: Invalid_Name!
    type: imap
    host: mail.example.com
    username: user@example.com
    password: secret
    folder: INBOX

database:
  url: postgresql://localhost/test

llm:
  api_key: sk-test
"""
        config_file = _write_config(tmp_path / "config.yaml", bad_name_yaml)

        with pytest.raises(ConfigError, match="Config validation failed"):
            load_config(str(config_file))

    def test_multiple_sources(self, tmp_path: Path) -> None:
        multi_yaml = """\
sources:
  - name: email-1
    type: imap
    host: mail1.example.com
    username: user1@example.com
    password: pass1
    folder: INBOX

  - name: email-2
    type: imap
    host: mail2.example.com
    username: user2@example.com
    password: pass2
    folder: Receipts

database:
  url: postgresql://localhost/test

llm:
  api_key: sk-test
"""
        config_file = _write_config(tmp_path / "config.yaml", multi_yaml)

        config = load_config(str(config_file))

        assert len(config.sources) == 2
        assert config.sources[0].name == "email-1"
        assert config.sources[1].name == "email-2"
