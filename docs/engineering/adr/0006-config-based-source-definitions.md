# ADR-0006: Config-Based Source Definitions

## Status

Proposed

## Context

Phase 1 uses flat environment variables to configure a single IMAP source (`IMAP_HOST`, `IMAP_USERNAME`, `IMAP_PASSWORD`, `IMAP_FOLDER`, etc.). Phase 2 introduces Google Drive as a second source type, and the product spec calls for supporting multiple sources of each type (e.g., two IMAP accounts, three Drive folders).

Key factors:

- **Multi-source support**: Flat env vars don't scale — configuring two IMAP accounts would require `IMAP2_HOST`, `IMAP2_USERNAME`, etc., which is fragile and ugly.
- **Source naming**: Each source needs a human-readable name for log output, `--source` CLI filtering, and database tracking.
- **Security**: Passwords, API keys, and OAuth tokens must not be stored in config files that might be committed to version control.
- **Simplicity**: The tool is a local CLI — the config system should be straightforward, not enterprise-grade.

## Decision

Use a **YAML configuration file** with **named source entries** and **environment variable interpolation** for secrets.

### Config file location

Search order (first found wins):
1. `--config` CLI flag
2. `RECEIPT_INDEX_CONFIG` env var
3. `./receipt-index.yaml` (project-local)
4. `~/.config/receipt-index/config.yaml` (XDG convention)

### Source definitions

Each source is a named entry with a `type` discriminator:

```yaml
sources:
  - name: personal-email
    type: imap
    host: mail.example.com
    password: ${IMAP_PASSWORD}
    # ...

  - name: scanned-receipts
    type: gdrive
    folder_id: "1aBcDeFg..."
    token_json: ${GDRIVE_TOKEN_JSON}
```

### Secret handling

`${VAR_NAME}` syntax in string values is resolved against environment variables at load time. If the referenced variable is unset, config loading fails with a clear error. Secrets never appear as literals in the config file.

### No backward compatibility

Phase 2 is a clean break. The Phase 1 env var config helpers are removed. If no config file is found, the CLI fails with a clear error and example config. No fallback to legacy env vars.

## Consequences

### Positive

- **Scales to N sources**: Adding a third IMAP account is one more YAML block, not a naming convention negotiation.
- **Self-documenting**: The config file describes the full source topology in one place.
- **Version-controllable**: Config files (minus secrets) can be committed alongside the project.
- **Typed validation**: Pydantic discriminated union validates config at load time, catching errors early.
- **CLI ergonomics**: `--source personal-email` is clearer than `--imap-host mail.example.com`.

### Negative

- **New dependency**: `pyyaml` added to runtime dependencies (widely used, low risk).
- **Migration effort**: Existing `.env`-only users must create a config file (mitigated by example config and clear error messages).

### Alternatives Considered

- **TOML**: Python-native (PEP 680), but polymorphic source definitions are less readable in TOML's table syntax. YAML's flow is more natural for lists of typed objects.
- **JSON**: Verbose, no comments, not human-friendly for hand-edited config.
- **Keep env vars, invent naming conventions**: `IMAP_1_HOST`, `IMAP_2_HOST`, etc. Fragile, error-prone, and requires documentation of the naming scheme.
- **Config in database**: Over-engineered for a CLI tool. Config should be inspectable and version-controllable without database access.

## Follow-ups

- Implement `AppConfig` Pydantic model with discriminated union for source types
- Implement YAML loading with env var interpolation
- Implement config file discovery
- Add `--config` flag to CLI
- Write migration guide for existing env var users
- Add example config file to repository
