# Getting Started

Set up Receipt Search Index from scratch and run your first ingest.

## Prerequisites

- **Python 3.11+** — [python.org](https://www.python.org/downloads/)
- **uv** — [docs.astral.sh/uv](https://docs.astral.sh/uv/) (Python package manager)
- **Docker** and Docker Compose — [docker.com](https://www.docker.com/)
- **Anthropic API key** — [console.anthropic.com](https://console.anthropic.com/)
- An IMAP email account with receipts in a designated folder

> **Note:** golang-migrate is downloaded automatically by `make setup` — no manual install needed.

## 1. Clone and Install

```bash
git clone https://github.com/rmorison/shoebox-filer.git
cd shoebox-filer
make setup        # Installs Python deps, Playwright browser, and pre-commit hooks
```

## 2. Start Database

```bash
docker compose up -d    # Starts PostgreSQL 18
make migrate-up         # Applies database schema migrations
```

To verify the database is running:

```bash
docker compose ps
```

## 3. Configure Environment

```bash
cp example.env .env
```

Edit `.env` with your settings. The key variables:

| Variable | Description | Example |
|---|---|---|
| `IMAP_HOST` | Your email server | `imap.gmail.com` |
| `IMAP_PORT` | IMAP port (usually 993 for SSL) | `993` |
| `IMAP_USERNAME` | Email address | `you@example.com` |
| `IMAP_PASSWORD` | App-specific password | (see below) |
| `IMAP_FOLDER` | Folder containing receipts | `INBOX.Receipts` |
| `IMAP_USE_SSL` | Use SSL connection | `true` |
| `ANTHROPIC_API_KEY` | Your Anthropic API key | `sk-ant-...` |
| `DATABASE_URL` | PostgreSQL connection string | (default works with Docker) |
| `RECEIPT_STORE_PATH` | Where PDFs are saved | `./data/receipts` |

Optional variables (defaults are usually fine):

| Variable | Description | Default |
|---|---|---|
| `MIGRATION_DATABASE_URL` | Superuser URL for running migrations | (see `example.env`) |
| `LLM_MODEL` | Anthropic model for metadata extraction | `claude-haiku-5-5` |
| `LOG_LEVEL` | Logging verbosity | `INFO` |

`PLAYWRIGHT_BROWSERS_PATH` can generally be left at its default. The Docker port variables (`POSTGRES_HOST_PORT`, `GREENMAIL_SMTP_PORT`, `GREENMAIL_IMAP_PORT`) are commented out in `example.env`: Docker Compose uses its own defaults without them, but the integration tests need them set (see [Integration tests](#integration-tests)).

### Email App-Specific Passwords

Most email providers require an app-specific password rather than your main account password:

- **Gmail**: [Create an app password](https://support.google.com/accounts/answer/185833) (requires 2FA enabled)
- **Other providers**: Check your provider's documentation for IMAP access and app-specific passwords

## 4. Set Up Your Email Folder

Create a dedicated folder in your email client for receipts (e.g., `Receipts` or `INBOX.Receipts`). Move or copy receipt emails into this folder. The tool will ingest all messages from this folder.

Set the `IMAP_FOLDER` variable in your `.env` to match the folder path. Folder naming varies by provider:

- **Gmail**: `[Gmail]/Receipts` or a label name
- **Most IMAP servers**: `INBOX.Receipts` or `Receipts`

Check your email client's folder list if unsure of the exact path.

## 5. Running the CLI

`make setup` installs the project into a uv-managed virtual environment. Run all CLI commands with `uv run`:

```bash
uv run receipt-index --help
```

If you prefer, you can activate the virtual environment first and run `receipt-index` directly:

```bash
source .venv/bin/activate
receipt-index --help
```

All examples in this guide and the [CLI Reference](cli-reference.md) use the `uv run` form.

## 6. First Ingest

Preview what will be processed without making changes:

```bash
uv run receipt-index ingest --dry-run
```

When you're satisfied, run a small batch first:

```bash
uv run receipt-index ingest --limit 5
```

Then ingest everything:

```bash
uv run receipt-index ingest
```

You'll see output like:

```
Processed: 12  Skipped: 3  Failed: 0
```

- **Processed**: Receipts successfully indexed with metadata extracted and PDFs generated
- **Skipped**: Messages that didn't look like receipts (low LLM confidence)
- **Failed**: Messages that encountered errors during processing

## 7. Search Your Receipts

```bash
uv run receipt-index search --vendor amazon
```

See the [CLI Reference](cli-reference.md) for all search options and more examples.

## Integration tests

The integration tests (for contributors) run against the Docker Compose stack: PostgreSQL and GreenMail, a mock mail server. They rebuild and empty tables, so they never guess where that stack listens. `POSTGRES_HOST_PORT`, `GREENMAIL_SMTP_PORT` and `GREENMAIL_IMAP_PORT` must all be set, and `example.env` leaves them commented out. Until all three are set, every integration test skips without connecting, and the skip message names the variables that are missing.

After `make setup`, set the three ports in `.env`. Choose ports nothing else on the host uses; on a host that also runs another copy of the stack, such as production, don't use Compose's defaults (15432, 3025, 3143). For example:

```bash
POSTGRES_HOST_PORT=25432
GREENMAIL_SMTP_PORT=23025
GREENMAIL_IMAP_PORT=23143
```

A `.env` created from an older `example.env` still sets the three ports to Compose's defaults, so check it on a shared host.

Change the port in `DATABASE_URL` and `MIGRATION_DATABASE_URL` in `.env` to the same `POSTGRES_HOST_PORT`. Then start the stack and run the tests. The tests apply the schema themselves, so they need no `make migrate-up`. Docker Compose and the Makefile both read `.env`, so each command uses the same ports:

```bash
docker compose up -d
make test-integration
```

To run `pytest` directly instead of through `make`, export the three variables in your shell first. Don't rely on `.env` for that: the tests read the variables from the environment before anything loads it.

## Next Steps

- [CLI Reference](cli-reference.md) — Full command documentation and example workflows
- [Production Setup](production-setup.md) — Native PostgreSQL 18 instead of Docker
- [Troubleshooting](troubleshooting.md) — Common issues and solutions
