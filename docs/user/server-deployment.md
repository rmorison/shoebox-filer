# Server Deployment

Take a clean Ubuntu box to an unattended receipt-index service: Postgres in Docker, `receipt-index ingest` on a 15-minute schedule, receipts and nightly database dumps mirrored to Dropbox.

This is the canonical operations document for the always-on deployment — provisioning, first-launch gates, the one-time cutover from the dev machine, weekly monitoring, upgrades, and disaster recovery.

> **Not this document:** [Production Setup](production-setup.md) describes running against a native PostgreSQL server on the dev machine. That is the arrangement this deployment replaces.

**Contents**

1. [Overview and layout](#1-overview-and-layout)
2. [Server prerequisites](#2-server-prerequisites)
3. [Provisioning](#3-provisioning)
4. [Build and migrate](#4-build-and-migrate)
5. [First-launch Go/No-Go gates](#5-first-launch-gono-go-gates)
6. [Cutover from the dev machine (one-time)](#6-cutover-from-the-dev-machine-one-time)
7. [Weekly monitoring](#7-weekly-monitoring)
8. [Routine operations](#8-routine-operations)
9. [Searching from the laptop](#9-searching-from-the-laptop)
10. [Read-only MCP server](#10-read-only-mcp-server)

---

## 1. Overview and layout

One Docker Compose project, named `receipt-index` (set by `name:` at the top of `deploy/docker-compose.yml`), with two services:

| Service | What it is |
|---|---|
| `db` | `postgres:18`, named volume `pgdata`, published on `127.0.0.1:15432` only |
| `scheduler` | The app image with `supercronic /app/crontab` as its entrypoint — ingest every 15 min, two rclone mirror lines, one nightly `pg_dump` |

**The git clone is the deployment unit.** There is no registry and no CI: the image is built on the server from the checkout, and deploying an update is `git pull` + rebuild. Everything the server needs beyond the clone is a handful of git-ignored files at the clone root.

Documented clone location: **`~/receipt-index`**, in the home directory of the login user you SSH in as. That user should be uid/gid 1000, which is also the image's `appuser` — so the bind-mounted directories need no `sudo` to manage and files written by the container are readable by you. `/srv/receipt-index` works equally well if you prefer a system location; every command below is run from the clone root, so only the `cd` changes, but you will need `sudo` for the `chown` steps.

Layout after provisioning:

```
~/receipt-index/                 <- clone root; all compose commands run from here
├── .env                         <- secrets (git-ignored, chmod 600)
├── data/
│   ├── receipts/                <- rendered PDF tree      (uid 1000)
│   ├── backups/                 <- nightly pg_dump files  (uid 1000)
│   └── mcp-logs/                <- MCP access log, never mirrored (uid 1000)
├── rclone-config/               <- Dropbox token          (uid 1000, chmod 700)
└── deploy/
    ├── docker-compose.yml       <- the production compose file
    ├── crontab                  <- supercronic schedule
    ├── backup.sh                <- nightly dump script
    ├── example.receipt-index.yaml  <- config template (copy + edit, see 3.2b)
    ├── receipt-index.yaml       <- your copy (git-ignored; no secrets, only ${VAR})
    ├── db-init/01-create-roles.sh
    ├── example.env
    └── rclone-setup.md
```

Compose resolves relative paths against the directory holding the compose file, so `deploy/docker-compose.yml` refers to the four state directories with a `../` prefix (`../.env`, `../data/receipts`, `../data/backups`, `../rclone-config`) and to its own siblings with `./`. That is why `.env` belongs at the **clone root** and not in `deploy/`.

Every command in this document assumes:

```bash
cd ~/receipt-index
```

and uses the explicit `-f deploy/docker-compose.yml` form, so the repo-root `docker-compose.yml` (the dev stack: db + GreenMail) is never picked up by accident.

Laptop-side commands use **`receipt-server`** as the SSH target. Either define that alias in `~/.ssh/config` (recommended — the search wrapper defaults to it) or substitute your server's hostname wherever it appears.

---

## 2. Server prerequisites

- **Docker Engine + the Compose v2 plugin.** Install from Docker's own apt repository, not the distro `docker.io` package (which lags and does not ship `docker compose`).

  ```bash
  docker --version
  docker compose version        # must print "Docker Compose version v2..." or newer
  ```

- **Docker enabled at boot** — everything about reboot survival depends on this, because the containers' `restart: unless-stopped` policies only apply once the daemon is running:

  ```bash
  sudo systemctl enable --now docker
  systemctl is-enabled docker   # must print "enabled"
  ```

- **The deploy user in the `docker` group** (`sudo usermod -aG docker $USER`, then log out and back in), so `docker compose` and the SSH search wrapper work without `sudo`.

- **Outbound network**: HTTPS/443 to `api.anthropic.com`, `www.googleapis.com`, `api.dropboxapi.com` and `content.dropboxapi.com`, plus **IMAPS/993** to your IMAP provider. No inbound ports are needed; the database is published on loopback only.

- **`git`, `make`, `curl`** — `make migrate-up` downloads golang-migrate into `.bin/` with curl.

- **`uv`** ([docs.astral.sh/uv](https://docs.astral.sh/uv/)) — only for Gate A, which runs the in-container render tests from the repo's test suite. Skip it if you accept the manual fallback in Gate A.

- **Disk**: the pgdata volume, the receipt tree, seven database dumps, and capped container logs (10 MB × 7 per service). A few GB is plenty; check with `df -h` before starting.

---

## 3. Provisioning

### 3.1 Clone

```bash
git clone https://github.com/rmorison/shoebox-filer.git ~/receipt-index
cd ~/receipt-index
```

### 3.2 Create `.env` at the clone root

```bash
cp deploy/example.env .env
chmod 600 .env
```

`deploy/example.env` documents every variable. Real values come from 1Password (`.env` is git-ignored and is never committed):

| Variable | Source |
|---|---|
| `POSTGRES_PASSWORD` | Generate: `openssl rand -hex 24` |
| `RECEIPT_INDEX_PASSWORD` | Generate: `openssl rand -hex 24` — must match the password inside `DATABASE_URL` |
| `IMAP_PASSWORD` | 1Password — your IMAP mailbox item |
| `ANTHROPIC_API_KEY` | 1Password — your Anthropic API key item |
| `GDRIVE_TOKEN_JSON` | Output of `receipt-index auth gdrive --client-credentials <client_secret.json>`, run on the laptop |

### 3.2b Create the server app config

```bash
cp deploy/example.receipt-index.yaml deploy/receipt-index.yaml
```

Edit the copy (it is git-ignored) with your real source definitions: IMAP host, username, and folder, and the Google Drive folder id. Secrets stay as `${VAR}` references into `.env`.

`GDRIVE_CREDENTIALS_JSON` is **not** required on the server. The template deliberately drops that key from the gdrive source, and the OAuth client id/secret needed for refresh already live inside `GDRIVE_TOKEN_JSON`. (The dev config may still interpolate it; do not copy that line over.)

Also do **not** set `PLAYWRIGHT_BROWSERS_PATH` in the server `.env`. The image bakes `/ms-playwright`; the dev value (`.playwright`) is a relative path that does not exist in the container, and if it leaks in, every HTML receipt fails to render and is permanently dead-lettered.

### 3.3 The `$`-in-secrets hazard — read before pasting a password

Docker Compose performs variable expansion on values inside an `env_file`. **A secret containing `$` is silently corrupted.** Verified behaviour: a value written as

```
IMAP_PASSWORD=ab$wcd$$ef
```

is delivered to the container as `ab$ef` — `$wcd` expands to the empty string (compose only mutters `The "wcd" variable is not set` on stderr) and `$$` collapses to a literal `$`. Authentication then fails with an error that says nothing about the real cause. `make`, which also reads this file, has the same hazard.

Rules:

- Escape every literal dollar sign by doubling it: `ab$wcd` → `ab$$wcd`.
- Applies to `IMAP_PASSWORD`, `ANTHROPIC_API_KEY`, both Postgres passwords, and the passwords embedded in `DATABASE_URL` / `MIGRATION_DATABASE_URL`.
- Generate the Postgres passwords from an alphanumeric alphabet (`openssl rand -hex 24`) so the question never comes up for those.

Two checks. First, no expansion warnings at all:

```bash
docker compose -f deploy/docker-compose.yml config --quiet     # must print nothing
```

Second — the check that actually proves it, because a *defined* variable expands silently with no warning — compare the value **as the container sees it** against 1Password, hashed so nothing is printed:

```bash
# In the container (uid 1000, real .env, real compose expansion):
docker compose -f deploy/docker-compose.yml run --rm --entrypoint sh scheduler \
    -c 'printf %s "$IMAP_PASSWORD" | sha256sum'

# On the laptop, from 1Password (adjust the field reference to the item):
op read "op://Personal/<your-imap-item>/password" | tr -d '\n' | sha256sum
```

The two digests must match. Repeat for `ANTHROPIC_API_KEY`. Do this **before** Gate D — a corrupted key surfaces there as an opaque auth failure.

### 3.4 Create the bind-mount directories *before* the first `up -d`

Docker creates a missing bind-mount source as an empty directory owned by `root:root`. The container runs as uid 1000 and cannot write to it — and a store-write failure dead-letters receipts exactly like a render gap does. Create them first:

```bash
mkdir -p data/receipts data/backups data/mcp-logs rclone-config
chown -R 1000:1000 data/receipts data/backups data/mcp-logs rclone-config   # sudo if not your uid
chmod 700 rclone-config                                        # holds a standing Dropbox refresh token
```

`chmod 700` on `rclone-config/` is not decoration: `rclone.conf` contains a long-lived Dropbox refresh token with write access to the mirror.

### 3.5 One-time rclone/Dropbox authorization

Follow **[deploy/rclone-setup.md](../../deploy/rclone-setup.md)**: create a dedicated Dropbox app (own client ID — the shared rclone ID is rate-limited during a bulk mirror), enable the four scopes *before* authorizing, run `rclone config` on the laptop, then `scp -r` the config **directory** to `~/receipt-index/rclone-config` and re-apply the ownership and mode above.

The mount is a whole directory, read-write, on purpose: Dropbox access tokens expire in about four hours and rclone refreshes them by writing a temp file and renaming it over `rclone.conf`. A `:ro` mount or a single-file bind mount breaks the rename and the mirror dies silently a few hours after a deploy that looked fine.

---

## 4. Build and migrate

### 4.1 Build the image on the server

```bash
docker compose -f deploy/docker-compose.yml build
```

This tags `receipt-index:latest` (the compose `image: receipt-index` key), which is what Gate A's tests look for by default.

### 4.2 Start only the database

```bash
docker compose -f deploy/docker-compose.yml up -d db
docker compose -f deploy/docker-compose.yml ps          # db must reach "healthy"
```

The scheduler stays down. **Starting the scheduler is what enables the schedule**, and that is the last thing that happens on launch day (see Gate G). Everything before then runs as one-off `docker compose run --rm` commands.

On first boot of an empty volume, `deploy/db-init/01-create-roles.sh` runs once and creates the `receipt_index` login role (owner of the database and of schema `public`) plus the three NOLOGIN grant-target roles the migrations reference by name (`receipt_index_dev_all` / `_write` / `_read`). It never runs again — a later role change is an `ALTER ROLE`, by hand.

### 4.3 Apply migrations — an explicit deploy step

Migrations are **not** run by the container. They are a deliberate host-side step, before the scheduler ever starts, and again after every upgrade.

Set `MIGRATION_DATABASE_URL` in `.env`. It connects over the loopback-published host port, and it **must already contain a `?` query parameter** — the Makefile appends `&x-migrations-table=...` to whatever you give it, so a URL without `?sslmode=disable` is malformed and golang-migrate fails to connect:

```
MIGRATION_DATABASE_URL=postgresql://receipt_index:<RECEIPT_INDEX_PASSWORD>@127.0.0.1:15432/receipt_index?sslmode=disable
```

**Run migrations as the `receipt_index` role, not as the superuser.** `receipt_index` owns the database and schema `public` (via `db-init`), and the cutover dump restores every object owned by `receipt_index`. If migrations run as `postgres`, a freshly built server ends up with schema `receipt` and its tables owned by the superuser while a restored server has them owned by the app role — two servers with divergent ownership from the same repo. The app role has everything it needs: `pg_trgm` is a *trusted* extension in Postgres 13+, so the database owner can `CREATE EXTENSION` without superuser.

> `deploy/example.env` ships this line with the `receipt_index` role already in place — migrations run as the database owner, not the superuser. The superuser credentials (`POSTGRES_PASSWORD`) are still needed separately for the cutover `pg_restore` (section 6).

Then:

```bash
make migrate-up
```

The Makefile auto-exports `.env`, downloads golang-migrate v4.18.3 into `.bin/` if absent, and applies both migration directories with separate version tables (`schema_migrations_public`, `schema_migrations_receipt`) — both directories start at `000001`, so a shared table would corrupt the state.

---

## 5. First-launch Go/No-Go gates

Run these in order. **Each is a hard stop**: do not proceed to the next gate until the current one passes.

The ordering is not ceremony. Failed items land in `ingest_log` and are then permanently skipped — the idempotency check is the union of `receipts.source_id` and `ingest_log.source_id`, and nothing ever retries a dead-lettered item. A launch-day environmental failure (missing browser, unwritable mount, bad credential) therefore does not present as an outage; it presents as **receipts that silently never appear**, one per message the scheduler touched while broken. So the 15-minute schedule on real mail is enabled **last**, after everything it depends on has been proven.

### Gate A — rendering works in the server-built image

Both render engines must work *in the image built on this server*, as uid 1000.

```bash
uv sync --all-extras
RECEIPT_INDEX_IMAGE=receipt-index:latest \
    uv run pytest tests/integration/test_docker_render.py -m integration --no-cov
```

**Pass:** all tests green. That covers Playwright/Chromium rendering an HTML receipt, weasyprint rendering a text receipt (which proves the pango/gdk-pixbuf system libraries are present), `PLAYWRIGHT_BROWSERS_PATH` resolving to the image's baked `/ms-playwright` even when the host value is poisoned, and `supercronic` / `rclone` / `pg_dump` all being on `PATH` — with `pg_dump` reporting version 18 (an older client cannot dump a v18 server). Every script asserts `os.getuid() == 1000` internally.

If the tests skip, the image tag is wrong — `docker image inspect receipt-index:latest`.

*Without uv on the server*, the minimum manual equivalent:

```bash
docker run --rm --shm-size=1g --entrypoint python receipt-index:latest -c '
import os
from receipt_index.renderer import _html_to_pdf_playwright
assert os.getuid() == 1000
pdf = _html_to_pdf_playwright("<html><body><h1>smoke</h1></body></html>")
assert pdf[:5] == b"%PDF-"
print("playwright OK", len(pdf))'
```

but prefer the test file — it is the version that stays in sync with the code.

### Gate B — platform

```bash
# 1. Compose file lints and every referenced variable resolves
docker compose -f deploy/docker-compose.yml config --quiet

# 2. Database is healthy
docker compose -f deploy/docker-compose.yml ps

# 3. pgdata is on the volume mounted at /var/lib/postgresql (NOT a subdirectory)
docker compose -f deploy/docker-compose.yml exec db \
    sh -c 'echo "$PGDATA"; ls -d "$PGDATA"'

# 4. Sentinel write as uid 1000 to both writable mounts
docker compose -f deploy/docker-compose.yml run --rm --entrypoint sh scheduler -c '
    id -u
    touch /app/data/receipts/.sentinel && rm /app/data/receipts/.sentinel
    touch /app/data/backups/.sentinel  && rm /app/data/backups/.sentinel
    touch /app/.rclone/.sentinel       && rm /app/.rclone/.sentinel
    echo ALL-MOUNTS-WRITABLE'
```

**Pass:** (1) prints nothing at all — any `variable is not set` warning is a broken `.env`; (2) `db` shows `healthy`; (3) `PGDATA` is `/var/lib/postgresql/18/docker`, i.e. *inside* the volume mounted at `/var/lib/postgresql` — if the volume were mounted at `/var/lib/postgresql/data` instead, the cluster would live outside the volume and vanish on the next container restart; (4) prints `1000` then `ALL-MOUNTS-WRITABLE`.

A host-side `ls -l` is **not** a substitute for step 4. Only a write from inside the container as uid 1000 proves the thing that matters.

### Gate C — schema

```bash
docker compose -f deploy/docker-compose.yml run --rm --entrypoint sh scheduler -c '
    psql "$DATABASE_URL" -c "SELECT '\''public'\'' AS dir, version, dirty FROM schema_migrations_public
                             UNION ALL
                             SELECT '\''receipt'\'', version, dirty FROM schema_migrations_receipt;"'
```

**Pass — record these numbers in your deploy notes:**

| Migration table | Expected version | dirty |
|---|---|---|
| `schema_migrations_public` | `1` | `f` |
| `schema_migrations_receipt` | `7` | `f` |

(As of 2026-08-01. A `dirty = t` means a migration aborted half-applied; fix it before anything else touches the database.)

Then prove the **application** read path — same role, same DSN, same container the scheduler will use:

```bash
docker compose -f deploy/docker-compose.yml run --rm --entrypoint receipt-index scheduler \
    search --vendor zzz-no-such-vendor --output json
```

**Pass:** exit 0 and `[]` on a pre-cutover (empty) database. This is the check that catches an ownership/grant lockout — the failure mode where the schema is perfect and the app still cannot read it.

### Gate D — environment and credentials, with zero writes

```bash
docker compose -f deploy/docker-compose.yml run --rm --entrypoint receipt-index scheduler \
    ingest --dry-run
```

One clean dry run proves three things at once: the config loader resolved every `${VAR}` in `deploy/receipt-index.yaml` (a missing one hard-fails and lists *all* of them), IMAP authenticated, and the Google Drive token refreshed — all with no database writes, no rendering, and therefore no dead-letter exposure.

> **Not a hang.** On an empty database the dry run enumerates the *entire* historical mailbox, because nothing is known yet. It is slow and extremely log-heavy. Let it finish. (This is the known O(N) IMAP enumeration; after cutover it is fast again because the union of known `source_id`s is populated.)

**Pass:** exits 0, having listed the candidate items.

### Gate E — cutover parity and DLQ baseline

Perform **[section 6](#6-cutover-from-the-dev-machine-one-time)** in full, including every verification query.

**Pass:** the distinct `source_id` union count on the server equals the value captured from the source at dump time; the per-status `ingest_log` breakdown is recorded in your deploy notes as the monitoring baseline; every `pdf_path` resolves to a file.

### Gate F — Dropbox

```bash
# Remote listing as uid 1000, through the mounted config directory
docker compose -f deploy/docker-compose.yml run --rm --entrypoint rclone scheduler \
    lsd dropbox: --config /app/.rclone/rclone.conf

# Initial bulk mirror (post-cutover: the full historical tree)
docker compose -f deploy/docker-compose.yml run --rm --entrypoint rclone scheduler \
    copy /app/data/receipts dropbox:receipt-index/receipts \
    --config /app/.rclone/rclone.conf -v

# Count parity
find data/receipts -type f | wc -l
docker compose -f deploy/docker-compose.yml run --rm --entrypoint rclone scheduler \
    size dropbox:receipt-index/receipts --config /app/.rclone/rclone.conf
```

**Pass:** `lsd` succeeds as uid 1000, the bulk copy completes with no `too_many_requests` errors in its output (if you see them, the remote is using rclone's shared client ID — redo `deploy/rclone-setup.md` step 1), and the Dropbox object count equals the server file count.

The bulk mirror is the real stress case, not the one-new-file case, and it must pass **before** the scheduler starts: `rclone copy` is additive and never deletes, so a mirror of a bad cutover does not self-clean.

### Gate G — supervised cycle, then the schedule

```bash
# 1. One supervised cycle, watched end to end
docker compose -f deploy/docker-compose.yml run --rm --entrypoint receipt-index scheduler \
    ingest --limit 1
```

Note the `--entrypoint receipt-index`: the service's entrypoint is `supercronic /app/crontab`, so a bare `run scheduler receipt-index ingest` would append the CLI as *arguments to supercronic*. Once the scheduler is running, `exec` is the more convenient form (`exec` ignores the entrypoint):

```bash
docker compose -f deploy/docker-compose.yml exec scheduler receipt-index ingest --limit 1
```

**Pass:** exit 0, a PDF appears under `data/receipts`, the corresponding `receipts` row exists, and `receipt-index failures` shows nothing new.

```bash
# 2. Enable the schedule — this starts supercronic and all four cron lines
docker compose -f deploy/docker-compose.yml up -d

# 3. Watch two automatic ticks (at :00/:15/:30/:45)
docker compose -f deploy/docker-compose.yml logs -f scheduler
```

**Pass, after two automatic ticks:**

- Zero duplicate `source_id` processing — the first-cycle duplicate query in [section 6.6](#66-verification-queries) still returns `0`.
- No `ingest_log` growth in `failed` or `skipped` beyond the recorded baseline.
- A sync tick (`:07/:22/:37/:52`) mirrors any new file to Dropbox.

While you are here, **record the exact log strings** for a non-zero ingest exit and for supercronic's "falling behind" warning — those literal strings are what the weekly log scan in [section 7](#7-weekly-monitoring) greps for.

```bash
# 4. Reboot test, against the final configuration
sudo reboot
# ... then, after it comes back:
docker compose -f deploy/docker-compose.yml ps        # both services up
docker compose -f deploy/docker-compose.yml logs --since 20m scheduler
```

**Pass:** both services return without human intervention and a scheduled tick fires. See [section 7](#7-weekly-monitoring) about the one expected failed tick immediately after a reboot.

---

## 6. Cutover from the dev machine (one-time)

Moves the existing receipt corpus — database rows *and* the rendered PDF tree — from the dev machine's native PostgreSQL 18 (port 5435, database `receipt_index`) to the server. Run it as Gate E, with the scheduler still down.

`ingest_log` is **load-bearing data, not "just a log"**: idempotency is the union of `receipts.source_id` and `ingest_log.source_id`. If only `receipts` made it across, every historically skipped or failed item would be reprocessed — LLM spend, and previously-skipped items may now insert under model drift.

No sequence resync is needed: there are no sequences, `serial`, or identity columns in either schema (UUID v7 defaults throughout).

### 6.1 Preconditions

- [ ] **The server database is empty.** If Gates B–D touched it (they do: `make migrate-up` creates the migration tables), drop and recreate before restoring — `pg_restore` into a schema that already exists collides on everything.
- [ ] **Proven, not assumed:** immediately before the drop, the server's `ingest_log` has **zero rows** (see 6.3). "The scheduler was never up" is a claim; zero rows is evidence.
- [ ] **The rsync target is empty.** `find data/receipts -type f | wc -l` on the server returns `0`. An additive rsync over leftovers strands orphan PDFs: filenames embed the LLM-extracted vendor, and extraction is not deterministic enough for names to collide cleanly.
- [ ] Gates A–D have passed.

### 6.2 Enforce the laptop stop

Not a habit — a lock. Rename the laptop's config so an absent-minded `receipt-index ingest` cannot connect to anything:

```bash
cd /path/to/receipt-index   # your laptop clone
mv .env .env.retired-$(date +%Y%m%d)
mv receipt-index.yaml receipt-index.yaml.retired-$(date +%Y%m%d)
```

Take the dump **strictly after** the last laptop run has completed.

Capture the source-of-truth number *from the source*, at dump time:

```bash
psql "postgresql://receipt_index@localhost:5435/receipt_index" -At -c "
    SELECT count(*) FROM (
        SELECT source_id FROM receipt.receipts
        UNION
        SELECT source_id FROM receipt.ingest_log
    ) AS known;"
```

Write that number down. It is the single strongest cutover check.

### 6.3 Dump

```bash
pg_dump --format=custom --no-password \
    --file=~/receipt-index-cutover-$(date +%Y%m%d).dump \
    "postgresql://receipt_index@localhost:5435/receipt_index"
```

**Retain this file. It is *the* rollback artifact.** If the cutover is aborted, re-restore from this same dump — never take a second dump (by then the laptop database may have drifted, and a fresh dump would launder the very state you are rolling back from).

Copy it to the server:

```bash
scp ~/receipt-index-cutover-*.dump receipt-server:~/
```

On the server, prove emptiness, then drop and recreate:

```bash
cd ~/receipt-index

# Evidence of "paused": both must be 0
docker compose -f deploy/docker-compose.yml run --rm --entrypoint sh scheduler -c '
    psql "$DATABASE_URL" -c "SELECT
        (SELECT count(*) FROM receipt.receipts)   AS receipts,
        (SELECT count(*) FROM receipt.ingest_log) AS ingest_log;"'

# Fresh, empty, owned by the app role.
# db-init only runs on first boot of an empty volume, so the database-level
# bits it applied must be re-applied by hand here. The roles are cluster-level
# and survive a DROP DATABASE.
docker compose -f deploy/docker-compose.yml exec db sh -c '
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres -c "DROP DATABASE $POSTGRES_DB;"
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres -c "CREATE DATABASE $POSTGRES_DB OWNER receipt_index;"
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -c "
        GRANT CONNECT ON DATABASE $POSTGRES_DB TO receipt_index_dev_all, receipt_index_dev_write, receipt_index_dev_read;
        ALTER SCHEMA public OWNER TO receipt_index;"'
```

### 6.4 Restore

```bash
docker compose -f deploy/docker-compose.yml cp \
    ~/receipt-index-cutover-<date>.dump db:/tmp/cutover.dump

docker compose -f deploy/docker-compose.yml exec db sh -c '
    pg_restore --exit-on-error --verbose \
        --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" /tmp/cutover.dump'
```

**Without `--no-owner` and without `--no-acl`** — both omissions are deliberate:

- `--no-owner` would leave every object owned by the superuser, and the application (connecting as `receipt_index`) would be running on objects it does not own. The dump's `ALTER ... OWNER TO receipt_index` statements succeed precisely because `db-init` pre-created that role.
- `--no-acl` would drop the column-level grants the migrations issue; the only ACLs in the dump target the three `receipt_index_dev_*` roles, which exist.

The dump also carries the `pg_trgm` extension and **both** `schema_migrations_*` tables at their source versions (public `1`, receipt `7` as of 2026-08-01), which is what makes the next step a no-op rather than a re-run.

```bash
make migrate-up
```

**Expect "no change".** If it applies anything, that is a genuine post-v7 migration added in the repo since the laptop last migrated — which is the correct ordering for it to be applied in.

Re-run the Gate C queries: same expected versions, `dirty = f`.

### 6.5 Files

```bash
# On the laptop — -c compares by checksum, not size+mtime
rsync -av -c --info=progress2 \
    /path/to/receipt-index/data/receipts/ \
    receipt-server:receipt-index/data/receipts/

# Counts must match
find /path/to/receipt-index/data/receipts -type f | wc -l   # laptop
ssh receipt-server 'find ~/receipt-index/data/receipts -type f | wc -l'             # server
```

Then on the server:

```bash
chown -R 1000:1000 data/receipts      # sudo if not your uid
```

The database stores paths **relative to the store root**, so the tree transplants with no database rewriting.

### 6.6 Verification queries

Run these **as the app role over the app DSN** — that is, inside the container, using `$DATABASE_URL`. Running them as the superuser would pass even when the application is locked out by ownership or grants, which is the exact failure class these are here to catch.

```bash
docker compose -f deploy/docker-compose.yml run --rm --entrypoint sh scheduler -c 'psql "$DATABASE_URL"'
```

**1. Distinct `source_id` union — the strongest single number.** Must equal the value captured in 6.2:

```sql
SELECT count(*) FROM (
    SELECT source_id FROM receipt.receipts
    UNION
    SELECT source_id FROM receipt.ingest_log
) AS known;
```

**2. Per-table counts** — compare against the laptop:

```sql
SELECT 'receipts' AS relation, count(*) FROM receipt.receipts
UNION ALL
SELECT 'ingest_log', count(*) FROM receipt.ingest_log;
```

**3. Per-status `ingest_log` breakdown — RECORD THIS. It is the monitoring baseline** for [section 7](#7-weekly-monitoring):

```sql
SELECT status, count(*) FROM receipt.ingest_log GROUP BY status ORDER BY status;
```

> `receipts` ≫ `ingest_log` is **expected**, not a defect: receipts ingested before the dead-letter log existed have no log rows at all. Do not "fix" it.

**4. Schema integrity** — a partially-errored restore can leave the counts right and the indexes silently missing:

```sql
SELECT extname, extversion FROM pg_extension WHERE extname = 'pg_trgm';

SELECT indexname FROM pg_indexes
WHERE schemaname = 'receipt' AND indexname = 'idx_receipts_vendor';
```

Both must return exactly one row (`idx_receipts_vendor` is the GIN trigram index on `vendor` that every `search --vendor` depends on).

**5. Referential file check** — every `pdf_path` resolves to a real file. Run it in the container, where the store is mounted:

```bash
docker compose -f deploy/docker-compose.yml run --rm --entrypoint sh scheduler -c '
    psql "$DATABASE_URL" -At -c "SELECT pdf_path FROM receipt.receipts" \
    | while IFS= read -r p; do
          [ -f "/app/data/receipts/$p" ] || echo "MISSING $p"
      done | tee /tmp/missing.txt | wc -l'
```

**Must be `0`.** Orphans (files with no row) are counted separately and are tolerable:

```bash
docker compose -f deploy/docker-compose.yml run --rm --entrypoint sh scheduler -c '
    psql "$DATABASE_URL" -At -c "SELECT pdf_path FROM receipt.receipts" | sort > /tmp/db.txt
    ( cd /app/data/receipts && find . -type f | sed "s|^\./||" ) | sort > /tmp/fs.txt
    echo "orphan files: $(comm -13 /tmp/db.txt /tmp/fs.txt | wc -l)"'
```

**6. First-cycle duplicate check** — run after Gate G's first automatic ticks. Duplicates cannot appear as double rows (`uq_receipts_source_id` forbids it); they appear as *fresh log rows for old items*. Substitute the cutover timestamp:

```sql
SELECT count(*)
FROM receipt.ingest_log l
JOIN receipt.receipts r ON r.source_id = l.source_id
WHERE l.created_at > TIMESTAMPTZ '2026-08-01 00:00:00+00'   -- cutover mark
  AND r.created_at < TIMESTAMPTZ '2026-08-01 00:00:00+00';
```

**Must be `0`.**

**7. End-to-end, from the laptop** — a known historical receipt is searchable and viewable through the SSH wrapper (see [section 9](#9-searching-from-the-laptop)):

```bash
scripts/receipt-search search --vendor "Southern California Edison"
scripts/receipt-search show <one-of-those-ids>
```

### 6.7 Finish

Proceed to Gate F (Dropbox bulk mirror, count-verified) and only then Gate G. Do not start the scheduler earlier: bringing it up activates *all four* cron lines at once — ingest, both mirrors, and the nightly backup. If you need the scheduler running with the mirror still off, comment out the two `rclone` lines in `deploy/crontab` locally (leave the edit uncommitted) and restore them after Gate F.

### 6.8 Abort criterion

**The point of no return is the first server cycle that ingests genuinely new mail.**

Before that point — rollback:

```bash
docker compose -f deploy/docker-compose.yml stop scheduler
# drop + recreate the database exactly as in 6.3, then re-restore
# from the SAME retained dump file (6.4). Never take a fresh dump.
```

Then restore the laptop's `.env` / `receipt-index.yaml` from their `.retired-*` names.

After that point, the laptop database is stale: recovery is a *reverse cutover* (dump the server, restore to the laptop), not a rollback. The Dropbox mirror is additive and needs no unwinding in either case.

### 6.9 Retiring the dev PostgreSQL

Keep the dev cluster's `receipt_index` database until the server has earned it. Explicit criterion — **7 consecutive days** of:

- zero growth in `failed` and `skipped` `ingest_log` counts beyond the baseline,
- no supercronic falling-behind warnings,
- Dropbox/server file-count parity holding.

Then, and only then:

```bash
# The ONLY irreversible step in this plan. Take a final dump first.
pg_dump --format=custom --file=~/receipt-index-final-$(date +%Y%m%d).dump \
    "postgresql://receipt_index@localhost:5435/receipt_index"
# Store it somewhere durable, verify it is non-empty, and only then:
psql -p 5435 -c 'DROP DATABASE receipt_index;'
```

---

## 7. Weekly monitoring

Solo-operator routine. Five minutes, once a week.

### 7.1 Per-status `ingest_log` breakdown vs. the recorded baseline

The important one, and the reason it is not just `receipt-index failures`:

**`receipt-index failures` filters `status = 'failed'` only.** A receipt the LLM scored below the confidence threshold is logged as **`skipped`**, the cycle exits **0**, nothing appears in `failures` — and the item is dead-lettered forever, because idempotency counts `ingest_log` rows regardless of status. That is the silent half of the data-loss surface, and it has real precedent in this project (the `amount: 0` saga; payment confirmations not recognized as receipts).

```bash
docker compose -f deploy/docker-compose.yml exec scheduler sh -c '
    psql "$DATABASE_URL" -c "SELECT status, count(*) FROM receipt.ingest_log
                             GROUP BY status ORDER BY status;"'
```

Compare all three numbers against the Gate E baseline. `success` growing is normal. **`skipped` or `failed` growing needs eyeballs:**

```bash
docker compose -f deploy/docker-compose.yml exec scheduler sh -c '
    psql "$DATABASE_URL" -c "
        SELECT created_at, source_type, source_id, email_sender, email_subject, error_message
        FROM receipt.ingest_log
        WHERE status = '\''skipped'\''
          AND created_at > NOW() - INTERVAL '\''8 days'\''
        ORDER BY created_at DESC;"'
```

Read the senders and subjects. A genuine non-receipt is fine. A real receipt in that list means the extraction threshold or prompt needs work — and recovering it requires deleting its `ingest_log` row (see [section 8.2](#82-dlq-recovery)).

Swap `'skipped'` for `'failed'` for the failure side, where `error_message` is populated.

*Future work:* a `receipt-index failures --status skipped` flag, so this stops being a raw SQL step.

### 7.2 Log scan

```bash
docker compose -f deploy/docker-compose.yml logs --since 168h scheduler \
    | grep -Ei 'level=error|job failed|behind|Traceback'
```

Grep for the exact strings recorded during Gate G; the pattern above is the broad net.

Two things to know:

- **One failed ingest tick immediately after every reboot is expected.** Container restart policies do not honour `depends_on` health ordering, so the scheduler can fire its first tick while Postgres is still starting. The run aborts on connection failure *before* any item is processed, so it writes no `ingest_log` rows and dead-letters nothing; the next tick self-heals. A *second* consecutive failure is not expected.
- **Falling-behind warnings** mean a cycle outran its 15-minute interval. supercronic's no-overlap guarantee holds (it will not start an overlapping run), so nothing is corrupted — but this is the early-warning signal for the O(N) mailbox enumeration issue, and the cue to revisit the cadence.

### 7.3 Dropbox freshness

From any device, no SSH: check the newest file under `receipt-index/receipts` in Dropbox. This is the free end-to-end delivery check — it exercises ingest, rendering, the store, and the mirror in one look.

Read it correctly: a fresh file **confirms** delivery. The *absence* of new files is not a health signal, because it is the normal state during a quiet week. Only chase it when you know receipts arrived in the window.

Also glance at `receipt-index/backups`: seven files, day-of-week names, the newest dated last night.

### 7.4 Disk

```bash
df -h /
du -sh ~/receipt-index/data/*
```

Container logs are capped (10 MB × 7 files per service) and the backup directory is bounded at seven dumps by day-of-week rotation, so growth should be dominated by the receipt tree and pgdata.

### 7.5 Future work

A dead-man's-snitch (healthchecks.io-style) ping as a fourth crontab line after each successful cycle. Absence-of-ping covers crash-loop, falling-behind, and config breakage in one mechanism — and unlike everything above, it pages *you* instead of waiting to be checked.

---

## 8. Routine operations

### 8.1 Upgrade

```bash
cd ~/receipt-index
git pull
docker compose -f deploy/docker-compose.yml build

# Stop the scheduler BEFORE `up -d` recreates it.
# Recreation kills the container; a cycle interrupted between writing a PDF and
# committing its row leaves an orphan file behind. Check for an in-flight cycle
# first — ticks land on :00/:15/:30/:45 and normally finish in well under a
# minute, so there is a wide idle window.
docker compose -f deploy/docker-compose.yml exec scheduler pgrep -af 'receipt-index ingest' || echo "idle"
docker compose -f deploy/docker-compose.yml stop scheduler

# Migrations before the new code runs (the db service stays up throughout)
make migrate-up

docker compose -f deploy/docker-compose.yml up -d
docker compose -f deploy/docker-compose.yml logs -f scheduler
```

Rebuild the image on **every** `playwright` version bump — the browser build is pinned per Playwright release, and a mismatch between the pip package and the baked browser breaks HTML rendering (and therefore dead-letters receipts).

After an upgrade that touched the renderer, the Dockerfile, or the config, re-run Gates A–D before walking away.

### 8.2 DLQ recovery

Dead-lettering is permanent by design: a `source_id` present in `ingest_log` is never looked at again, whatever its status. Recovery is deleting the log row so the item becomes unknown again.

Only worth doing for **environmental** causes — a rendering gap, a permissions problem, a network blip, a corrupted credential. Content failures (the LLM genuinely cannot read the document) will simply fail again, and re-running them costs Anthropic spend for nothing.

1. **Fix the cause first.** Re-run the relevant gate to prove it.

2. **Check for an orphaned PDF for that `source_id`.** A failure between rendering and the database write leaves a file with no row; leaving it behind means a duplicate PDF after the retry succeeds:

   ```bash
   docker compose -f deploy/docker-compose.yml exec scheduler sh -c '
       psql "$DATABASE_URL" -c "
           SELECT source_id, email_subject, error_message, created_at
           FROM receipt.ingest_log WHERE status = '\''failed'\''
           ORDER BY created_at DESC;"'
   # then look for a plausibly-matching file under the vendor/date path:
   ls -l data/receipts/<YYYY>/<MM>/
   ```

3. **Delete the log rows to force a retry.** Scope it — never `DELETE FROM receipt.ingest_log WHERE status = 'failed'` wholesale, which would also resurrect genuine content failures:

   ```sql
   -- Preview first
   SELECT source_id, email_subject, error_message
   FROM receipt.ingest_log
   WHERE status = 'failed'
     AND created_at >= TIMESTAMPTZ '2026-08-01 00:00:00+00'
     AND error_message LIKE '%<the environmental error substring>%';

   -- Then delete exactly those
   DELETE FROM receipt.ingest_log
   WHERE status = 'failed'
     AND created_at >= TIMESTAMPTZ '2026-08-01 00:00:00+00'
     AND error_message LIKE '%<the environmental error substring>%';
   ```

   The same applies to `status = 'skipped'` rows for receipts that should have been recognized.

4. The next scheduled cycle picks them up. Watch it, then re-check the per-status breakdown.

### 8.3 Disaster recovery from Dropbox

The full-loss story: the server is gone and Dropbox holds everything — the nightly dumps under `receipt-index/backups` and the complete receipt tree under `receipt-index/receipts`.

1. Rebuild the host per [section 2](#2-server-prerequisites), then provision per [section 3](#3-provisioning) (clone, `.env` from 1Password, directories with uid 1000, rclone config).

2. `docker compose -f deploy/docker-compose.yml build && docker compose -f deploy/docker-compose.yml up -d db`. First boot of the empty volume runs `db-init`, creating the `receipt_index` owner role and the grant-target roles — this must happen **before** the restore, or the dump's `OWNER TO` statements fail and the app is locked out.

3. Pull both trees back:

   ```bash
   docker compose -f deploy/docker-compose.yml run --rm --entrypoint rclone scheduler \
       copy dropbox:receipt-index/backups /app/data/backups \
       --config /app/.rclone/rclone.conf -v

   docker compose -f deploy/docker-compose.yml run --rm --entrypoint rclone scheduler \
       copy dropbox:receipt-index/receipts /app/data/receipts \
       --config /app/.rclone/rclone.conf -v

   ls -l data/backups/          # pick the newest receipt_index-<dow>.dump
   chown -R 1000:1000 data      # sudo if not your uid
   ```

4. Restore into the fresh, empty database — again **without** `--no-owner` and **without** `--no-acl`:

   ```bash
   docker compose -f deploy/docker-compose.yml cp \
       data/backups/receipt_index-<dow>.dump db:/tmp/restore.dump
   docker compose -f deploy/docker-compose.yml exec db sh -c '
       pg_restore --exit-on-error --verbose \
           --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" /tmp/restore.dump'
   make migrate-up      # no-op, or applies a delta newer than the dump
   ```

5. If the MCP server was set up, recreate its read-only role: all of [10.1](#101-one-time-setup) step 2, with the password already in `RECEIPT_INDEX_MCP_DATABASE_URL` in the restored `.env`. Then run its check, step 5.

6. Re-run Gates A–D and the verification queries in [6.6](#66-verification-queries), then Gate G. Expect to lose at most one day of ingest (the window between the last nightly dump and the loss); those messages are still in the mailbox and will be re-ingested on the next cycles, since their `source_id`s went down with the database.

---

## 9. Searching from the laptop

`scripts/receipt-search` runs the CLI inside the server's scheduler container over SSH and passes every argument straight through:

```bash
scripts/receipt-search search --vendor "Acme Parking (U.S.), LLC"
scripts/receipt-search search --date-from 2026-01-01 --date-to 2026-03-31 --output json
scripts/receipt-search show 019537f1-...-a3
scripts/receipt-search failures
scripts/receipt-search --help
```

Symlink it onto your `PATH` if you use it often:

```bash
ln -s /path/to/receipt-index/scripts/receipt-search ~/bin/receipt-search
```

Environment overrides:

| Variable | Default | Purpose |
|---|---|---|
| `RECEIPT_INDEX_SSH_HOST` | `receipt-server` | SSH target (hostname or `~/.ssh/config` alias) |
| `RECEIPT_INDEX_COMPOSE_PROJECT` | `receipt-index` | Compose project name |
| `RECEIPT_INDEX_SERVICE` | `scheduler` | Service to exec into |

Two details worth knowing when it misbehaves:

- It targets the container by **compose project name** (`docker compose -p receipt-index exec`), not by compose-file path, so it does not care where the repo was cloned on the server. The price is that the project must be **up** — which is the normal state under `restart: unless-stopped`. `no such service: scheduler` means the service is down; SSH in and check `docker compose -f deploy/docker-compose.yml ps`.
- Arguments are re-quoted client-side before being handed to SSH, because SSH flattens its argument list into a single string that the remote shell re-parses. That is what keeps `--vendor "Acme Parking (U.S.), LLC"` intact as one argument.

Anything the CLI can do is available this way, including `ingest`, but leave scheduled ingest to supercronic — a manual run that overlaps a scheduled tick is not protected by the no-overlap guarantee (which is per cron line, inside the scheduler).

## 10. Read-only MCP server

`receipt-index mcp` serves the index to MCP clients (agents) over stdio, read-only. See [CLI Reference: mcp](cli-reference.md#mcp) for the tools and for registering it with a client. In production the client launches it **as the account that holds the production credentials**: it SSHes in as the deploy user and execs into the scheduler container, the same route `scripts/receipt-search` takes.

```bash
ssh receipt-server docker compose -p receipt-index exec -T scheduler receipt-index mcp
```

The server refuses to start unless its database role is read-only. The only login role db-init creates is `receipt_index`, which owns everything, so production needs a one-time setup first.

### 10.1 One-time setup

**1. Create the access-log directory.** Fetches are logged to `data/mcp-logs`, a separate bind mount. It is deliberately outside `data/receipts` and `data/backups`, because the crontab copies both of those to Dropbox and a fetch log has no business leaving the host. Skip this if you provisioned with the current [3.4](#34-create-the-bind-mount-directories-before-the-first-up--d).

```bash
cd ~/receipt-index
mkdir -p data/mcp-logs
chown 1000:1000 data/mcp-logs   # sudo if not your uid
```

**2. Create a read-only login role.** Create it as a member of `receipt_index_dev_read`, which the migrations grant `SELECT` on both tables and nothing else. Open psql in the db container, then run the statements and type the password at the `\password` prompt, so it never appears in a command line or the shell history:

```bash
docker compose -f deploy/docker-compose.yml exec db sh -c 'psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB"'
```

```sql
CREATE ROLE receipt_index_mcp LOGIN;
GRANT receipt_index_dev_read TO receipt_index_mcp;
GRANT CONNECT ON DATABASE :"DBNAME" TO receipt_index_mcp;
\password receipt_index_mcp
\q
```

Generate the password from an alphanumeric alphabet (`openssl rand -hex 24`), as for the other Postgres passwords ([3.3](#33-the--in-secrets-hazard--read-before-pasting-a-password)). The role is cluster-level. It survives a `DROP DATABASE` but its `CONNECT` grant doesn't, so after recreating the database in 6.3, run the `GRANT CONNECT` line again. Disaster recovery (8.3) starts a new cluster, and `pg_dump` doesn't carry roles, so there the role is gone: redo all of this step, setting the password already in the restored `.env`.

**3. Add two lines to the clone-root `.env`:**

```bash
RECEIPT_INDEX_MCP_DATABASE_URL=postgresql://receipt_index_mcp:<password>@db:5432/receipt_index  # pragma: allowlist secret
RECEIPT_INDEX_MCP_ACCESS_LOG=/app/data/mcp-logs/mcp-access.jsonl
```

Use your `POSTGRES_DB` as the database name if it isn't `receipt_index`.

**4. Recreate the scheduler.** Compose reads `env_file` only when it creates the container, so `exec` won't see the new lines until you recreate it. As in [8.1](#81-upgrade), stop the scheduler between ingest cycles first:

```bash
docker compose -f deploy/docker-compose.yml exec scheduler pgrep -af 'receipt-index ingest' || echo "idle"
docker compose -f deploy/docker-compose.yml stop scheduler
docker compose -f deploy/docker-compose.yml up -d scheduler
```

**5. Check it.** Run the startup check with stdin closed, so the server starts and exits at once:

```bash
docker compose -f deploy/docker-compose.yml exec -T scheduler receipt-index mcp </dev/null; echo "exit=$?"
```

**Pass:** `MCP server ready on stdio (access log: /app/data/mcp-logs/mcp-access.jsonl)` and `exit=0`. `Error: the database role can write (...)` means it is still connecting as `receipt_index`: the variable is unset in the container (step 4) or the URL names the wrong role. `lacks SELECT` means the role isn't a member of `receipt_index_dev_read`, or the migrations' grants are missing.

### 10.2 The access log

One JSON line per `get_receipt_file` call: timestamp, receipt ID, stored path, size, whether content was returned, the outcome, and the client's name and version. If the log can't be written, the server withholds the file. The log is small and isn't rotated. Read it with:

```bash
tail -n 20 data/mcp-logs/mcp-access.jsonl
```
