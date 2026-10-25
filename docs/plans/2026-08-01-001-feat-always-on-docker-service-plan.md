---
title: "feat: Always-on Docker service with Dropbox mirror"
type: feat
status: completed
date: 2026-08-01
origin: docs/brainstorms/2026-08-01-always-on-service-requirements.md
deepened: 2026-08-01
---

# feat: Always-on Docker service with Dropbox mirror

## Overview

Move receipt ingestion from a manual, laptop-bound CLI run to an unattended Docker compose deployment on a home server: Postgres 18 in compose, a supercronic-scheduled `receipt-index ingest` every 15 minutes with a no-overlap guarantee, and an rclone additive copy of `data/receipts` to Dropbox. No new application features — two small app-code changes (library log levels, render-time network egress blocking), image hardening, compose/scheduler wiring, and a documented cutover.

---

## Problem Frame

New receipts sit unprocessed until the operator runs `receipt-index ingest` on the dev machine, and the PDF store exists only there (see origin: docs/brainstorms/2026-08-01-always-on-service-requirements.md). The service must process receipts within ~15–30 minutes unattended, survive reboots, and mirror receipts to Dropbox for off-machine access.

**Critical constraint discovered in research:** the existing production `Dockerfile` installs neither Playwright browsers nor weasyprint's system libraries, so every HTML/text email receipt would fail rendering in-container. Because failures land in `ingest_log` (DLQ) and are then *permanently skipped* by idempotency, deploying the current image would silently lose receipts. Image hardening (U1) is therefore a hard prerequisite for everything else.

---

## Requirements Trace

- R1. Single compose project on the server; `docker compose up -d` brings up everything; survives reboots → U3, U5
- R2. Ingest is the existing one-shot CLI on a 15-minute schedule; no new daemon code → U3
- R3. Ingest runs never overlap → U3 (supercronic default no-overlap)
- R4. `data/receipts` mirrored one-way to Dropbox, additive only → U4
- R5. Postgres 18 in compose with persistent volume; one-time dump/restore cutover from dev pg:5435 → U3, U6
- R6. Migrations applied as an explicit deploy step (both migration tables) → U5
- R7. Search/show/failures via SSH wrapper → U5
- R8. Secrets in server-side `.env` + server-side rclone config; nothing baked into image → U3, U4, U5
- R9. Per-cycle summary logging; readable container logs; DLQ inspectable → U2, U3

**Origin flows:** F1 (scheduled ingest → U3), F2 (Dropbox mirror → U4), F3 (search from laptop → U5)

---

## Scope Boundaries

Carried from origin (see origin doc for rationale):

- No push triggers (IMAP IDLE, GDrive webhooks) — polling only
- No web UI, API, or remote search endpoint
- No bidirectional Dropbox sync; Dropbox is not an ingest source
- No CI/CD or registry image delivery — image is built on the server from a git clone
- The O(N) IMAP enumeration issue is not addressed here

### Deferred to Follow-Up Work

- Stale doc cleanup: `docs/user/getting-started.md`, `cli-reference.md`, `troubleshooting.md` still reference the retired `RECEIPT_STORE_PATH` env var — fix opportunistically or as a docs follow-up, not blocking this deploy
- Starting a `docs/solutions/` learnings KB after this deployment lands (none exists today)

---

## Context & Research

### Relevant Code and Patterns

- `Dockerfile` — multi-stage uv build, `appuser` uid/gid 1000, `ENTRYPOINT ["receipt-index"]`; no apt packages, no browsers (the U1 gap)
- `docker-compose.yml` — dev-only (db + greenmail); stays untouched; pg18 volume must mount at `/var/lib/postgresql` (not a subdir)
- `Makefile` — golang-migrate v4.18.3 auto-downloaded to `.bin/`; `migrate-up` runs both migration dirs with separate `x-migrations-table` params; requires `MIGRATION_DATABASE_URL` to already contain a `?` query param (Makefile appends with `&`); auto-exports `.env`
- `src/receipt_index/config.py` — config search order: `--config` flag → `RECEIPT_INDEX_CONFIG` → `./receipt-index.yaml` (CWD-relative) → XDG path. `${VAR}` interpolation hard-fails listing all missing vars
- `src/receipt_index/cli.py` — `load_dotenv()` at import; `logging.basicConfig` per command to stdout; `ingest` exits 1 if any item failed (per-cycle signal, DLQ'd items never retried); store path resolved relative to CWD (`/app` in container)
- `src/receipt_index/renderer.py` — render order: PDF pass-through → Playwright Chromium (HTML) → weasyprint fallback; defaults `PLAYWRIGHT_BROWSERS_PATH` to relative `.playwright` if unset — must be set explicitly in the image
- `src/receipt_index/repository.py` — idempotency = union of `receipts.source_id` and `ingest_log.source_id`; UNIQUE on `source_id` makes races non-corrupting; **no app-level locking** — no-overlap is entirely the scheduler's job
- `src/receipt_index/store.py` — DB stores paths relative to store root → store tree can be rsynced to the server without DB rewrites
- `src/receipt_index/auth.py` + `adapters/gdrive.py` — GDrive token lives in env (`GDRIVE_TOKEN_JSON`), refreshed in memory only; headless-safe
- `db/init/01-create-roles.sql` + `docs/user/production-setup.md` — role/grant pattern (migrations grant to `receipt_index_dev_write`/`_read`); reusable for server Postgres init
- Known config gotcha: checked-in `receipt-index.yaml` interpolates `${GDRIVE_CREDENTIALS_JSON}` (a key the gdrive source model ignores) — the server `.env` must define it or the deployed YAML must drop the key

### Institutional Learnings

- No `docs/solutions/` KB exists. Carried from CLAUDE.md/project memory: pg18 volume path, dual migration tables, O(N) IMAP poll cost, DLQ needs monitoring when unattended, pdfminer/fontTools log flood (fixed here as U2)

### External References

- supercronic (github.com/aptible/supercronic): waits for a running job before rescheduling — **no-overlap is the default**; `-overlapping` opts out; single static binary; jobs log to stdout
- rclone Dropbox (rclone.org/dropbox, rclone.org/remote_setup): custom Dropbox app ID recommended (shared rclone ID hits `too_many_requests`); scopes are frozen into the token at authorize time; token auto-refresh **rewrites rclone.conf via file rename** → mount the whole config dir read-write, never `:ro`, never a single-file bind
- `rclone copy` is additive (never deletes at destination) — matches R4; `rclone sync` would propagate deletions (rejected)
- Playwright in Docker (playwright.dev/python/docs/docker): install as root with `PLAYWRIGHT_BROWSERS_PATH=/ms-playwright` + `playwright install --with-deps --only-shell chromium`, world-readable; pin `python:3.11-slim-bookworm`; `init: true` for zombie reaping; `shm_size: 1gb` (default 64 MB /dev/shm crashes Chromium); Playwright disables the Chromium sandbox by default in containers (enabling it needs a custom seccomp profile); email HTML is **third-party content crossing a trust boundary**, so the compensating control is render-time network egress blocking (U7) rather than seccomp work; browser builds are pinned per Playwright release — rebuild image on every `playwright` bump

---

## Key Technical Decisions

- **supercronic in the app image, compose `entrypoint` override**: no-overlap by default satisfies R3 with zero config; no docker socket exposure (rejected: ofelia needs `/var/run/docker.sock` root-equivalent access; plain sleep-loop drifts and has no missed-tick visibility)
- **rclone binary installed in the app image, run as a second crontab line**: one scheduler, no sidecar container, no docker socket (rejected: `rclone/rclone` sidecar adds a second scheduling mechanism for no benefit at this scale)
- **Separate ingest and sync cron lines, offset a few minutes** (e.g. `*/15` and `7,22,37,52`): decoupled so an ingest exit 1 (any single failed item) never blocks the mirror; supercronic's no-overlap applies per job line
- **Custom Dropbox app ID** in "development" status for a single user: avoids shared-client rate limiting; enable scopes (`files.content.write/read`, `files.metadata.write`, `account_info.read`) *before* authorizing
- **Separate production compose file** (`deploy/docker-compose.yml`): dev `docker-compose.yml` (db + greenmail) stays untouched; prod file has no greenmail
- **Postgres bound to `127.0.0.1` host port only** on the server: lets the Makefile migrate target and ad-hoc `psql` run on the server (and from the laptop via SSH tunnel) without exposing the DB (resolves origin deferred question)
- **Bind mounts for `data/receipts`, config YAML, crontab, and rclone config dir; named volume for pgdata**: receipts tree must be rsync-able and rclone-readable from the host; pgdata never needs host-side access
- **Explicit `RECEIPT_INDEX_CONFIG=/app/receipt-index.yaml`** in compose env: CWD-relative config discovery is fragile under `docker compose exec -w`
- **Image built on the server from a git clone** (origin decision): no registry; deploy = `git pull && docker compose build && docker compose up -d`
- **Production-specific DB init script, not the dev one** (deepening finding, verified against the live source DB): `db/init/01-create-roles.sql` creates `receipt_index_dev_*` roles but *not* `receipt_index` — the login role that owns every object in the dump — and hardcodes `GRANT ... ON DATABASE receipt_index_dev` plus the `localpass` password. Under the postgres image's `ON_ERROR_STOP` init, a different `POSTGRES_DB` name aborts the container on first boot; even if boot succeeds, the restore's `ALTER ... OWNER TO receipt_index` statements fail and the app role is locked out on cycle one. Prod init mirrors the source role set exactly (`receipt_index` login owner + `receipt_index_dev_write`/`_read` nologin members, per `docs/user/production-setup.md`), parameterized DB name, secret password; server `DATABASE_URL` connects as `receipt_index` (owner), matching the source's actual privilege model (table-level ACLs on the live source are NULL — the app-as-owner model is the real one). The init script **also creates `receipt_index_dev_all` (nologin, idempotent)** even though the live source lacks it: migrations `000002`/`000004` GRANT to that role, so a fresh empty server DB cannot pass `make migrate-up` (gate C) without it — verified against the migration files and the live source DB (review finding)
- **Cutover restore = fresh-database full restore, migrate-up as no-op afterward** (deepening finding): restore the full dump (which carries both `schema_migrations_*` tables at their correct versions — public v1 / receipt v7 today) into an *empty* server DB as the compose superuser, **without** `--no-owner` (so ownership lands on `receipt_index`) and without `--no-acl` (the only ACLs are column grants to roles that exist); then `make migrate-up` verifies as a no-op or applies any post-v7 delta in the repo. Rejected: `--data-only` restore — requires excluding both migration tables, exact version parity, FK-order luck, and collides on UNIQUE(source_id) if the server ever ingested anything

---

## Open Questions

### Resolved During Planning

- Scheduler choice (origin deferred): supercronic — see decisions
- rclone coupling and cadence (origin deferred): second crontab line, offset from ingest; Dropbox destination `dropbox:receipt-index/receipts`
- Postgres host port exposure (origin deferred): yes, loopback-only
- pdfminer/fontTools log noise (origin deferred): fixed in app code as U2

### Deferred to Implementation

- Exact pinned versions (supercronic release + sha256, Playwright pip pin, postgres:18 minor, rclone version): read from upstream at build time
- Whether `--only-shell` Chromium suffices for all real receipt HTML: verify in U1 smoke tests, fall back to full chromium install if a rendering gap appears
- `rclone copy --immutable` tripwire flag: adopt only after confirming the mid-write-copy caveat is acceptable — supercronic's no-overlap is *per job line*, so a long ingest can overlap a sync tick and rclone may copy a PDF mid-write; today that self-heals on the next pass (size/modtime re-copy), but under `--immutable` it becomes a permanent mismatch. A periodic server-vs-Dropbox count-parity check is the detector either way
- Server filesystem layout (`/srv/receipt-index` vs `~/receipt-index` clone location): decided in U5 runbook when provisioning

---

## Implementation Units

- U1. **Harden the production image for in-container rendering**

**Goal:** The image can render every receipt type (PDF pass-through, HTML via Playwright Chromium, text via weasyprint) and carries supercronic + rclone binaries.

**Requirements:** R2 (prerequisite), R4; blocks all deployment units

**Dependencies:** None

**Files:**
- Modify: `Dockerfile`
- Test: `tests/integration/test_docker_render.py` (new; gated like other integration tests)

**Approach:**
- Pin base `python:3.11-slim-bookworm` in both stages
- Final stage, as root before `USER appuser`: apt weasyprint runtime libs (pango, gdk-pixbuf, fontconfig + a font package), `ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright`, `playwright install --with-deps --only-shell chromium` (via the venv's playwright, world-readable), supercronic binary (pinned release + sha256), rclone binary, and `postgresql-client-18` from the PGDG apt repo (bookworm's default client is v15, older than the v18 server — needed for the nightly pg_dump backup job)
- Keep `ENTRYPOINT ["receipt-index"]`; the scheduler service overrides entrypoint in compose

**Patterns to follow:**
- Existing multi-stage uv layout in `Dockerfile`; Microsoft's shared-`PLAYWRIGHT_BROWSERS_PATH` pattern

**Test scenarios:**
- Happy path: container renders an HTML email fixture to PDF via Playwright as uid 1000 (`docker run --rm --entrypoint python <image> - <<render smoke script>>` style harness)
- Happy path: weasyprint renders a plain-text fixture to PDF in-container (proves system libs present)
- Edge case: rendering works with `PLAYWRIGHT_BROWSERS_PATH` as baked into the image (no host env leakage)
- Error path: image build fails loudly if `playwright install` and the pip `playwright` version drift (document the coupling in a Dockerfile comment)

**Verification:**
- Both render paths produce non-empty PDFs inside the container as uid 1000; `supercronic -version` and `rclone version` succeed in the image; image builds reproducibly on a clean machine

---

- U2. **Tame third-party library log noise in the CLI entrypoint**

**Goal:** Container logs at INFO are readable: pdfminer, fontTools (and similarly noisy libs) clamped to WARNING regardless of app log level.

**Requirements:** R9

**Dependencies:** None (parallel with U1)

**Files:**
- Modify: `src/receipt_index/cli.py`
- Test: `tests/unit/test_cli.py`

**Approach:**
- After `logging.basicConfig(...)`, set known-noisy library loggers (`pdfminer`, `fontTools`, `PIL`) to WARNING unless the configured app level is DEBUG *and* the user explicitly wants library debug (keep it simple: always clamp; revisit if library debugging is ever needed)

**Patterns to follow:**
- Existing per-command logging setup in `src/receipt_index/cli.py`

**Test scenarios:**
- Happy path: with config level INFO, `pdfminer`/`fontTools`/`PIL` loggers are at WARNING after CLI init
- Edge case: with config level DEBUG, app loggers emit DEBUG while noisy libraries stay clamped
- Happy path: existing log format/level behavior for `receipt_index.*` loggers unchanged

**Verification:**
- An ingest run over a PDF-bearing fixture produces no pdfminer/fontTools INFO/DEBUG lines at level INFO

---

- U3. **Production compose project with scheduled ingest**

**Goal:** `docker compose up -d` on the server runs Postgres and a supercronic scheduler that fires `receipt-index ingest` every 15 minutes, no overlap, reboot-safe.

**Requirements:** R1, R2, R3, R5 (service half), R8 (env_file), R9 (per-cycle summary via existing ingest output + supercronic tick logs)

**Dependencies:** U1

**Files:**
- Create: `deploy/docker-compose.yml`
- Create: `deploy/crontab`
- Create: `deploy/receipt-index.yaml` (server config: imap + gdrive sources, `store.path: /app/data/receipts`, INFO logging)
- Create: `deploy/example.env` (server env template: DB URL, IMAP password, Anthropic key, `GDRIVE_TOKEN_JSON`, `GDRIVE_CREDENTIALS_JSON` if the YAML keeps that key)
- Create: `deploy/db-init/01-create-roles.sql` (production role init — see Key Technical Decisions; **not** the dev `db/init/` script)
- Test: none (validated by U5 runbook smoke steps and `docker compose config`)

**Approach:**
- `db`: `postgres:18`, named volume at `/var/lib/postgresql`, `deploy/db-init/` role scripts (creates `receipt_index` login owner + `receipt_index_dev_write`/`_read`/`_dev_all` nologin members — `_dev_all` is required by migration grants 000002/000004 on a fresh DB; DB name/password from env, nothing hardcoded), healthcheck, `restart: unless-stopped`, ports `127.0.0.1:15432:5432`
- `scheduler`: built app image; `entrypoint: ["supercronic", "/app/crontab"]`; `init: true`; `shm_size: 1gb`; `restart: unless-stopped`; `depends_on: db: condition: service_healthy`; `env_file: .env`; env `RECEIPT_INDEX_CONFIG=/app/receipt-index.yaml`, `PLAYWRIGHT_BROWSERS_PATH` (match image); bind mounts: `./data/receipts:/app/data/receipts`, `./receipt-index.yaml:/app/receipt-index.yaml:ro`, `./crontab:/app/crontab:ro`, rclone config dir (see U4)
- Crontab line: `*/15 * * * * receipt-index ingest` — supercronic skips a tick if the previous run is still going (R3); DB is compose-internal hostname `db`
- **Nightly DB backup crontab line** (review finding — post-cutover, pgdata on one server is otherwise the only copy of receipt metadata, vendor relabels, and the load-bearing ingest_log idempotency state; the Dropbox PDFs cannot be re-ingested): pg_dump against `db` into a bind-mounted `data/backups/` dir with day-of-week filename rotation — bounded at 7 files because `rclone copy` updates changed files (it only never deletes), so the mirror stays bounded too
- Host bind-mount ownership: `data/receipts`, `data/backups`, and the rclone config dir must be owned by uid/gid 1000 (runbook step in U5)

**Test scenarios:**
Test expectation: none in pytest — infrastructure config. Validation is behavioral (see Verification) plus `docker compose -f deploy/docker-compose.yml config` lint in U5's deploy steps.

**Verification:**
- Fresh `docker compose up -d` on a clean host: db healthy, scheduler running; ingest fires on the next 15-minute boundary and logs a cycle summary; a deliberately long-running tick is skipped, not overlapped (observable via supercronic's falling-behind warning); `docker compose restart` and a host reboot both resume the service unattended
- In-container store-write proof as uid 1000: create/delete a sentinel file under the bind-mounted `data/receipts` from inside the scheduler container (ownership `ls` alone is insufficient — a store-write failure DLQs items exactly like a render gap)
- `ingest --dry-run` in-container with the real `.env` succeeds — the config loader hard-fails listing every missing `${VAR}`, so one clean dry-run proves env completeness and exercises IMAP/GDrive auth with zero writes and zero DLQ exposure
- One deliberately failing cycle is observed so the nonzero-exit log signature in supercronic output is known before launch (this is the pattern weekly monitoring greps for)

---

- U4. **Dropbox mirror via rclone cron job**

**Goal:** Every rendered receipt appears in Dropbox within minutes of ingest; deletions/renames on the server never delete anything in Dropbox.

**Requirements:** R4, R8 (rclone token handling)

**Dependencies:** U1, U3

**Files:**
- Modify: `deploy/crontab` (add sync line)
- Modify: `deploy/docker-compose.yml` (rclone config dir bind mount)
- Create: `deploy/rclone-setup.md` (or fold into U5 runbook — one-time Dropbox app + auth procedure)
- Test: none (one-time OAuth is manual; behavior verified operationally)

**Approach:**
- One-time (documented, performed on laptop): create a scoped Dropbox app (own client ID/secret; enable `account_info.read`, `files.metadata.write`, `files.content.write`, `files.content.read` **before** authorizing; redirect `http://localhost:53682/`), run `rclone config` locally, scp the resulting config dir to the server
- Mount the whole rclone config dir **read-write** into the scheduler container (token refresh rewrites `rclone.conf` via rename — `:ro` or single-file mounts break refresh)
- Crontab line offset from ingest (e.g. `7,22,37,52 * * * *`): `rclone copy /app/data/receipts dropbox:receipt-index/receipts --config /app/.rclone/rclone.conf -v` — `copy` is additive-only per R4
- Second copy target for the nightly DB dumps: `data/backups/` → `dropbox:receipt-index/backups` (same additive semantics; day-of-week rotation keeps it at 7 objects)
- No coupling to ingest exit code: sync runs even when a cycle had a failed item

**Test scenarios:**
Test expectation: none in pytest — external service + one-time OAuth. Operational checks in Verification.

**Verification:**
- New receipt PDF appears under the Dropbox folder within one sync interval; deleting a file server-side does *not* delete it in Dropbox on subsequent runs; token refresh survives >4 hours unattended (short-lived Dropbox tokens force refresh) with `rclone.conf` updated in place; rate-limit errors absent from logs (custom app ID working)
- Initial bulk mirror of the full historical tree (the actual rate-limit and correctness stress case, not the single-new-file case): Dropbox file count equals server count afterward
- rclone remote listing succeeds from inside the container as uid 1000 via the mounted config dir; an induced auth failure's log signature is observed once so weekly monitoring can recognize it

---

- U5. **Deployment runbook, migration step, and SSH search wrapper**

**Goal:** A single document takes a clean Ubuntu server to a running service, and the operator can search from the laptop.

**Requirements:** R1 (reboot durability), R6, R7, R8

**Dependencies:** U3, U4 (documents their artifacts)

**Files:**
- Create: `docs/user/server-deployment.md`
- Create: `scripts/receipt-search` (laptop-side SSH wrapper: `ssh receipt-server docker compose -f ... exec scheduler receipt-index "$@"` shape)
- Modify: `docs/user/production-setup.md` (pointer note: native-PG setup superseded by server deployment for this project)
- Test: none — documentation + trivial wrapper script

**Approach:**
- Runbook sections: server prerequisites (Docker Engine + compose plugin, boot-enabled); clone + `.env` provisioning from 1Password (never committed); bind-mount dir creation with uid 1000 ownership; image build; **migrations as an explicit step** — run `make migrate-up` on the server against `127.0.0.1:15432` with `MIGRATION_DATABASE_URL` containing `?sslmode=disable` (Makefile appends `&x-migrations-table=...`; golang-migrate auto-downloads to `.bin/`); `docker compose up -d`; smoke checks (`docker compose logs`, `receipt-index failures`); upgrade procedure (`git pull && docker compose build && up -d`, re-run migrations)
- **First-launch Go/No-Go gates**, ordered, each a hard stop, with the 15-minute schedule on real mail enabled *last* (the DLQ converts launch-day environmental failures into permanent skips): A) render smoke on the server-built image, both engines, as uid 1000; B) platform — compose lints, db healthy, pgdata at `/var/lib/postgresql`, in-container sentinel write to the store bind mount and the rclone config dir; C) schema — migration tables at expected versions (recorded in the runbook), not dirty; app-role read path proven via in-container `search`; D) `ingest --dry-run` with the real `.env` (complete env/auth proof, zero writes); E) cutover parity + DLQ baseline recorded (see U6); F) Dropbox — remote listing as uid 1000, initial bulk mirror count-verified; G) one supervised manual cycle, then enable the schedule, observe two automatic ticks, then the reboot test against the final config
- **Pre-cutover smoke is `ingest --dry-run`, not a real ingest** — a real smoke ingest would leave the server DB and store non-empty, breaking U6's fresh-database restore precondition (deepening finding)
- **Monitoring section (weekly, solo-operator)**: compare the **full per-status ingest_log breakdown (success/skipped/failed) against the cutover baseline** — not just `failures`. A low-confidence receipt is logged as `skipped`, exits 0, never appears in `receipt-index failures` (which filters `status='failed'` only), and is permanently skipped — the silent half of the data-loss surface, with real precedent (amount:0 saga, payment confirmations). The runbook includes a documented SQL query to list new `skipped` rows' subject/sender for eyeball review; a `failures --status skipped` CLI flag is named as future work. Also: compose-log scan for nonzero ingest exits and supercronic falling-behind warnings (the latter is the O(N) IMAP enumeration early-warning); Dropbox freshness — newest-file timestamp checkable from any device with no SSH, the free end-to-end delivery check (confirms liveness only when receipts were expected in the window). Name as future work: a crontab line pinging a healthchecks.io-style dead-man's-snitch URL after each successful cycle
- **DLQ recovery procedure**: after fixing an environmental cause (render, perms, network), delete the affected `failed` ingest_log rows to force retry; content failures will simply re-fail
- **Disaster recovery from Dropbox**: restore procedure using the newest `backups/` dump (DB) plus the mirrored `receipts/` tree (files) onto a rebuilt server — the full-loss story, documented while everything works
- SSH wrapper covers `search`, `show`, `failures`; note that `docker compose exec` needs the scheduler container running (it always is)
- Include the `GDRIVE_CREDENTIALS_JSON` interpolation gotcha in the env checklist

**Test scenarios:**
Test expectation: none — docs and a passthrough script; validated by executing the runbook end-to-end on the real server (Verification).

**Verification:**
- A fresh follow-the-runbook pass on the server reaches a healthy `docker compose ps` with migrations applied and a clean in-container `ingest --dry-run`; `scripts/receipt-search search --vendor foo` returns results from the laptop (post-cutover); every Go/No-Go gate has a concrete pass criterion written in the runbook

---

- U6. **Data cutover from dev machine to server**

**Goal:** All existing receipts (DB rows + PDF tree) live on the server; the dev pg:5435 instance is retired for this project; no receipt is double-processed or lost.

**Requirements:** R5

**Dependencies:** U5 (runbook exists, service deployed but scheduler paused during cutover)

**Files:**
- Modify: `docs/user/server-deployment.md` (one-time cutover appendix: procedure + verification queries)
- Test: none — one-time operational procedure with explicit verification queries

**Approach:**
- Preconditions (deepening findings, verified against the live source DB): server DB is *empty* at cutover start — if U5 smoke steps or a stray cycle touched it, drop/recreate first, and verify server `ingest_log` has zero rows immediately before restore ("paused" is proven, not assumed). The rsync target store is likewise empty (additive rsync over smoke-test leftovers strands orphan PDFs — LLM vendor extraction isn't deterministic enough for filenames to collide cleanly)
- Laptop stop is *enforced*, not habitual: rename the laptop `.env`/config at cutover start so an accidental `receipt-index ingest` cannot connect; take the dump strictly after the last laptop run completes; retain the dump file as the rollback artifact (on abort, re-restore from the *same* dump — never re-dump)
- Sequence: enforce laptop stop → `pg_dump` from dev pg:5435 → fresh-database full restore as compose superuser, no `--no-owner`, no `--no-acl` (see Key Technical Decisions; requires the `receipt_index` role from `deploy/db-init/`; the dump carries `pg_trgm`, both `schema_migrations_*` tables, and all ownership) → `make migrate-up` as verification no-op (or applies any post-v7 delta in the repo — the only correct ordering) → checksum-verified rsync of `data/receipts` → chown uid 1000 → run initial Dropbox bulk mirror and count-verify (cutover is not complete without it; do **not** enable the sync cron line before verification passes — `rclone copy` is additive, so a bad cutover mirrored to Dropbox never self-cleans) → supervised cycle, then enable schedule
- No sequence resync required — verified: no sequences, serial, or identity columns exist in either schema (UUID v7 defaults throughout)
- source_id stability across machines is verified (IMAP Message-ID / content-hash fallback, GDrive file ID) — the first server cycle re-enumerates the full mailbox but skips everything already known
- `ingest_log` is load-bearing data, not "just a log": idempotency is the *union* of `receipts.source_id` and `ingest_log.source_id` — if only `receipts` survives the restore, historical skipped/failed items get reprocessed (LLM spend, and previously-skipped items may insert under model drift)
- Retire: point of no return is the first server cycle that ingests genuinely new mail — before it, abort = stop scheduler, drop server DB, re-restore from the retained dump; after it, the laptop DB is stale and recovery means reverse-cutover, not rollback. Drop dev pg only after an explicit criterion — e.g. 7 days with zero `failures` growth, no falling-behind warnings, and Dropbox count parity — and take a final dump immediately before the drop (the plan's only irreversible step)

**Test scenarios:**
Test expectation: none — operational cutover; the verification queries below are the acceptance test.

**Verification:**
- Strongest single number: distinct `source_id` union count (receipts ∪ ingest_log) on the server equals the value captured from the source at dump time; plus per-table row counts and the per-status ingest_log breakdown (success/skipped/failed) — the breakdown is recorded in the runbook as the monitoring baseline. Note: receipts ≫ ingest_log rows is *expected* (pre-DLQ-era receipts have no log rows) — do not "fix" it
- Verification queries run **as the app role over the app DSN**, not as superuser — the only check that catches the ownership/grant lockout failure class; include the idempotency UNION query itself
- Referential file check: every `pdf_path` in the restored DB resolves to an existing file under the server store root (0 missing); orphan files counted separately (tolerable)
- Schema integrity: `pg_trgm` extension present and the vendor trigram index exists post-restore (a partially-errored restore can leave counts right and indexes silently missing)
- First-cycle duplicate check, made concrete: zero `ingest_log` rows created after cutover whose `source_id` belongs to a receipt with pre-cutover `created_at` — duplicates cannot appear as double rows (UNIQUE), only as fresh log rows for old items
- A known historical receipt (e.g., a known utility or lease receipt) is searchable and `show`-able via the laptop wrapper

---

- U7. **Block network egress during HTML-to-PDF rendering**

**Goal:** A rendered email cannot make outbound network requests: receipt HTML is third-party content processed unattended 96×/day with Chromium's sandbox disabled, in a container holding every credential the service owns — egress blocking removes the exfiltration/phone-home path if a renderer exploit ever lands, and as a side effect makes PDFs deterministic (no remote images/trackers fetched at render time).

**Requirements:** Plan-local security hardening (no origin R-ID; accepted during document review in place of risk-acceptance)

**Dependencies:** None (parallel with U1/U2; must land before first unattended cycle, i.e., before U3 goes live)

**Files:**
- Modify: `src/receipt_index/renderer.py`
- Test: `tests/unit/test_renderer.py`

**Approach:**
- Playwright path: intercept all requests in the render context and abort anything that is not the inline document itself (route-level block; no remote fetches)
- weasyprint path: supply a URL fetcher that refuses remote URLs, for parity
- Expect blank boxes where receipts referenced remote images — acceptable; the receipt's text content is what extraction and reconciliation use

**Patterns to follow:**
- Existing renderer structure in `src/receipt_index/renderer.py` (Playwright primary, weasyprint fallback)

**Test scenarios:**
- Happy path: HTML with only inline content renders to a non-empty PDF with egress blocking active
- Happy path: HTML referencing a remote image renders successfully with the image blocked (no network attempt, no exception)
- Error path: a render must not hang or fail outright because a remote resource is unreachable-by-policy (blocked ≠ timeout)
- Integration: weasyprint fallback path also refuses a remote URL via the custom fetcher

**Verification:**
- A render of a fixture containing remote references completes with zero outbound network requests observed (assertable via Playwright's route interception in tests); both engines produce non-empty PDFs

---

## System-Wide Impact

- **Interaction graph:** dev `docker-compose.yml`, Makefile test targets, and CI are untouched; the only shared-surface change is `Dockerfile` (U1) — CI builds of the image (if any) get bigger/slower, and `src/receipt_index/cli.py` (U2) — all CLI commands gain the log clamp
- **Error propagation:** ingest exit 1 on any failed item is a *per-cycle* signal, not a stuck state (failed items are DLQ'd and never retried); supercronic logs the non-zero exit and fires the next tick normally; rclone sync is deliberately decoupled from ingest exit codes
- **State lifecycle risks:** DLQ permanence is the sharp edge — a receipt that fails once (e.g., rendering gap) is never retried. Pre-cutover render smoke tests (U1) and a post-deploy `failures` check in the runbook mitigate; recovery path (delete the failed `ingest_log` rows to force retry) documented in the runbook
- **API surface parity:** none — no CLI or schema changes beyond log levels
- **Integration coverage:** U1's in-container render tests are the load-bearing cross-layer proof; everything else is validated operationally per unit Verification
- **Unchanged invariants:** dev workflow (`docker compose up -d` + GreenMail + `make test-integration`), migration file contents, DB schema, receipt file naming, and the CLI's command surface all stay exactly as they are

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Rendering deps gap → silent permanent data loss via DLQ | U1 first; in-container render tests for both engines; runbook step checks `failures` after first cycles; documented DLQ-row deletion recovery |
| Dropbox token refresh breaks (config mounted wrong) | Whole-dir read-write mount (rename-safe); U4 verification includes a >4h unattended refresh check |
| Shared rclone app ID rate limits | Custom Dropbox app ID from day one |
| Overlapping ingest runs | supercronic default no-overlap; app-level UNIQUE(source_id) makes a worst-case race non-corrupting |
| pg18 volume path mistake loses data on restart | Volume at `/var/lib/postgresql` (known learning, already correct in dev compose); runbook copies the pattern |
| IMAP poll cost grows with mailbox size (the O(N) IMAP enumeration issue) | Accepted per origin scope; 15-min cadence revisited if cycles start approaching the interval (supercronic falling-behind warnings make this visible) |
| Cutover races (laptop and server both ingesting) | Enforced laptop stop (config renamed so accidental ingest can't connect); dump taken after last laptop run; idempotency union makes accidental overlap non-corrupting |
| Restore leaves app role locked out (dev init script lacks `receipt_index` owner role; dev DB-name grant aborts container init) | Production-specific `deploy/db-init/` role script mirroring the source role set; restore without `--no-owner`; verification queries run as the app role |
| Restore into a non-empty server DB (U5 smoke leftovers) corrupts migration state or collides on UNIQUE(source_id) | Fresh-database restore precondition; pre-cutover smoke is `--dry-run` only; server `ingest_log` verified empty immediately before restore |
| Sync copies a PDF mid-write (no-overlap is per cron line) | Self-heals on next pass under plain `copy`; blocks adopting `--immutable` until accepted; count-parity check is the detector |
| Renderer exploit via malicious email HTML (Chromium sandbox disabled in-container, unattended processing) | U7 blocks all render-time network egress — a compromised render cannot phone home or exfiltrate the container's credentials; full seccomp sandbox enablement remains a future option |
| Server disk/host failure after dev pg retirement destroys the only DB copy | Nightly rotating pg_dump into the Dropbox-mirrored `data/backups/` tree; restore-from-Dropbox procedure in the runbook |

---

## Documentation / Operational Notes

- `docs/user/server-deployment.md` becomes the canonical ops doc (deploy, upgrade, Go/No-Go gates, cutover appendix, DLQ recovery, monitoring baseline, SSH wrapper)
- Weekly ops routine (baseline-driven, see U5): full per-status ingest_log breakdown vs recorded baseline (skipped growth matters as much as failed); log scan for nonzero exits and falling-behind warnings; Dropbox newest-file freshness as the no-SSH delivery check
- Future work (named, not scoped): dead-man's-snitch ping (healthchecks.io-style) as a third crontab line after successful cycles — absence-of-ping covers crash-loop, falling-behind, and config breakage in one mechanism
- Anthropic spend: polling itself costs no LLM calls; per-poll cost is IMAP/GDrive enumeration only

---

## Sources & References

- **Origin document:** [docs/brainstorms/2026-08-01-always-on-service-requirements.md](../brainstorms/2026-08-01-always-on-service-requirements.md)
- Related code: `Dockerfile`, `docker-compose.yml`, `Makefile`, `src/receipt_index/{cli,config,renderer,repository,store}.py`, `db/init/01-create-roles.sql`
- Related issues: O(N) IMAP enumeration (accepted, out of scope)
- External docs: github.com/aptible/supercronic · rclone.org/dropbox · rclone.org/remote_setup · playwright.dev/python/docs/docker
