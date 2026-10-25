"""YAML-based application configuration with env var interpolation.

Config file search order (first found wins):
1. Explicit path via ``config_path`` argument or ``--config`` CLI flag
2. ``RECEIPT_INDEX_CONFIG`` environment variable
3. ``./receipt-index.yaml`` (current working directory)
4. ``~/.config/receipt-index/config.yaml`` (XDG convention)
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, Discriminator, Field, Tag, ValidationError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

_ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")

_EXAMPLE_CONFIG = """\
# receipt-index.yaml
sources:
  - name: personal-email
    type: imap
    host: mail.example.com
    port: 993
    username: user@example.com
    password: ${IMAP_PASSWORD}
    folder: INBOX.Receipts
    use_ssl: true

database:
  url: ${DATABASE_URL}

store:
  path: ./data/receipts

llm:
  model: claude-haiku-4-5-20251001
  api_key: ${ANTHROPIC_API_KEY}

logging:
  level: INFO
"""


class ConfigError(Exception):
    """Raised when configuration loading fails."""


# ---------------------------------------------------------------------------
# Config models
# ---------------------------------------------------------------------------


class ImapSourceConfig(BaseModel):
    """IMAP email source configuration."""

    name: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    type: Literal["imap"] = "imap"
    host: str
    port: int = 993
    username: str
    password: str
    folder: str = "INBOX"
    use_ssl: bool = True


class GdriveSourceConfig(BaseModel):
    """Google Drive source configuration."""

    name: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    type: Literal["gdrive"] = "gdrive"
    folder_id: str
    token_json: str


def _source_discriminator(v: Any) -> str:
    """Discriminate source config union by the ``type`` field."""
    if isinstance(v, dict):
        return str(v.get("type", ""))
    return str(getattr(v, "type", ""))


SourceConfig = Annotated[
    Annotated[ImapSourceConfig, Tag("imap")]
    | Annotated[GdriveSourceConfig, Tag("gdrive")],
    Discriminator(_source_discriminator),
]


class DatabaseConfig(BaseModel):
    """Database connection configuration."""

    url: str


class StoreConfig(BaseModel):
    """File store configuration."""

    path: str = "./data/receipts"


class LlmConfig(BaseModel):
    """LLM provider configuration."""

    model: str = "claude-haiku-4-5-20251001"
    api_key: str


class LoggingConfig(BaseModel):
    """Logging configuration."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


class AppConfig(BaseModel):
    """Top-level application configuration."""

    sources: list[SourceConfig]
    database: DatabaseConfig
    store: StoreConfig = StoreConfig(path="./data/receipts")
    llm: LlmConfig
    logging: LoggingConfig = LoggingConfig()


# ---------------------------------------------------------------------------
# Env var interpolation
# ---------------------------------------------------------------------------


def _interpolate_env_value(value: str) -> str:
    """Replace ``${VAR}`` patterns in a string with environment variable values.

    Raises :class:`ConfigError` if a referenced variable is not set.
    """
    missing: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        var = match.group(1)
        val = os.environ.get(var)
        if val is None:
            missing.append(var)
            return match.group(0)  # preserve original for error reporting
        return val

    result = _ENV_PATTERN.sub(_replace, value)
    if missing:
        raise ConfigError(f"Environment variable(s) not set: {', '.join(missing)}")
    return result


def _interpolate_env_vars(data: Any) -> Any:
    """Recursively walk a parsed YAML structure and interpolate env vars.

    Collects *all* missing env var names across the entire config and raises
    a single :class:`ConfigError` listing them all.
    """
    all_missing: list[str] = []

    def _walk(node: Any) -> Any:
        if isinstance(node, str):
            try:
                return _interpolate_env_value(node)
            except ConfigError as exc:
                msg = str(exc)
                if "not set:" in msg:
                    vars_str = msg.split("not set:", 1)[1].strip()
                    all_missing.extend(v.strip() for v in vars_str.split(","))
                return node
        if isinstance(node, dict):
            return {k: _walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_walk(item) for item in node]
        return node

    result = _walk(data)
    if all_missing:
        unique = list(dict.fromkeys(all_missing))
        raise ConfigError(f"Environment variable(s) not set: {', '.join(unique)}")
    return result


# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------


def load_config_dict(yaml_content: str) -> dict[str, Any]:
    """Parse a YAML string, interpolate env vars, and return the raw dict.

    This is the low-level loader used by :func:`load_config`. It does not
    perform Pydantic validation.
    """
    try:
        data = yaml.safe_load(yaml_content)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML syntax: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("Config file must contain a YAML mapping at the top level")
    return _interpolate_env_vars(data)  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Config file discovery
# ---------------------------------------------------------------------------


def _find_config_file() -> Path | None:
    """Search for a config file in the standard locations.

    Returns the first existing file path, or ``None`` if no config is found.
    """
    # 1. RECEIPT_INDEX_CONFIG env var
    env_path = os.environ.get("RECEIPT_INDEX_CONFIG")
    if env_path:
        path = Path(env_path).expanduser()
        if path.is_file():
            logger.debug("Config found via RECEIPT_INDEX_CONFIG: %s", path)
            return path
        raise ConfigError(
            f"RECEIPT_INDEX_CONFIG points to '{env_path}' but the file does not exist"
        )

    # 2. ./receipt-index.yaml
    cwd_path = Path.cwd() / "receipt-index.yaml"
    if cwd_path.is_file():
        logger.debug("Config found in current directory: %s", cwd_path)
        return cwd_path

    # 3. ~/.config/receipt-index/config.yaml
    xdg_path = Path.home() / ".config" / "receipt-index" / "config.yaml"
    if xdg_path.is_file():
        logger.debug("Config found at XDG location: %s", xdg_path)
        return xdg_path

    return None


def _config_not_found_message() -> str:
    """Build a helpful error message when no config file is found."""
    locations = [
        "  1. --config CLI flag or config_path argument",
        "  2. RECEIPT_INDEX_CONFIG environment variable",
        "  3. ./receipt-index.yaml (current directory)",
        "  4. ~/.config/receipt-index/config.yaml",
    ]
    return (
        "No configuration file found. Searched:\n"
        + "\n".join(locations)
        + "\n\nCreate a config file with the following structure:\n\n"
        + _EXAMPLE_CONFIG
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_config(config_path: str | None = None) -> AppConfig:
    """Load and validate the application configuration.

    Parameters
    ----------
    config_path:
        Explicit path to a YAML config file. If provided, this takes
        precedence over all other search locations.

    Returns
    -------
    AppConfig
        The fully validated application configuration.

    Raises
    ------
    ConfigError
        If no config file is found, the file cannot be read, env vars are
        missing, or validation fails.
    """
    path: Path
    if config_path is not None:
        path = Path(config_path).expanduser()
        if not path.is_file():
            raise ConfigError(f"Config file not found: {config_path}")
    else:
        found = _find_config_file()
        if found is None:
            raise ConfigError(_config_not_found_message())
        path = found

    logger.info("Loading config from %s", path)
    try:
        yaml_content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Cannot read config file: {exc}") from exc

    config_dict = load_config_dict(yaml_content)

    try:
        return AppConfig.model_validate(config_dict)
    except ValidationError as exc:
        raise ConfigError(f"Config validation failed:\n{exc}") from exc
