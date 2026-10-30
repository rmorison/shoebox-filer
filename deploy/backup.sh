#!/bin/sh
#
# Database backup, run from deploy/crontab inside the scheduler container.
# Writes a pg_dump custom-format archive into the bind-mounted
# /app/data/backups directory under a fixed set of names, so the directory
# and the rclone mirror of it stay bounded (`rclone copy` never deletes at
# the destination, but it does overwrite changed files — a fixed name set is
# what keeps Dropbox from growing):
#
#   backup.sh [nightly]   receipt_index-<1..7>.dump, day of week (7 names)
#   backup.sh monthly     receipt_index-month-<01..12>.dump, month (12 names)
#
# A second run on the same day, or in the same month, replaces that file.
#
# Why a script instead of an inline crontab command:
#   * supercronic hands the whole command line to `$SHELL -c` and does NOT
#     implement classic cron's `%`-means-newline rule, so `date +%u` inline
#     would in fact work — but relying on that divergence from vixie-cron is
#     the kind of thing that breaks silently if the scheduler is ever swapped.
#   * The dump-to-temp-then-rename dance below needs more than one statement.
#
# Custom format (-Fc) is deliberate: it is already compressed, and the cutover
# and disaster-recovery procedures restore with `pg_restore` *without*
# --no-owner / --no-acl so that ownership lands back on receipt_index.
#
# Written to a .tmp name and renamed into place so a concurrent rclone sync
# tick can never mirror a half-written dump.

set -eu

# Check the argument before connecting, so a bad crontab line fails fast.
kind="${1:-nightly}"
case "${kind}" in
    nightly | monthly) ;;
    *)
        echo "usage: backup.sh [nightly|monthly]" >&2
        exit 2
        ;;
esac

# Dump as the read login when RECEIPT_INDEX_READ_DATABASE_URL is set: it can
# read every table, golang-migrate's version tables included (migration
# 000014), and nothing else. Without it, fall back to DATABASE_URL, as before
# the separate logins. Once DATABASE_URL is the write login, that fallback
# fails on schema_migrations_public, so set the read URL first (see
# docs/user/server-deployment.md, "Separate database logins").
DUMP_URL="${RECEIPT_INDEX_READ_DATABASE_URL:-${DATABASE_URL:-}}"
: "${DUMP_URL:?RECEIPT_INDEX_READ_DATABASE_URL or DATABASE_URL must be set in the container environment}"
if [ -z "${RECEIPT_INDEX_READ_DATABASE_URL:-}" ]; then
    echo "backup: RECEIPT_INDEX_READ_DATABASE_URL is unset; dumping through DATABASE_URL"
fi
# Say which login the dump runs as, so a run that falls back to another login
# shows it in the log even when it succeeds.
dump_user="$(psql --no-psqlrc --no-password -Atc 'SELECT current_user' "${DUMP_URL}")"

BACKUP_DIR="${BACKUP_DIR:-/app/data/backups}"

mkdir -p "${BACKUP_DIR}"

if [ "${kind}" = monthly ]; then
    # 01..12, the month the dump is taken in
    target="${BACKUP_DIR}/receipt_index-month-$(date +%m).dump"
else
    # 1..7, Monday..Sunday
    target="${BACKUP_DIR}/receipt_index-$(date +%u).dump"
fi
tmp="${target}.tmp"

# A failed dump must not strand a partial .tmp: the mirror is additive-only,
# so anything it uploads stays on Dropbox forever. Belt (this trap) and
# suspenders (--exclude "*.tmp" on the crontab mirror line).
trap 'rm -f "${tmp}"' EXIT

echo "backup: ${kind} dump as ${dump_user} to ${target}"

pg_dump --format=custom --no-password --file="${tmp}" "${DUMP_URL}"
mv -f "${tmp}" "${target}"

echo "backup: wrote ${target} ($(wc -c < "${target}") bytes)"
