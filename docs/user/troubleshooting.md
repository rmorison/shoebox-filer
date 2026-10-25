# Troubleshooting

## Docker / Database

### `connection refused` or `could not connect to server`

The PostgreSQL container isn't running.

```bash
docker compose up -d
docker compose ps          # Verify it's healthy
```

### Migration errors

Ensure you're using the migration superuser URL, not the app URL:

```bash
# Uses MIGRATION_DATABASE_URL from .env
make migrate-up
```

If migrations are out of sync, check the current version:

```bash
# Check public schema migrations
migrate -path db/migrations/public \
  -database "${MIGRATION_DATABASE_URL}&x-migrations-table=schema_migrations_public" version

# Check receipt schema migrations
migrate -path db/migrations/receipt \
  -database "${MIGRATION_DATABASE_URL}&x-migrations-table=schema_migrations_receipt" version
```

### `golang-migrate` not found

Install from [GitHub releases](https://github.com/golang-migrate/migrate/releases) or via your package manager. On Linux:

```bash
curl -L https://github.com/golang-migrate/migrate/releases/download/v4.18.1/migrate.linux-amd64.tar.gz | tar xz
sudo mv migrate /usr/local/bin/
```

## IMAP Connection

### `Login failed` or authentication errors

- Verify `IMAP_HOST`, `IMAP_USERNAME`, and `IMAP_PASSWORD` in `.env`
- Use an **app-specific password**, not your main account password
- For Gmail: ensure IMAP is enabled in Settings > Forwarding and POP/IMAP

### `Folder not found`

Check the exact folder path. Folder names are case-sensitive and format varies by provider:

- Gmail: `[Gmail]/Receipts` or just the label name
- Most IMAP: `INBOX.Receipts` or `Receipts`

You can list available folders with any IMAP client (e.g., Thunderbird, mutt) to find the exact path.

### SSL/TLS errors

Ensure `IMAP_USE_SSL=true` and `IMAP_PORT=993` for standard SSL connections. STARTTLS is not currently supported — use SSL (port 993) or plain IMAP (`IMAP_USE_SSL=false`, port 143). Most modern providers require SSL.

## Ingest

### Many messages skipped

Skipped messages are those the LLM determined are not receipts (low confidence). This is expected for non-receipt emails in the folder. To reduce skips, move only actual receipt emails into your designated folder.

### `amount > 0` validation errors

Some emails (newsletters, shipping notifications without amounts) may cause the LLM to return a zero amount. These are correctly skipped. If a legitimate receipt is being skipped, check that the email contains a clear purchase amount.

### Ingest is slow

Each message requires an LLM API call and PDF rendering. Use `--limit` to process in batches:

```bash
uv run receipt-index ingest --limit 20
```

### Missing environment variable errors

Ensure all required variables are set in `.env`. At minimum:

- `IMAP_HOST`, `IMAP_PORT`, `IMAP_USERNAME`, `IMAP_PASSWORD`, `IMAP_FOLDER`
- `DATABASE_URL`
- `ANTHROPIC_API_KEY`
- `RECEIPT_STORE_PATH`

## Search

### `No receipts found`

- Verify receipts have been ingested: `uv run receipt-index search` (no filters)
- Check for typos in vendor names — search is case-insensitive but requires a substring match
- Widen your date range or amount range

### Vendor name doesn't match

The vendor name is extracted by the LLM and may not exactly match what you expect. Try a shorter substring:

```bash
# Instead of "Whole Foods Market"
uv run receipt-index search --vendor "whole"
```

## PDF Output

### PDFs are empty or missing

- Verify `RECEIPT_STORE_PATH` in `.env` points to a writable directory
- Check that Playwright's Chromium was installed: `make setup`
- Look for errors in the ingest output or set `LOG_LEVEL=DEBUG` for verbose output

## General

### Verbose logging

Set `LOG_LEVEL=DEBUG` in `.env` for detailed output:

```bash
LOG_LEVEL=DEBUG uv run receipt-index ingest --limit 1
```

### Resetting the database

To start fresh (drops all data):

```bash
make migrate-down         # Roll back migrations
make migrate-up           # Re-apply migrations
```

Note: this does not delete PDF files from the file store. Remove them manually if needed:

```bash
rm -rf ./data/receipts/*
```
