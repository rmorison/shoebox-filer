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
# Uses MIGRATION_DATABASE_URL: from .env in a dev checkout, from .env.db on the server
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

### A receipt that is only a link

Some merchants, point-of-sale systems especially, send an email that holds only a link to a hosted receipt. Ingest doesn't follow the link, since most receipt and order pages need a login. It records the item as a link-only receipt, with the link, instead of skipping it silently:

```bash
uv run receipt-index failures --status link-only                # host and subject
uv run receipt-index failures --status link-only --output json  # with receipt_link
```

Open each link and save the receipt by hand. The item stays on this list, since a receipt saved by hand is a new item, so use `--since YYYY-MM-DD` to see only the new ones. Items skipped before link-only receipts were recorded can be re-checked with `retry --status skipped --since YYYY-MM-DD --yes`: their links then show up in this list.

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

## Always-on ingest (`serve`)

These apply to the server's `ingest` service, after the cutover in [Server Deployment](server-deployment.md#89-cutover-to-serve) 8.9. Run every command from the clone root. Run `status` in the `ingest` container: it reads `serve`'s heartbeat file there, and anywhere else it says `heartbeat: none`.

```bash
docker compose -f deploy/docker-compose.yml exec ingest receipt-index status
docker compose -f deploy/docker-compose.yml exec ingest receipt-index status --check
```

### The `ingest` container is unhealthy

`docker compose ps` shows `(unhealthy)` after three failed `status --check` runs in a row, a minute apart. Docker doesn't restart an unhealthy container, so it stays that way until the cause clears. Read why:

```bash
docker compose -f deploy/docker-compose.yml exec ingest receipt-index status --check
docker inspect --format '{{range .State.Health.Log}}{{.Output}}{{end}}' "$(docker compose -f deploy/docker-compose.yml ps -q ingest)"
docker compose -f deploy/docker-compose.yml logs --since 1h ingest
```

Each line names a problem; the table in 8.9 step 5 says what each means. The usual ones:

- **`heartbeat missing` or `heartbeat stale ...`:** `serve` isn't running in the container, or its worker is stuck. A config error stops `serve` at start, and the log says which key. If the log shows nothing new for minutes, `docker compose -f deploy/docker-compose.yml restart ingest`.
- **`source <name> stale ...` or `never succeeded`:** reads of that source fail. `status` shows the source's `last error`. A mail server or Drive outage clears itself; an expired app password or Drive token needs a new one in `.env`, then `up -d`.
- **`source <name> paused ...`:** the worker paused the source after repeated transient or config failures. `status` shows the reason under `Worker`.
- **`database unreachable`:** check `docker compose -f deploy/docker-compose.yml ps db` and `logs db`.

### A missed or failed ping

The monitor alerts for one of two reasons:

- **Pings stopped.** `serve` isn't running, or the host or Docker is down, or `serve` can't reach the monitor. Check `ps` first. If `ingest` is up and healthy, look for failed pings in `logs ingest`: they're logged without the URL, and a failed ping never stops `serve`. If no ping was ever sent, check that `ping_url` is uncommented in `deploy/receipt-index.yaml` and that the container was recreated after the change (`up -d --force-recreate ingest`): `serve` reads its config only when it starts.
- **A failure ping arrived.** Its body has one line per problem, the same lines as `status --check`, and never error text or receipt details. `terminal 1 in 24h` means an item failed for good in the last day: it doesn't make the container unhealthy, and the alert clears itself a day later. List them with `receipt-index failures --status terminal`, fix the cause, and requeue them with `retry`, as in [DLQ recovery](server-deployment.md#82-dlq-recovery).

If every command fails with an error about `HEALTH_PING_URL` or `ping_url`, the config has `ping_url` uncommented but the variable isn't in the container's environment, or is empty. Set it in `.env` and `up -d`, or comment the key out again.

### The watchdog restarted `serve`

The log shows `Watchdog: <step> has run <N>s, past item_timeout_seconds (1200); ...` and `serve` exits with code 4. One item, or one source's listing, ran past `item_timeout_seconds` and couldn't be stopped from inside, so `serve` exited and Docker restarted it (`restart: unless-stopped`). The item's unfinished attempt is recovered after the restart and counts as a failed attempt, so an item that hangs every time ends up terminal rather than restarting `serve` for ever. One restart is nothing to act on. Several for the same item mean a renderer or extraction hang worth a look: `failures --source <name>` shows the item.

### A source lock is held by another `ingest`

`status --check` shows `source <name> lock lost` or `source <name> not held`, and `serve` doesn't watch that source. Another process holds its lock: almost always a one-shot `receipt-index ingest` started by hand, or a cron ingest line left in a crontab. While `serve` holds a source, a one-shot `ingest` exits 3 with `source <name> is being watched by a running serve`; it is the other order, a one-shot started first, that leaves `serve` without the source. If another process holds every source, `serve` has nothing to watch: it prints the same line for each source and exits 3, and Docker keeps restarting it. `ps` shows `ingest` restarting, `logs ingest` shows the lines, and the healthcheck or the monitor reports a missing or stale heartbeat.

1. Find it. A one-shot run in the scheduler shows in its process list (the image has no `ps`, so read `/proc`):

   ```bash
   docker compose -f deploy/docker-compose.yml exec -T scheduler sh -c 'grep -lsa "[r]eceipt-index.ingest" /proc/[0-9]*/cmdline' </dev/null
   ```

2. Let it finish, or stop it. Then restart `serve` so it takes the lock again:

   ```bash
   docker compose -f deploy/docker-compose.yml restart ingest
   ```

3. If a cron line started it, remove the line from `deploy/crontab` and `up -d`.

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
