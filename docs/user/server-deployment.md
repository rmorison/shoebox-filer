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
├── .env                         <- the scheduler's secrets (git-ignored, chmod 600)
├── .env.db                      <- the owner and superuser credentials (git-ignored, chmod 600)
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
    ├── example.env.db
    └── rclone-setup.md
```

Compose resolves relative paths against the directory holding the compose file, so `deploy/docker-compose.yml` refers to the state at the clone root with a `../` prefix (`../.env`, `../.env.db`, `../data/receipts`, `../data/backups`, `../rclone-config`) and to its own siblings with `./`. That is why `.env` and `.env.db` belong at the **clone root** and not in `deploy/`.

The two env files keep the database owner's credentials out of the application container. Only the `db` service and the host-side `make migrate-up` read `.env.db`; the scheduler reads `.env`, which holds no owner or superuser credential. Anyone with a shell in the scheduler sees only the application's logins.

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

### Rootless Docker

The stack also runs under [rootless Docker](https://docs.docker.com/engine/security/rootless/), where the daemon runs as the deploy user rather than as root. Three things change.

- **The daemon is a user service.** It replaces the system `docker` unit and the `docker` group above:

  ```bash
  systemctl --user enable --now docker
  systemctl --user is-enabled docker   # must print "enabled"
  ```

  Point the CLI at it with the `rootless` context rather than a `DOCKER_HOST` set in a shell startup file, which an SSH command such as the search wrapper may not read. Check from the laptop:

  ```bash
  ssh receipt-server docker context use rootless
  ssh receipt-server docker info --format '{{.SecurityOptions}}'   # must include name=rootless
  ```

- **Linger, for reboot survival.** Without it, the user's services, the daemon among them, start only when the user logs in and stop when the last session ends. With it, they start at boot:

  ```bash
  sudo loginctl enable-linger "$USER"
  loginctl show-user "$USER" --property=Linger   # must print "Linger=yes"
  ```

- **Bind-mount ownership.** Container uids map to subordinate host uids, so the container's uid 1000 isn't host uid 1000, and a host-side `chown 1000:1000` gives the directories the wrong owner. Container root is your host user, though, so chown from inside a container. Do this in place of the `chown` and `chmod` in [3.4](#34-create-the-bind-mount-directories-before-the-first-up--d), once [3.5](#35-one-time-rclonedropbox-authorization) has put the rclone config in place:

  ```bash
  cd ~/receipt-index
  docker run --rm -v "$PWD:/w" -w /w busybox \
      chown -R 1000:1000 data/receipts data/backups data/mcp-logs rclone-config
  docker run --rm -v "$PWD:/w" -w /w busybox chmod 700 rclone-config
  ```

  Afterwards your host user can't write into these directories directly, so later changes, such as a new rclone config, go through a container the same way. Gate B's sentinel step proves the result. The same applies to every later `chown 1000:1000` in this guide (6.5, 8.3 and 10.1): run it in the container form. To write into a directory from the host, as the cutover rsync in 6.5 does, hand it to container root first, which is your host user, then back:

  ```bash
  docker run --rm -v "$PWD:/w" -w /w busybox chown -R 0:0 data/receipts
  # ... host-side writes ...
  docker run --rm -v "$PWD:/w" -w /w busybox chown -R 1000:1000 data/receipts
  ```

  The app config is a single read-only file mount, so it needs only to be world-readable ([3.2b](#32b-create-the-server-app-config)).

---

## 3. Provisioning

### 3.1 Clone

```bash
git clone https://github.com/rmorison/shoebox-filer.git ~/receipt-index
cd ~/receipt-index
```

### 3.2 Create `.env` and `.env.db` at the clone root

```bash
(umask 077; cp deploy/example.env .env; cp deploy/example.env.db .env.db)
```

Two files, so that the owner's credentials never reach the application container:

- **`.env.db`** ([`deploy/example.env.db`](../../deploy/example.env.db)): the superuser (`POSTGRES_*`), the three login passwords and `MIGRATION_DATABASE_URL`. Read by the `db` service and by the Makefile, on the host.
- **`.env`** ([`deploy/example.env`](../../deploy/example.env)): the application's URLs, which log in as `receipt_index_write` and `receipt_index_read`, and its other secrets. Read by the scheduler and by the Makefile.

No variable belongs in both. Never put a `POSTGRES_*`, `RECEIPT_INDEX_*PASSWORD` or `MIGRATION_DATABASE_URL` line in `.env`: Gate B's step 5 checks for them.

Real values come from 1Password (both files are git-ignored and never committed). Store both files there:

| Variable | File | Source |
|---|---|---|
| `POSTGRES_PASSWORD` | `.env.db` | Generate: `openssl rand -hex 24` |
| `RECEIPT_INDEX_PASSWORD` | `.env.db` | Generate: `openssl rand -hex 24` — the owner login `receipt_index`; must match the password inside `MIGRATION_DATABASE_URL` |
| `RECEIPT_INDEX_WRITE_PASSWORD` | `.env.db` | Generate: `openssl rand -hex 24` — the application's login `receipt_index_write`; must match the password inside `DATABASE_URL` in `.env` |
| `RECEIPT_INDEX_READ_PASSWORD` | `.env.db` | Generate: `openssl rand -hex 24` — the read login `receipt_index_read`; must match the password inside `RECEIPT_INDEX_READ_DATABASE_URL` and `RECEIPT_INDEX_MCP_DATABASE_URL` in `.env` |
| `IMAP_PASSWORD` | `.env` | 1Password — your IMAP mailbox item |
| `ANTHROPIC_API_KEY` | `.env` | 1Password — your Anthropic API key item |
| `GDRIVE_TOKEN_JSON` | `.env` | Output of `receipt-index auth gdrive --client-credentials <client_secret.json>`, run on the laptop |

### 3.2b Create the server app config

```bash
cp deploy/example.receipt-index.yaml deploy/receipt-index.yaml
```

Edit the copy (it is git-ignored) with your real source definitions: IMAP host, username, and folder, and the Google Drive folder id. Secrets stay as `${VAR}` references into `.env`.

On [rootless Docker](#rootless-docker), the container's uid 1000 isn't your host user and reads this file as "other", so make it world-readable. It holds no secrets:

```bash
chmod 644 deploy/receipt-index.yaml
```

`GDRIVE_CREDENTIALS_JSON` is **not** required on the server. The template deliberately drops that key from the gdrive source, and the OAuth client id/secret needed for refresh already live inside `GDRIVE_TOKEN_JSON`. (The dev config may still interpolate it; do not copy that line over.)

Also do **not** set `PLAYWRIGHT_BROWSERS_PATH` in the server `.env`. The image bakes `/ms-playwright`; the dev value (`.playwright`) is a relative path that does not exist in the container, and if it leaks in, every HTML receipt fails to render and is permanently dead-lettered.

### 3.3 The `$`-in-secrets hazard — read before pasting a password

Docker Compose performs variable expansion on values inside an `env_file`, `.env` and `.env.db` alike. **A secret containing `$` is silently corrupted.** Verified behaviour: a value written as

```
IMAP_PASSWORD=ab$wcd$$ef
```

is delivered to the container as `ab$ef` — `$wcd` expands to the empty string (compose only mutters `The "wcd" variable is not set` on stderr) and `$$` collapses to a literal `$`. Authentication then fails with an error that says nothing about the real cause. `make`, which also reads both files, has the same hazard.

Rules:

- Escape every literal dollar sign by doubling it: `ab$wcd` → `ab$$wcd`.
- Applies to `IMAP_PASSWORD`, `ANTHROPIC_API_KEY`, every Postgres password, and the passwords embedded in the `*_DATABASE_URL` variables.
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

On [rootless Docker](#rootless-docker), run only the `mkdir` here, and set ownership and mode from inside a container after 3.5.

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

First check that `.env.db` sets all three login passwords. db-init needs them, and it runs only once (below):

```bash
grep -c -E '^RECEIPT_INDEX_(|WRITE_|READ_)PASSWORD=.' .env.db   # must print 3
```

```bash
docker compose -f deploy/docker-compose.yml up -d db
docker compose -f deploy/docker-compose.yml ps          # db must reach "healthy"
```

The scheduler stays down. **Starting the scheduler is what enables the schedule**, and that is the last thing that happens on launch day (see Gate G). Everything before then runs as one-off `docker compose run --rm` commands.

On first boot of an empty volume, `deploy/db-init/01-create-roles.sh` runs once. It creates the three NOLOGIN grant-target roles the migrations reference by name (`receipt_index_dev_all` / `_write` / `_read`) and three logins, one per level of the Database Standards ([8.4](#84-separate-database-logins)):

- `receipt_index`, the owner of the database and of schema `public`, for migrations only;
- `receipt_index_write`, the application's login, a member of `receipt_index_dev_write`;
- `receipt_index_read`, for backups, forensic queries and the MCP server, a member of `receipt_index_dev_read`.

It never runs again — a later role change is an `ALTER ROLE`, by hand. If it fails, for example on a missing password, the `db` logs say so, and the volume is left initialized without the roles: on a **new, empty** server only, remove it with `docker compose -f deploy/docker-compose.yml down -v`, fix `.env.db`, and start again.

### 4.3 Apply migrations — an explicit deploy step

Migrations are **not** run by the container. They are a deliberate host-side step, before the scheduler ever starts, and again after every upgrade.

Set `MIGRATION_DATABASE_URL` in `.env.db`, never in `.env`, which the scheduler reads. It connects over the loopback-published host port, and it **must already contain a `?` query parameter** — the Makefile appends `&x-migrations-table=...` to whatever you give it, so a URL without `?sslmode=disable` is malformed and golang-migrate fails to connect:

```
MIGRATION_DATABASE_URL=postgresql://receipt_index:<RECEIPT_INDEX_PASSWORD>@127.0.0.1:15432/receipt_index?sslmode=disable
```

**Run migrations as the `receipt_index` role, not as the superuser.** `receipt_index` owns the database and schema `public` (via `db-init`), and the cutover dump restores every object owned by `receipt_index`. If migrations run as `postgres`, a freshly built server ends up with schema `receipt` and its tables owned by the superuser while a restored server has them owned by `receipt_index` — two servers with divergent ownership from the same repo. The owner has everything it needs: `pg_trgm` is a *trusted* extension in Postgres 13+, so the database owner can `CREATE EXTENSION` without superuser.

> `deploy/example.env.db` ships this line with the `receipt_index` role already in place — migrations run as the database owner, not the superuser. The superuser credentials (`POSTGRES_PASSWORD`) are still needed separately for the cutover `pg_restore` (section 6).

Then:

```bash
make migrate-up
```

The Makefile auto-exports `.env` and then `.env.db`, downloads golang-migrate v4.18.3 into `.bin/` if absent, and applies both migration directories with separate version tables (`schema_migrations_public`, `schema_migrations_receipt`) — both directories start at `000001`, so a shared table would corrupt the state.

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

# 4. Sentinel write as uid 1000 to every writable mount
docker compose -f deploy/docker-compose.yml run --rm --entrypoint python scheduler -c '
import os, pathlib
print(os.getuid())
for d in ("/app/data/receipts", "/app/data/backups", "/app/data/mcp-logs", "/app/.rclone"):
    sentinel = pathlib.Path(d, ".sentinel")
    sentinel.touch()
    sentinel.unlink()
print("ALL-MOUNTS-WRITABLE")'

# 5. The scheduler's environment holds no owner or superuser credential.
#    Prints variable names only, never values.
docker compose -f deploy/docker-compose.yml run --rm --entrypoint sh scheduler -c \
    'found=$( { env | cut -d= -f1 | grep -xE "MIGRATION_DATABASE_URL|POSTGRES_(USER|PASSWORD)|RECEIPT_INDEX_(|WRITE_|READ_)PASSWORD"; env | grep -E "://(receipt_index|postgres)[:@]" | cut -d= -f1; } | sort -u | tr "\n" " "); echo "owner credentials: ${found:-none}"'
```

**Pass:** (1) prints nothing at all — any `variable is not set` warning is a broken `.env` or `.env.db`, and an `env file ... not found` error a missing one; (2) `db` shows `healthy`; (3) `PGDATA` is `/var/lib/postgresql/18/docker`, i.e. *inside* the volume mounted at `/var/lib/postgresql` — if the volume were mounted at `/var/lib/postgresql/data` instead, the cluster would live outside the volume and vanish on the next container restart; (4) prints `1000` then `ALL-MOUNTS-WRITABLE`; a `PermissionError` names the mount that isn't writable; (5) prints exactly `owner credentials: none`. Any name it prints is in `.env` and belongs in `.env.db` ([3.2](#32-create-env-and-envdb-at-the-clone-root)); a `DATABASE_URL` it names logs in as the owner, not as `receipt_index_write`.

A host-side `ls -l` is **not** a substitute for step 4. Only a write from inside the container as uid 1000 proves the thing that matters.

### Gate C — schema

```bash
docker compose -f deploy/docker-compose.yml run --rm --entrypoint sh scheduler -c '
    psql "$RECEIPT_INDEX_READ_DATABASE_URL" -c "SELECT '\''public'\'' AS dir, version, dirty FROM schema_migrations_public
                             UNION ALL
                             SELECT '\''receipt'\'', version, dirty FROM schema_migrations_receipt;"'
```

**Pass — record these numbers in your deploy notes:**

| Migration table | Expected version | dirty |
|---|---|---|
| `schema_migrations_public` | the highest number in `db/migrations/public` (`1`) | `f` |
| `schema_migrations_receipt` | the highest number in `db/migrations/receipt` (`14` as of the separate database logins) | `f` |

(A `dirty = t` means a migration aborted half-applied; fix it before anything else touches the database.)

`make migrate-up` never applies a migration numbered at or below the version a table records, and says nothing about it. A migration added to the repository after a higher-numbered one has been applied is skipped for good, so every new migration takes a number above every applied one.

The query runs as the read login, the one the nightly backup uses, so it also proves that login can read the version tables, which `pg_dump` needs. `permission denied for table schema_migrations_public` means migration 000014 hasn't run.

Then prove the **application** read path — the application's login (`receipt_index_write`), same DSN, same container the scheduler will use:

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

**Pass:** both services return without human intervention and a scheduled tick fires. On [rootless Docker](#rootless-docker) this needs linger. See [section 7](#7-weekly-monitoring) about the one expected failed tick immediately after a reboot.

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

# Fresh, empty, owned by receipt_index.
# db-init only runs on first boot of an empty volume, so the database-level
# bits it applied must be re-applied by hand here. The roles are cluster-level
# and survive a DROP DATABASE.
docker compose -f deploy/docker-compose.yml exec db sh -c '
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres -c "DROP DATABASE $POSTGRES_DB;"
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres -c "CREATE DATABASE $POSTGRES_DB OWNER receipt_index;"
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -c "
        GRANT CONNECT ON DATABASE $POSTGRES_DB TO receipt_index_dev_all, receipt_index_dev_write, receipt_index_dev_read, receipt_index_write, receipt_index_read;
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

- `--no-owner` would leave every object owned by the superuser, so migrations (connecting as `receipt_index`) could no longer alter them. The dump's `ALTER ... OWNER TO receipt_index` statements succeed precisely because `db-init` pre-created that role.
- `--no-acl` would drop the column-level grants the migrations issue, and with them every privilege the write and read logins have; the only ACLs in the dump target the three `receipt_index_dev_*` roles, which exist.

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

On [rootless Docker](#rootless-docker), hand `data/receipts` to your host user before the rsync above, and use the container form of this `chown` after it.

The database stores paths **relative to the store root**, so the tree transplants with no database rewriting.

### 6.6 Verification queries

Run these **as the application's login over the app DSN** — that is, inside the container, using `$DATABASE_URL`. Running them as the superuser would pass even when the application is locked out by ownership or grants, which is the exact failure class these are here to catch.

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
    ( cd /app/data/receipts && find . -path ./originals -prune -o -type f ! -name ".put.*.tmp" -print | sed "s|^\./||" ) | sort > /tmp/fs.txt
    echo "orphan files: $(comm -13 /tmp/db.txt /tmp/fs.txt | wc -l)"'
```

The count leaves out temp files, and `originals/`, whose files aren't renditions and so never appear in `pdf_path`. The store writes each file to a `.put.<random>.tmp` name in its folder and links it into place, so a temp file is left behind only when an ingest is killed mid-write. Nothing references one, and the Dropbox mirror skips them. Delete any older than an hour, when no write can still be using it:

```bash
docker compose -f deploy/docker-compose.yml run --rm --entrypoint sh scheduler -c '
    find /app/data/receipts -name ".put.*.tmp" -mmin +60 -print -delete'
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
    psql "$RECEIPT_INDEX_READ_DATABASE_URL" -c "SELECT status, count(*) FROM receipt.ingest_log
                             GROUP BY status ORDER BY status;"'
```

Compare all three numbers against the Gate E baseline. `success` growing is normal. **`skipped` or `failed` growing needs eyeballs:**

```bash
docker compose -f deploy/docker-compose.yml exec scheduler sh -c '
    psql "$RECEIPT_INDEX_READ_DATABASE_URL" -c "
        SELECT created_at, source_type, source_id, email_sender, email_subject, error_message
        FROM receipt.ingest_log
        WHERE status = '\''skipped'\''
          AND created_at > NOW() - INTERVAL '\''8 days'\''
        ORDER BY created_at DESC;"'
```

Read the senders and subjects. A genuine non-receipt is fine. A real receipt in that list means the extraction threshold or prompt needs work, and it is recovered with `receipt-index retry` (see [section 8.2](#82-dlq-recovery)).

Swap `'skipped'` for `'failed'` for the failure side, where `error_message` is populated. The counts are of records, not items: an item that failed and was retried has a record for each attempt.

The same lists, one line per item, come from the CLI:

```bash
docker compose -f deploy/docker-compose.yml exec scheduler \
    receipt-index failures --status skipped --since <date a week ago>
docker compose -f deploy/docker-compose.yml exec scheduler \
    receipt-index failures --status terminal
```

`--status retrying` lists failures still waiting for an automatic retry. Those clear themselves; `terminal` ones don't.

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

The first upgrade to a release that reads `.env.db` needs that file before `up -d`: follow [8.5](#85-split-the-owner-credentials-out-of-env) instead of this section, once. If the scheduler is stopped and the schedule must stay off, change the commands below as [8.6](#86-with-the-scheduler-stopped) says.

```bash
cd ~/receipt-index
git pull
docker compose -f deploy/docker-compose.yml build

# Stop the scheduler BEFORE `up -d` recreates it.
# Recreation kills the container; a cycle interrupted between writing a PDF and
# committing its row leaves an orphan file behind. Check for an in-flight cycle
# first — ticks land on :00/:15/:30/:45 and normally finish in well under a
# minute, so there is a wide idle window.
# The image has no pgrep, so the check reads each process's command line
# from /proc, and prints `idle` only when the exec ran and found no ingest
# (`check-exit=1` inside the container). `NOT idle` means an ingest is running
# (wait a minute and check again) or the check didn't run at all: a stopped
# container or a failed exec never reads as idle. If `docker compose ps` shows
# the scheduler isn't running, there is no cycle to wait for. The `[r]` keeps
# grep from matching the shell that runs it.
docker compose -f deploy/docker-compose.yml exec -T scheduler sh -c 'grep -lsa "[r]eceipt-index.ingest" /proc/[0-9]*/cmdline; echo "check-exit=$?"' </dev/null | grep -qx 'check-exit=1' && echo idle || echo "NOT idle"
docker compose -f deploy/docker-compose.yml stop scheduler

# Migrations before the new code runs (the db service stays up throughout)
make migrate-up

docker compose -f deploy/docker-compose.yml up -d
docker compose -f deploy/docker-compose.yml logs -f scheduler
```

Rebuild the image on **every** `playwright` version bump — the browser build is pinned per Playwright release, and a mismatch between the pip package and the baked browser breaks HTML rendering (and therefore dead-letters receipts).

After an upgrade that touched the renderer, the Dockerfile, or the config, re-run Gates A–D before walking away. After an upgrade that changed the MCP tools, restart each MCP client ([section 10](#10-read-only-mcp-server)).

### 8.2 DLQ recovery

A failed item is retried automatically: a transient failure (the LLM overloaded or rate-limited, a network error) after 30 seconds to 15 minutes without using up an attempt, a content failure after 5 and then 30 minutes. The third content failure is **terminal**, and a skipped item (judged not a receipt) is never retried automatically. See [Retries](cli-reference.md#retries) in the CLI reference. Terminal and skipped items stay where they are until they are requeued by hand with `receipt-index retry`. Nothing in `ingest_log` is ever deleted: a requeue adds a `queued` record, and the next `ingest` runs the item again.

Two things happen on their own after the upgrade that adds retries. Items whose processing started but never finished (the run stopped mid-item; the old `ingest` logged a warning about them) are picked up by the next `ingest`: each is recorded as a failure that uses up an attempt, then retried, up to three paid attempts. Failures recorded before the upgrade are not retried until you requeue them. When a retry succeeds over a failure that had already stored a PDF, the old file is left behind and logged; `receipt-index failures --orphans` lists such files.

Only requeue for **environmental** causes: a rendering gap, a permissions problem, a corrupted credential, a bug since fixed. Content failures (the LLM genuinely cannot read the document) will simply fail again, and re-running them costs Anthropic spend for nothing.

1. **Fix the cause first.** Re-run the relevant gate to prove it.

2. **Preview.** `retry` writes nothing without `--yes`. Narrow it by error text, class, date, source or source ID; it refuses a selection that would match every failure unless `--all` is given:

   ```bash
   docker compose -f deploy/docker-compose.yml exec scheduler \
       receipt-index retry --since 2026-08-01 --error-like "<the environmental error substring>"
   ```

   The preview lists each matching item, its error class and attempt. It also lists any **orphaned PDF** for those items, a file stored before a failed database write, which would leave a duplicate PDF once the retry succeeds. Remove an orphan by hand if it's listed; `retry` never deletes files. For failures recorded before files were tracked, it says how many it couldn't check. For those, look for a plausibly-matching file under the receipt's year and month (receipts stored before the `renditions/` layout are under `data/receipts/<YYYY>/<MM>/`):

   ```bash
   ls -l data/receipts/renditions/<YYYY>/<MM>/
   ```

3. **Requeue exactly those.** Run the same command with `--yes`. It lists what it requeued and records the request in `receipt.retry_requests`:

   ```bash
   docker compose -f deploy/docker-compose.yml exec scheduler \
       receipt-index retry --since 2026-08-01 --error-like "<the environmental error substring>" --yes
   ```

   After an LLM outage, `--transient` selects every failure classed as transient. Use `--status skipped` for skipped items that should have been recognized as receipts.

4. **The next scheduled cycle runs them.** Run `receipt-index ingest` by hand to run them at once. Watch it, then re-check with `receipt-index failures --status queued` (should be empty) and the per-status breakdown. Each requeue is on record:

   ```bash
   docker compose -f deploy/docker-compose.yml exec scheduler sh -c '
       psql "$RECEIPT_INDEX_READ_DATABASE_URL" -c "
           SELECT requested_at, requested_by, filters, matched, requeued
           FROM receipt.retry_requests ORDER BY requested_at DESC LIMIT 10;"'
   ```

### 8.3 Disaster recovery from Dropbox

The full-loss story: the server is gone and Dropbox holds everything — the nightly dumps under `receipt-index/backups` and the complete receipt tree under `receipt-index/receipts`.

1. Rebuild the host per [section 2](#2-server-prerequisites), then provision per [section 3](#3-provisioning) (clone, `.env` and `.env.db` from 1Password, directories with uid 1000, rclone config). A single `.env` saved before [8.5](#85-split-the-owner-credentials-out-of-env) still holds the owner's lines: split it with 8.5 steps 2 and 3 first.

2. Check that `.env.db` sets all three login passwords, as in [4.2](#42-start-only-the-database). Roles are cluster-level and `pg_dump` doesn't carry them, so the logins come back only from `db-init`, which reads the passwords from `.env.db`. An env file saved before [8.4](#84-separate-database-logins) lacks `RECEIPT_INDEX_WRITE_PASSWORD` and `RECEIPT_INDEX_READ_PASSWORD`: add them from 1Password, or generate new ones and put the same values in the URLs that use them, before going on.

   ```bash
   grep -c -E '^RECEIPT_INDEX_(|WRITE_|READ_)PASSWORD=.' .env.db   # must print 3
   ```

   Then `docker compose -f deploy/docker-compose.yml build && docker compose -f deploy/docker-compose.yml up -d db`. First boot of the empty volume runs `db-init`, creating the `receipt_index` owner role, the write and read logins and the grant-target roles — this must happen **before** the restore, or the dump's `OWNER TO` statements fail and the app is locked out.

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

   On [rootless Docker](#rootless-docker), run this `chown` in the container form.

4. Restore into the fresh, empty database — again **without** `--no-owner` and **without** `--no-acl`:

   ```bash
   docker compose -f deploy/docker-compose.yml cp \
       data/backups/receipt_index-<dow>.dump db:/tmp/restore.dump
   docker compose -f deploy/docker-compose.yml exec db sh -c '
       pg_restore --exit-on-error --verbose \
           --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" /tmp/restore.dump'
   make migrate-up      # no-op, or applies a delta newer than the dump
   ```

5. Run the checks in [8.4](#84-separate-database-logins) step 7 and Gate B step 5: each URL connects as its login, the scheduler holds no owner credential, a backup runs as the read login, and the MCP check passes if the MCP server is set up.

6. Re-run Gates A–D and the verification queries in [6.6](#66-verification-queries), then Gate G. Expect to lose at most one day of ingest (the window between the last nightly dump and the loss); those messages are still in the mailbox and will be re-ingested on the next cycles, since their `source_id`s went down with the database.

### 8.4 Separate database logins

The application, the backups and the MCP server each log in with only the privileges they need, at the three levels of the [Database Standards](https://github.com/rmorison/engineering-standards/blob/main/code/database-standards.md):

| Level | Login | Used by | Gets its access from |
|---|---|---|---|
| `_all` | `receipt_index` | migrations (`MIGRATION_DATABASE_URL`) | owns every object |
| `_write` | `receipt_index_write` | the application (`DATABASE_URL`) | membership of `receipt_index_dev_write` |
| `_read` | `receipt_index_read` | the nightly backup and forensic queries (`RECEIPT_INDEX_READ_DATABASE_URL`), the MCP server (`RECEIPT_INDEX_MCP_DATABASE_URL`) | membership of `receipt_index_dev_read` |

Because the application doesn't own the tables, the migrations' column-level grants bind it, and it can't drop or disable the triggers that keep the extraction history append-only. Only the owner can, and only migrations log in as the owner.

**Known deviation from the standard.** The grant-target roles keep `_dev_` in their names (`receipt_index_dev_write`, not `receipt_index_prod_write`). The names are historical: every migration grants to them by name, so renaming them would be a migration change for no gain. The logins carry no environment in their names, to match `receipt_index`.

**Every new table needs a `SELECT` grant to `receipt_index_dev_read`.** The nightly backup runs as the read login, and `pg_dump` stops at the first table it can't read. A missing grant shows only as a failed backup in the scheduler's log.

A server provisioned with this version of `db-init` ([4.2](#42-start-only-the-database)) has these logins from first boot. A server provisioned earlier, where the application still logs in as `receipt_index`, switches once with the steps below. Nothing in the repository changes this by itself. Each step can run while the scheduler runs, apart from step 6, which recreates it between ingest cycles. If the scheduler is stopped and the schedule must stay off, change steps 6 and 7 and the rollback as [8.6](#86-with-the-scheduler-stopped) says.

**1. Check who owns the migration version tables.** Migration 000014 grants on them, and only their owner can:

```bash
docker compose -f deploy/docker-compose.yml exec db sh -c '
    psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -c "
        SELECT tablename, tableowner FROM pg_tables
         WHERE tablename LIKE '\''schema_migrations_%'\'' ORDER BY 1;"'
```

**Pass:** both rows show `receipt_index`. If one doesn't, hand it over before migrating, then re-run the check:

```bash
docker compose -f deploy/docker-compose.yml exec db sh -c '
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -c "
        ALTER TABLE public.schema_migrations_public OWNER TO receipt_index;
        ALTER TABLE public.schema_migrations_receipt OWNER TO receipt_index;"'
```

**2. Migrate.** `make migrate-up`, as in [4.3](#43-apply-migrations--an-explicit-deploy-step). It brings in migration 000014, which lets `receipt_index_dev_read` read the two version tables and takes back an `UPDATE` grant on `receipt.ingest_log` that no code used.

**3. Generate two passwords,** one per login, and store each in 1Password:

```bash
openssl rand -hex 24
```

**4. Create the logins.** Open psql in the db container as the superuser:

```bash
docker compose -f deploy/docker-compose.yml exec db sh -c 'psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB"'
```

Run these, and type each password at its `\password` prompt, so it never appears in a command line or the shell history. Each `CREATE ROLE` runs only when the role doesn't exist yet, and the rest can be repeated, so after a partial failure run the whole block again:

```sql
SELECT 'CREATE ROLE receipt_index_write LOGIN'
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'receipt_index_write')
\gexec
GRANT receipt_index_dev_write TO receipt_index_write;
GRANT CONNECT ON DATABASE :"DBNAME" TO receipt_index_write;
\password receipt_index_write

SELECT 'CREATE ROLE receipt_index_read LOGIN'
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'receipt_index_read')
\gexec
GRANT receipt_index_dev_read TO receipt_index_read;
GRANT CONNECT ON DATABASE :"DBNAME" TO receipt_index_read;
\password receipt_index_read

SELECT r.rolname, r.rolsuper, array_agg(g.rolname) AS member_of
  FROM pg_roles r
  JOIN pg_auth_members m ON m.member = r.oid
  JOIN pg_roles g ON g.oid = m.roleid
 WHERE r.rolname IN ('receipt_index_write', 'receipt_index_read')
 GROUP BY r.rolname, r.rolsuper ORDER BY 1;
\q
```

**Pass:** two rows, `receipt_index_read | f | {receipt_index_dev_read}` and `receipt_index_write | f | {receipt_index_dev_write}`. Each login is a member of its one role and nothing else.

**5. Set the two passwords and the three URLs** in an editor. Which file each goes in depends on whether this server has already split its env files ([8.5](#85-split-the-owner-credentials-out-of-env)):

```bash
test -e .env.db && echo split || echo "one file"
```

First keep a copy of `.env`, for rollback: it holds the current `DATABASE_URL` line. No service reads `.env.pre-logins`, and `.env.*` is git-ignored. `cp -p` keeps the file's mode, and `-n` never overwrites an earlier copy:

```bash
test ! -e .env.pre-logins && cp -pn .env .env.pre-logins && echo copied
```

**Pass:** `copied`. If `.env.pre-logins` already exists, an earlier run made it before editing `.env`. Keep it.

The two password lines:

```bash
RECEIPT_INDEX_WRITE_PASSWORD=<write password>
RECEIPT_INDEX_READ_PASSWORD=<read password>
```

The three URL lines:

```bash
DATABASE_URL=postgresql://receipt_index_write:<write password>@db:5432/receipt_index  # pragma: allowlist secret
RECEIPT_INDEX_READ_DATABASE_URL=postgresql://receipt_index_read:<read password>@db:5432/receipt_index  # pragma: allowlist secret
RECEIPT_INDEX_MCP_DATABASE_URL=postgresql://receipt_index_read:<read password>@db:5432/receipt_index  # pragma: allowlist secret
```

- **`one file`**, the usual case, since 8.5 runs after this section: all five lines go in `.env`, and the new `DATABASE_URL` replaces the current one. Leave `RECEIPT_INDEX_PASSWORD` and `MIGRATION_DATABASE_URL` in `.env` as they are: migrations still run as the owner. 8.5 later moves the owner's lines and the two passwords into `.env.db`.
- **`split`**: the two password lines go in `.env.db`, and the three URL lines in `.env`, where the new `DATABASE_URL` replaces the current one. `RECEIPT_INDEX_PASSWORD` and `MIGRATION_DATABASE_URL` stay in `.env.db`.

In either case, don't keep the old `DATABASE_URL` as a comment: the copy holds it, and 8.5 step 3 checks that `.env` has no owner URL, in a comment or not.

Use your `POSTGRES_DB` as the database name if it isn't `receipt_index`. The service doesn't read the two password variables; `db-init` does, on a disaster-recovery rebuild ([8.3](#83-disaster-recovery-from-dropbox)). Then check for expansion warnings, as in [3.3](#33-the--in-secrets-hazard--read-before-pasting-a-password):

```bash
docker compose -f deploy/docker-compose.yml config --quiet
```

**6. Recreate the scheduler** between ingest cycles. Compose reads `env_file` only when it creates a container. When step 5 changed a file `db` reads (the single `.env`, or `.env.db`), `up -d scheduler` recreates `db` too: Postgres restarts on the same volume, which takes seconds, and the scheduler waits for it to be healthy. Go on past the check only when it prints `idle`, as in [8.1](#81-upgrade):

```bash
docker compose -f deploy/docker-compose.yml exec -T scheduler sh -c 'grep -lsa "[r]eceipt-index.ingest" /proc/[0-9]*/cmdline; echo "check-exit=$?"' </dev/null | grep -qx 'check-exit=1' && echo idle || echo "NOT idle"
docker compose -f deploy/docker-compose.yml stop scheduler
docker compose -f deploy/docker-compose.yml up -d scheduler
```

**7. Verify.**

a. Each URL connects as its login:

```bash
docker compose -f deploy/docker-compose.yml exec scheduler sh -c '
    psql "$DATABASE_URL" -Atc "SELECT current_user"
    psql "$RECEIPT_INDEX_READ_DATABASE_URL" -Atc "SELECT current_user"
    psql "$RECEIPT_INDEX_MCP_DATABASE_URL" -Atc "SELECT current_user"'
```

**Pass:** `receipt_index_write`, `receipt_index_read`, `receipt_index_read`.

b. [Gate C](#gate-c--schema): the version query, now through the read login, and the application's search.

c. One supervised ingest. **Pass:** exit 0, and no `permission denied` in its output:

```bash
docker compose -f deploy/docker-compose.yml exec scheduler receipt-index ingest; echo "exit=$?"
```

d. A backup, by hand. It replaces today's dump with a fresh one. **Pass:** `backup: wrote /app/data/backups/receipt_index-<day>.dump (<size> bytes)`, with no `permission denied`:

```bash
docker compose -f deploy/docker-compose.yml exec scheduler /app/backup.sh
```

e. If the MCP server is set up, its check, [10.1](#101-one-time-setup) step 5.

f. The application can't switch off the history guard:

```bash
docker compose -f deploy/docker-compose.yml exec scheduler sh -c '
    psql "$DATABASE_URL" -c "ALTER TABLE receipt.extractions DISABLE TRIGGER trg_extractions_append_only"'
```

**Pass:** `ERROR:  must be owner of table extractions`.

**8. Retire the old MCP login.** If you set up the MCP server before this section existed, it logged in as `receipt_index_mcp`. Once check (e) passes on the read login, drop it in the psql session of step 4. `DROP ROLE` refuses while the role holds any privilege, so `DROP OWNED BY` goes first: it revokes every privilege the role holds in this database, its `CONNECT` included, and drops anything it owns (it should own nothing):

```sql
DROP OWNED BY receipt_index_mcp;
DROP ROLE receipt_index_mcp;
```

**9. Delete the copy** once every check in step 7 passes: `rm .env.pre-logins`.

**Rollback.** Put the old `DATABASE_URL` line back in `.env`, replacing the new one: copy it from `.env.pre-logins` in an editor. If the copy is gone, write it again as `DATABASE_URL=postgresql://receipt_index:<RECEIPT_INDEX_PASSWORD>@db:5432/receipt_index`, taking the password from `.env`, or from `.env.db` on a server that has split. The application logs in as `receipt_index` again, so an owner credential is back in the scheduler's environment, and the check in [8.5](#85-split-the-owner-credentials-out-of-env) step 6 names `DATABASE_URL` until you switch again. Recreate the scheduler as in step 6, and re-run Gate C's search check. Leave `RECEIPT_INDEX_READ_DATABASE_URL` set: backups as the read login don't depend on the application's login. To undo that too, remove the line and recreate the scheduler again, and backups go back to `DATABASE_URL`. `RECEIPT_INDEX_MCP_DATABASE_URL` stays on the read login: the MCP server never used the application's login. The logins and migration 000014 can stay. To remove `receipt_index_write` after a rollback, run `DROP OWNED BY` and `DROP ROLE` for it, as in step 8. Keep `receipt_index_read` while the backup or the MCP server uses it.

### 8.5 Split the owner credentials out of `.env`

A server provisioned with this version of [3.2](#32-create-env-and-envdb-at-the-clone-root) has two env files from the start. A server provisioned earlier keeps every variable in one `.env`, which both containers read, so the scheduler holds the owner's and the superuser's credentials. It moves over once, with the steps below. From this release, `deploy/docker-compose.yml` needs `.env.db`: without it, `docker compose config` and `up` fail with `env file ... not found` before touching a container.

**Precondition: [8.4](#84-separate-database-logins) is done,** so `DATABASE_URL` logs in as `receipt_index_write`. Otherwise `DATABASE_URL` is itself an owner credential and this split protects nothing. On a server still on the owner login, run 8.4 on the current checkout first, **before** `git pull` brings in this release: 8.4 step 6 recreates the scheduler, which needs `.env.db` once the new compose file is in place. Follow 8.4 from this guide as main has it, not from the copy in the server's checkout: an older copy's 8.4 step 5 keeps the old `DATABASE_URL` as a comment, which fails step 3 below, and has no 8.6. Read main's copy on GitHub, or with `git fetch && git show origin/main:docs/user/server-deployment.md | less`.

If the checkout predates 8.4 as well, it has neither 8.4's steps nor its migration. Go first to the last commit whose compose file doesn't need `.env.db`, which has both, and run 8.4 there. The `^1` is that commit, the parent of the merge that first required `.env.db`. `-m` makes `-S` compare each merge with its first parent:

```bash
cd ~/receipt-index
git fetch
git checkout "$(git log -m --first-parent --format=%H -S'../.env.db' origin/main -- deploy/docker-compose.yml | tail -1)^1" &&
    docker compose -f deploy/docker-compose.yml build
```

Then run 8.4, go back with `git checkout main`, and go on with step 1 below. Step 4's `git pull` brings in this release.

If the scheduler is stopped and the schedule must stay off, change steps 5 and 6 and the rollback as [8.6](#86-with-the-scheduler-stopped) says.

Steps 1 and 2 run once. If `.env.pre-split` or `.env.db` already exists, an earlier run got that far: don't run them again, since a second split of the already-split `.env` would replace both files with ones that hold no owner lines. Go on from step 3, or start over: `mv .env.pre-split .env && rm -f .env.db .env.new`, then step 1 again. The commands below refuse to overwrite either file, or a `.env.new` left by an interrupted run.

**1. Keep a copy** of the current file, for rollback. `cp -p` keeps its mode, `-n` never overwrites an earlier copy, and `.env.*` is git-ignored:

```bash
cd ~/receipt-index
test ! -e .env.pre-split && cp -pn .env .env.pre-split && echo copied
```

**Pass:** `copied`.

**2. Move the owner's lines** into `.env.db`. The subshell's `umask` creates both new files mode 600, `set -C` refuses to overwrite an existing `.env.db`, and `grep` writes the lines to files without printing them. The patterns also match a line written as `export NAME=` or indented:

```bash
(set -C; umask 077
 grep -E '^[[:space:]]*(export[[:space:]]+)?(POSTGRES_(USER|PASSWORD|DB)|RECEIPT_INDEX_(|WRITE_|READ_)PASSWORD|MIGRATION_DATABASE_URL)=' .env > .env.db &&
 grep -vE '^[[:space:]]*(export[[:space:]]+)?(POSTGRES_(USER|PASSWORD|DB)|RECEIPT_INDEX_(|WRITE_|READ_)PASSWORD|MIGRATION_DATABASE_URL)=' .env > .env.new &&
 mv .env.new .env)
```

**3. Check by count and by mode,** still printing no values:

```bash
grep -cE '^[[:space:]]*(export[[:space:]]+)?(POSTGRES_(USER|PASSWORD|DB)|RECEIPT_INDEX_(|WRITE_|READ_)PASSWORD|MIGRATION_DATABASE_URL)=' .env.db   # must print 7
grep -cE '^[[:space:]]*(export[[:space:]]+)?(POSTGRES_(USER|PASSWORD|DB)|RECEIPT_INDEX_(|WRITE_|READ_)PASSWORD|MIGRATION_DATABASE_URL)=' .env      # must print 0
grep -cE '^[[:space:]]*(export[[:space:]]+)?DATABASE_URL=' .env   # must print 1
grep -cE '://(receipt_index|postgres)[:@]' .env                   # must print 0
stat -c '%a %n' .env .env.db                                      # both 600
```

The fourth count reads comment lines too. If it isn't 0, `.env` still holds an owner URL, perhaps in a comment: delete that line in an editor. Compose ignores comments, so a comment never reached the container, but `.env` should hold no owner password at all. [8.4](#84-separate-database-logins) keeps its rollback copy in `.env.pre-logins`, not in a comment, so a server that followed 8.4 prints 0 here.

If `.env.db` holds fewer than 7 lines, `.env` lacked one of them: a server provisioned before 8.4 may lack the two login passwords; add them to `.env.db` from 1Password, as 8.3 step 2 says. Store `.env.db` in 1Password next to `.env`.

**4. Pull and check the compose file:**

```bash
git pull
docker compose -f deploy/docker-compose.yml build
docker compose -f deploy/docker-compose.yml config --quiet   # must print nothing
```

**5. Recreate both containers between ingest cycles.** `db`'s environment changed, so `up -d` recreates it as well as the scheduler: Postgres restarts on the same volume, which takes seconds. Stop the scheduler first, as in [8.1](#81-upgrade), so no cycle runs while the database restarts. Go on past the check only when it prints `idle`:

```bash
docker compose -f deploy/docker-compose.yml exec -T scheduler sh -c 'grep -lsa "[r]eceipt-index.ingest" /proc/[0-9]*/cmdline; echo "check-exit=$?"' </dev/null | grep -qx 'check-exit=1' && echo idle || echo "NOT idle"
docker compose -f deploy/docker-compose.yml stop scheduler
make migrate-up      # "no change", or this release's migrations; it reads .env.db
docker compose -f deploy/docker-compose.yml up -d
```

`make migrate-up` succeeding shows the Makefile found `MIGRATION_DATABASE_URL` in `.env.db`.

**6. Verify.**

a. The scheduler's environment holds no owner or superuser credential. The command prints variable names, never values:

```bash
docker compose -f deploy/docker-compose.yml exec scheduler sh -c \
    'found=$( { env | cut -d= -f1 | grep -xE "MIGRATION_DATABASE_URL|POSTGRES_(USER|PASSWORD)|RECEIPT_INDEX_(|WRITE_|READ_)PASSWORD"; env | grep -E "://(receipt_index|postgres)[:@]" | cut -d= -f1; } | sort -u | tr "\n" " "); echo "owner credentials: ${found:-none}"'
```

**Pass:** exactly `owner credentials: none`.

b. [8.4](#84-separate-database-logins) step 7: each URL connects as its login, Gate C, one supervised ingest, a backup by hand, and the MCP check if the MCP server is set up.

**7. Delete the copy** once every check passes: `rm .env.pre-split`.

**Rollback.** Put the old file back and recreate the scheduler between ingest cycles, as in step 5:

```bash
cp -p .env.pre-split .env
docker compose -f deploy/docker-compose.yml exec -T scheduler sh -c 'grep -lsa "[r]eceipt-index.ingest" /proc/[0-9]*/cmdline; echo "check-exit=$?"' </dev/null | grep -qx 'check-exit=1' && echo idle || echo "NOT idle"
docker compose -f deploy/docker-compose.yml stop scheduler
docker compose -f deploy/docker-compose.yml up -d scheduler
```

The scheduler then holds the owner credentials again, as before this section, and `db` keeps reading `.env.db`. To also go back to the previous release, check out its commit and run `up -d`: its compose file reads the restored `.env` for both services, and `.env.db` is ignored.

### 8.6 With the scheduler stopped

Sometimes the schedule has to stay off, for example until a backfill finishes, so the scheduler isn't running. `up -d` and `up -d scheduler` would start supercronic, and the schedule with it, and `exec` needs a running container. To run [8.1](#81-upgrade), [8.4](#84-separate-database-logins) or [8.5](#85-split-the-owner-credentials-out-of-env) and leave the schedule off, change their commands like this:

- **Skip the idle check and `stop scheduler`.** No cycle is running.
- **Each `exec` becomes a one-off `run`.** The program goes in `--entrypoint` and its arguments after `scheduler`. A one-off container reads `.env` when it starts, so it sees an edit straight away, with no recreate:

  ```bash
  # exec scheduler receipt-index ingest
  docker compose -f deploy/docker-compose.yml run --rm -T --entrypoint receipt-index scheduler ingest
  # exec scheduler sh -c '...'     (8.4 step 7a and 7f, 8.5 step 6a)
  docker compose -f deploy/docker-compose.yml run --rm -T --entrypoint sh scheduler -c '...'
  # exec scheduler /app/backup.sh  (8.4 step 7d)
  docker compose -f deploy/docker-compose.yml run --rm -T --entrypoint /app/backup.sh scheduler
  ```

  [10.1](#101-one-time-setup) step 4 gives the same form for the MCP check.
- **`up -d` and `up -d scheduler` become `up -d db`.** It still recreates `db` when its env file changed (8.4 step 6, 8.5 step 5), and leaves the scheduler stopped. Skip `logs -f scheduler`.
- **Copy a backup made by hand off the host yourself.** The sync cron lines don't run, so the dump from 8.4 step 7d stays in `data/backups` until the schedule starts. To copy it now:

  ```bash
  docker compose -f deploy/docker-compose.yml run --rm -T --entrypoint rclone scheduler \
      copy /app/data/backups dropbox:receipt-index/backups --exclude "*.tmp" \
      --config /app/.rclone/rclone.conf -v
  ```

When the schedule should start, `up -d` starts it, as in [Gate G](#gate-g--supervised-cycle-then-the-schedule) step 2.

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

The server refuses to start unless its database role is read-only. It logs in as the read login, `receipt_index_read`, a member of `receipt_index_dev_read` only, which `db-init` creates on a new server and [8.4](#84-separate-database-logins) creates on an older one. The application's logins, `receipt_index` and `receipt_index_write`, can write, so the server refuses them.

**Restart MCP clients after an upgrade that changes the tools.** A client loads the tool list and each tool's input schema when it connects, and keeps them until it restarts. Results can carry the new fields at once, but the client won't offer a new tool or filter until it restarts or reconnects to the server.

### 10.1 One-time setup

**1. Create the access-log directory.** Fetches are logged to `data/mcp-logs`, a separate bind mount. It is deliberately outside `data/receipts` and `data/backups`, because the crontab copies both of those to Dropbox and a fetch log has no business leaving the host. Skip this if you provisioned with the current [3.4](#34-create-the-bind-mount-directories-before-the-first-up--d).

```bash
cd ~/receipt-index
mkdir -p data/mcp-logs
chown 1000:1000 data/mcp-logs   # sudo if not your uid
```

On [rootless Docker](#rootless-docker), chown it from inside a container instead.

**2. Use the read login.** If the server predates the separate logins, do [8.4](#84-separate-database-logins) first: it creates `receipt_index_read` and sets `RECEIPT_INDEX_MCP_DATABASE_URL` to it. An older setup used its own `receipt_index_mcp` login; 8.4 step 8 retires it. There's one read login, for the backups, forensic queries and this server.

**3. Add the access-log line to the clone-root `.env`,** next to `RECEIPT_INDEX_MCP_DATABASE_URL` (which [8.4](#84-separate-database-logins) step 5, or `deploy/example.env` on a new server, already sets to the read login):

```bash
RECEIPT_INDEX_MCP_ACCESS_LOG=/app/data/mcp-logs/mcp-access.jsonl
```

**4. Recreate the scheduler.** Compose reads `env_file` only when it creates the container, so `exec` won't see the new lines until you recreate it. As in [8.1](#81-upgrade), stop the scheduler between ingest cycles first, going on only when the check prints `idle`:

```bash
docker compose -f deploy/docker-compose.yml exec -T scheduler sh -c 'grep -lsa "[r]eceipt-index.ingest" /proc/[0-9]*/cmdline; echo "check-exit=$?"' </dev/null | grep -qx 'check-exit=1' && echo idle || echo "NOT idle"
docker compose -f deploy/docker-compose.yml stop scheduler
docker compose -f deploy/docker-compose.yml up -d scheduler
```

`up -d scheduler` also starts supercronic, and with it the 15-minute schedule. Where the schedule must stay off, as before Gate G, leave the scheduler stopped. A one-off container reads `.env` when it starts, so run the check in step 5 as:

```bash
docker compose -f deploy/docker-compose.yml run --rm -T --entrypoint receipt-index scheduler mcp </dev/null; echo "exit=$?"
```

`exec` needs a running scheduler, so an MCP client connecting over SSH uses the same one-off form in place of the command in [section 10](#10-read-only-mcp-server). `run` reads the compose file, so it runs from the clone:

```bash
ssh receipt-server 'cd ~/receipt-index && docker compose -f deploy/docker-compose.yml run --rm -T --entrypoint receipt-index scheduler mcp'
```

**5. Check it.** Run the startup check with stdin closed, so the server starts and exits at once:

```bash
docker compose -f deploy/docker-compose.yml exec -T scheduler receipt-index mcp </dev/null; echo "exit=$?"
```

**Pass:** `MCP server ready on stdio (access log: /app/data/mcp-logs/mcp-access.jsonl)` and `exit=0`. `Error: the database role can write (...)` means it is connecting as `receipt_index` or `receipt_index_write`: the variable is unset in the container (step 4), so the server falls back to `DATABASE_URL`, or the URL names the wrong login. `lacks SELECT` means the role isn't a member of `receipt_index_dev_read`, or the migrations' grants are missing.

### 10.2 The access log

One JSON line per `get_receipt_file` call: timestamp, receipt ID, stored path, size, whether content was returned, the outcome, and the client's name and version. If the log can't be written, the server withholds the file. The log is small and isn't rotated. Read it with:

```bash
tail -n 20 data/mcp-logs/mcp-access.jsonl
```
