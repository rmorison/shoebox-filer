#!/bin/sh
#
# Nightly database backup, run from deploy/crontab inside the scheduler
# container. Writes a pg_dump custom-format archive into the bind-mounted
# /app/data/backups directory using day-of-week rotation, so the directory
# holds at most 7 files and the rclone mirror of it stays bounded too
# (`rclone copy` never deletes at the destination, but it does overwrite
# changed files — a fixed 7-name set is what keeps Dropbox from growing).
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

: "${DATABASE_URL:?DATABASE_URL must be set in the container environment}"

BACKUP_DIR="${BACKUP_DIR:-/app/data/backups}"

mkdir -p "${BACKUP_DIR}"

# 1..7, Monday..Sunday
dow="$(date +%u)"
target="${BACKUP_DIR}/receipt_index-${dow}.dump"
tmp="${target}.tmp"

# A failed dump must not strand a partial .tmp: the mirror is additive-only,
# so anything it uploads stays on Dropbox forever. Belt (this trap) and
# suspenders (--exclude "*.tmp" on the crontab mirror line).
trap 'rm -f "${tmp}"' EXIT

echo "backup: dumping to ${target}"

pg_dump --format=custom --no-password --file="${tmp}" "${DATABASE_URL}"
mv -f "${tmp}" "${target}"

echo "backup: wrote ${target} ($(wc -c < "${target}") bytes)"
