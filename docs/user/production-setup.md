# Production Setup

> **Superseded for this project.** The native-PostgreSQL setup below is how receipt-index ran on the dev machine before it moved to the always-on Docker service. That deployment — compose-managed Postgres, scheduled ingest, Dropbox mirror, and the one-time cutover off this native cluster — is documented in **[Server Deployment](server-deployment.md)**, which is now the canonical operations document.
>
> This page remains valid if you want a host-based install with your own PostgreSQL server.

Run Receipt Search Index against a native PostgreSQL 18 server instead of Docker. This is suitable for a host-based deployment where you want to ingest and search your full set of receipts.

## 1. Install PostgreSQL 18

On Ubuntu (with the [PGDG apt repository](https://wiki.postgresql.org/wiki/Apt) configured):

```bash
sudo apt install postgresql-18
```

This creates a default cluster (`18/main`), starts it, and enables autostart on reboot. Verify:

```bash
pg_lsclusters
```

You should see something like:

```
Ver Cluster Port Status Owner    Data directory
18  main    5432 online postgres /var/lib/postgresql/18/main
```

Note the port — if other PostgreSQL versions are installed, 18 may get assigned `5433` or higher.

## 2. Create Database and Roles

Connect as the `postgres` superuser and set up the database and application roles:

```bash
sudo -u postgres psql
```

```sql
-- Create the database
CREATE DATABASE receipt_index;

-- Create application roles (names match the grant migration)
CREATE ROLE receipt_index_dev_all WITH LOGIN PASSWORD 'your-secure-password';    -- pragma: allowlist secret
CREATE ROLE receipt_index_dev_write WITH LOGIN PASSWORD 'your-secure-password';  -- pragma: allowlist secret
CREATE ROLE receipt_index_dev_read WITH LOGIN PASSWORD 'your-secure-password';   -- pragma: allowlist secret

-- Grant database-level connect
GRANT CONNECT ON DATABASE receipt_index TO receipt_index_dev_all;
GRANT CONNECT ON DATABASE receipt_index TO receipt_index_dev_write;
GRANT CONNECT ON DATABASE receipt_index TO receipt_index_dev_read;

\q
```

The `_dev_` suffix in the role names is just a convention from the Docker dev setup — the grant migration references these names, so we reuse them here to keep things simple.

### Alternative: Single Application Role

If you prefer a simpler setup with one application role, create it and then add the grant target roles as members:

```bash
sudo -u postgres psql
```

```sql
CREATE DATABASE receipt_index;

-- Single application role
CREATE ROLE receipt_index WITH LOGIN PASSWORD 'your-secure-password';  -- pragma: allowlist secret
GRANT CONNECT ON DATABASE receipt_index TO receipt_index;

-- Create grant target roles (referenced by migrations) and assign to the app role
CREATE ROLE receipt_index_dev_write;
CREATE ROLE receipt_index_dev_read;
GRANT receipt_index_dev_write TO receipt_index;
GRANT receipt_index_dev_read TO receipt_index;

\q
```

This gives `receipt_index` all the permissions that the migrations grant to the `_write` and `_read` roles.

## 3. Run Migrations

Set the migration URL to use the `postgres` superuser (or another superuser role) and point it at your native cluster:

```bash
export MIGRATION_DATABASE_URL="postgresql://postgres@localhost:5432/receipt_index?sslmode=disable"
make migrate-up
```

This auto-downloads golang-migrate if needed, then applies all migrations in schema dependency order. The migrations create the `receipt` schema, tables, indexes, and grant permissions to the application roles created in step 2.

## 4. Configure `.env`

Update your `.env` to point at the native PostgreSQL instance instead of Docker:

```bash
# Database — application connection (write role, used by the CLI)
DATABASE_URL=postgresql://receipt_index_dev_write:your-secure-password@localhost:5432/receipt_index  # pragma: allowlist secret

# Database — migrations (superuser, used by make migrate-up)
MIGRATION_DATABASE_URL=postgresql://postgres@localhost:5432/receipt_index
```

Key differences from the Docker dev setup:

| Setting | Docker (dev) | Native (production) |
|---|---|---|
| Port | `15432` (mapped) | `5432` (default) or check `pg_lsclusters` |
| Database name | `receipt_index_dev` | `receipt_index` |
| Migration user | `main` (Docker superuser) | `postgres` (system superuser) |
| Passwords | `localpass` | Use secure passwords |
| SSL | `sslmode=disable` | Consider `sslmode=require` for remote connections |

All other `.env` variables (`IMAP_*`, `ANTHROPIC_API_KEY`, `RECEIPT_STORE_PATH`, etc.) remain the same.

## 5. Verify

Test the connection:

```bash
psql "$DATABASE_URL"
```

Run a dry-run ingest:

```bash
uv run receipt-index ingest --dry-run
```

Then ingest your full receipt set:

```bash
uv run receipt-index ingest
```

## 6. PostgreSQL Maintenance

### Check cluster status

```bash
pg_lsclusters
sudo systemctl status postgresql@18-main
```

### Start / stop / restart

```bash
sudo systemctl start postgresql@18-main
sudo systemctl stop postgresql@18-main
sudo systemctl restart postgresql@18-main
```

### Logs

```bash
sudo journalctl -u postgresql@18-main -f
```

### pg_hba.conf

For same-host deployment (the typical case), no `pg_hba.conf` changes are needed — the default configuration allows local connections via peer and md5 authentication.

If connecting from a different host, edit `/etc/postgresql/18/main/pg_hba.conf` to allow remote connections, and set `listen_addresses` in `postgresql.conf`. Restart after changes.
