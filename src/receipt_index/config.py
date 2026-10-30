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
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    SecretStr,
    Tag,
    ValidationError,
    field_validator,
    model_validator,
)

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
  backend: local
  path: ./data/receipts

llm:
  model: claude-haiku-5-5
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
    """Evidence store configuration.

    ``backend`` selects the store. Only ``local`` exists so far; the Drive
    backend adds ``drive`` and its own settings.
    """

    backend: Literal["local"] = "local"
    path: str = "./data/receipts"


class LlmConfig(BaseModel):
    """LLM provider configuration."""

    model: str = "claude-haiku-5-5"
    api_key: str
    # List prices per million tokens, for `reextract --dry-run` cost
    # estimates. Optional: prices change, so they are configuration.
    price_per_mtok_input: Decimal | None = Field(default=None, ge=0)
    price_per_mtok_output: Decimal | None = Field(default=None, ge=0)


class LoggingConfig(BaseModel):
    """Logging configuration."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


class HealthConfig(BaseModel):
    """``serve``'s health checks and dead-man ping (the ``ingest.health:`` block).

    ``ping_url`` is a capability: anyone holding it can send pings. It is a
    ``SecretStr``, and ``AppConfig`` hides input values in its errors, so a
    bad value never reaches a log line or a config error.
    """

    # The heartbeat is written every 30 s; twice that is the floor.
    heartbeat_stale_seconds: int = Field(default=120, ge=60)
    # When unset, each source's threshold is 3x its poll interval.
    source_stale_seconds: int | None = Field(default=None, ge=30)
    # An item that turned terminal this recently fails the ping; 0 = never.
    terminal_alert_seconds: int = Field(default=86400, ge=0)
    ping_url: SecretStr | None = None
    ping_interval_seconds: int = Field(default=300, ge=30)

    @field_validator("ping_url")
    @classmethod
    def _http_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        # No value in any message: the URL is a capability.
        url = value.get_secret_value().strip()
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("ping_url must be an http or https URL")
        if any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in url):
            raise ValueError("ping_url must not contain spaces or control characters")
        if "?" in url or "#" in url:
            # The failure ping appends /fail to the path.
            raise ValueError("ping_url must not have a query string or fragment")
        return SecretStr(url.rstrip("/"))


class IngestConfig(BaseModel):
    """Ingest timing (the ``ingest:`` block).

    Every key has a default, so a config without the block loads unchanged.
    """

    # A Drive change younger than this is left for the next run, because
    # scanner apps can create a file and upload its content in a second step.
    drive_settle_seconds: int = Field(default=60, ge=0)

    # `serve` (design sections 1, 2 and 4). IDLE is re-issued this often even
    # when nothing happened; RFC 2177 lets a server drop an idle client after
    # 29 minutes, and routers often drop idle flows sooner.
    idle_reissue_seconds: int = Field(default=600, ge=60)
    # The IMAP safety poll, which runs whether or not IDLE fired (and is the
    # only poll on a server without IDLE), and the Drive poll interval.
    imap_poll_seconds: int = Field(default=240, ge=30)
    drive_poll_seconds: int = Field(default=120, ge=30)
    # How long one step (an item, or a source's listing) may run before
    # `serve`'s watchdog exits the process. A slow but healthy item must never
    # reach it, because the restart charges the item a content attempt. The
    # floor is the sum of the deadlines one item can meet without failing:
    #   a retry's re-fetch reconnecting to IMAP (3 tries x 120 s + 3 s) 363
    #   the body fetch (IMAP socket timeout)                              120
    #   two model requests (one output retry; 180 s covers SDK retries)   360
    #   Playwright: launch, content and PDF, 60 s each                    180
    #   one database statement (statement_timeout)                         60
    #   weasyprint after a Playwright timeout (no deadline of its own)    117
    #                                                                    1200
    item_timeout_seconds: int = Field(default=1200, ge=1200)

    # Retries (design section 6). A content failure uses up an attempt and is
    # due again after the backoff for its attempt number; the failure that
    # reaches retry_max_attempts is terminal. Transient and config failures
    # don't use up an attempt, until an item has spent
    # transient_item_max_seconds failing that way.
    retry_max_attempts: int = Field(default=3, ge=1)
    retry_backoff_seconds: list[int] = Field(
        default_factory=lambda: [300, 1800, 7200, 43200]
    )
    transient_item_max_seconds: int = Field(default=21600, ge=0)

    health: HealthConfig = HealthConfig()

    def poll_seconds(self, source_type: str) -> int:
        """The poll interval of a source type: the IMAP safety poll or Drive's."""
        if source_type == "imap":
            return self.imap_poll_seconds
        return self.drive_poll_seconds

    @model_validator(mode="after")
    def _check_backoff(self) -> IngestConfig:
        needed = self.retry_max_attempts - 1
        if len(self.retry_backoff_seconds) < needed:
            msg = (
                f"retry_backoff_seconds has {len(self.retry_backoff_seconds)} "
                f"entries; retry_max_attempts {self.retry_max_attempts} needs "
                f"at least {needed}"
            )
            raise ValueError(msg)
        if any(seconds < 0 for seconds in self.retry_backoff_seconds):
            raise ValueError("retry_backoff_seconds entries must be 0 or more")
        return self


class AnnotationsConfig(BaseModel):
    """Changes made after extraction (the ``annotations:`` block).

    ``actor`` is the name the CLI records on each change it makes, unless
    ``--as`` gives another. Attribution, not authentication.
    """

    actor: str = Field(default="operator", min_length=1)

    @field_validator("actor")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("annotations.actor must not be blank")
        return value


class AppConfig(BaseModel):
    """Top-level application configuration.

    Validation errors name the key but never echo its value: config values
    include credentials and the health ping URL. (Pydantic takes this setting
    from the outermost model only.)
    """

    model_config = ConfigDict(hide_input_in_errors=True)

    sources: list[SourceConfig]
    database: DatabaseConfig
    store: StoreConfig = StoreConfig(path="./data/receipts")
    llm: LlmConfig
    logging: LoggingConfig = LoggingConfig()
    ingest: IngestConfig = IngestConfig()
    annotations: AnnotationsConfig = AnnotationsConfig()

    @model_validator(mode="after")
    def _check_source_stale(self) -> AppConfig:
        """An explicit stale threshold must not be below a poll it applies to."""
        stale = self.ingest.health.source_stale_seconds
        if stale is None:
            return self
        for kind in sorted({s.type for s in self.sources}):
            poll = self.ingest.poll_seconds(kind)
            if stale < poll:
                key = "imap_poll_seconds" if kind == "imap" else "drive_poll_seconds"
                msg = (
                    f"ingest.health.source_stale_seconds ({stale}) is below "
                    f"ingest.{key} ({poll}), so every {kind} source "
                    "would be stale between polls"
                )
                raise ValueError(msg)
        return self


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
