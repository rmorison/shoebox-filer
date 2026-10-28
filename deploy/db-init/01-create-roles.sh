#!/bin/bash
#
# Production role bootstrap for the compose Postgres instance.
#
# The official postgres image runs every *.sh and *.sql in
# /docker-entrypoint-initdb.d exactly once, on first boot of an empty data
# directory. This is a .sh (not a .sql) so the database name and the
# role passwords come from the container environment instead of being
# hardcoded — the defect that makes db/init/01-create-roles.sql (dev) unusable
# in production, where it would abort init under ON_ERROR_STOP by granting on a
# database name that does not exist.
#
# Roles created, at the three levels of the Database Standards:
#   receipt_index            LOGIN  — _all. Owns the database and, after a
#                                     migration or a restore, every object in
#                                     it. MIGRATION_DATABASE_URL connects here.
#   receipt_index_write      LOGIN  — _write. The application's login
#                                     (DATABASE_URL). Member of
#                                     receipt_index_dev_write only.
#   receipt_index_read       LOGIN  — _read. Backups, forensics and the MCP
#                                     server (RECEIPT_INDEX_READ_DATABASE_URL,
#                                     RECEIPT_INDEX_MCP_DATABASE_URL). Member
#                                     of receipt_index_dev_read only.
#   receipt_index_dev_all    NOLOGIN — grant target of migrations 000002/000004.
#   receipt_index_dev_write  NOLOGIN — grant target of migrations 000002/000004+.
#   receipt_index_dev_read   NOLOGIN — grant target of migration 000002+.
#
# The three _dev_* roles are NOLOGIN members of nothing; receipt_index is
# granted membership in each. It owns every object anyway, so the
# memberships add nothing to it. _dev_all in particular is NOT optional: a
# fresh (non-restored) database cannot pass `make migrate-up` without it,
# because 000002 and 000004 GRANT to that role by name.
#
# The write and read logins get their privileges only from the migrations'
# grants to their _dev_* role, so the column-level grants bind the
# application, and it can't drop or disable the history guard triggers,
# which only the owner can.
#
# Roles are cluster-level and pg_dump doesn't carry them. A disaster-recovery
# rebuild gets them back from this script, run on the new empty volume with
# the restored .env, before the restore.
#
# The "_dev_" naming is a historical artifact of the docker dev setup that the
# migration files bake in by name. Renaming them is a migration change, not a
# deployment change.

set -euo pipefail

: "${POSTGRES_DB:?POSTGRES_DB must be set (database name)}"
: "${POSTGRES_USER:?POSTGRES_USER must be set (superuser name)}"
: "${RECEIPT_INDEX_PASSWORD:?RECEIPT_INDEX_PASSWORD must be set (password for the receipt_index owner role)}"
: "${RECEIPT_INDEX_WRITE_PASSWORD:?RECEIPT_INDEX_WRITE_PASSWORD must be set (password for the receipt_index_write application login)}"
: "${RECEIPT_INDEX_READ_PASSWORD:?RECEIPT_INDEX_READ_PASSWORD must be set (password for the receipt_index_read login)}"

echo "db-init: creating roles in database '${POSTGRES_DB}'"

psql \
    --set ON_ERROR_STOP=1 \
    --username "${POSTGRES_USER}" \
    --dbname "${POSTGRES_DB}" \
    --set app_password="${RECEIPT_INDEX_PASSWORD}" \
    --set write_password="${RECEIPT_INDEX_WRITE_PASSWORD}" \
    --set read_password="${RECEIPT_INDEX_READ_PASSWORD}" \
    --set db="${POSTGRES_DB}" \
    <<'EOSQL'
-- 1. Application login role. Created without a password, then ALTERed, so the
--    secret is passed as a psql variable (:'app_password' is quoted as a SQL
--    literal by psql) rather than interpolated by the shell.
SELECT 'CREATE ROLE receipt_index LOGIN'
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'receipt_index')
\gexec

ALTER ROLE receipt_index WITH LOGIN INHERIT PASSWORD :'app_password';

-- 2. Grant-target roles referenced by name in db/migrations/receipt/*.sql.
--    NOLOGIN: they exist only to carry privileges, which receipt_index
--    inherits via membership.
DO $$
DECLARE
    target text;
BEGIN
    FOREACH target IN ARRAY ARRAY[
        'receipt_index_dev_all',
        'receipt_index_dev_write',
        'receipt_index_dev_read'
    ] LOOP
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = target) THEN
            EXECUTE format('CREATE ROLE %I NOLOGIN', target);
        END IF;
        EXECUTE format('GRANT %I TO receipt_index', target);
    END LOOP;
END $$;

-- 3. The application and read logins. Each is a member of one grant-target
--    role and nothing else, so it holds exactly what the migrations grant
--    that role. Passwords are passed the same way as the owner's.
SELECT 'CREATE ROLE receipt_index_write LOGIN'
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'receipt_index_write')
\gexec
ALTER ROLE receipt_index_write
    WITH LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS
    PASSWORD :'write_password';
GRANT receipt_index_dev_write TO receipt_index_write;

SELECT 'CREATE ROLE receipt_index_read LOGIN'
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'receipt_index_read')
\gexec
ALTER ROLE receipt_index_read
    WITH LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS
    PASSWORD :'read_password';
GRANT receipt_index_dev_read TO receipt_index_read;

-- 4. Database-level privileges and ownership.
GRANT CONNECT ON DATABASE :"db" TO receipt_index;
GRANT CONNECT ON DATABASE :"db" TO receipt_index_dev_all;
GRANT CONNECT ON DATABASE :"db" TO receipt_index_dev_write;
GRANT CONNECT ON DATABASE :"db" TO receipt_index_dev_read;
GRANT CONNECT ON DATABASE :"db" TO receipt_index_write;
GRANT CONNECT ON DATABASE :"db" TO receipt_index_read;

-- receipt_index owns the database so it can create schemas, and so the
-- cutover restore (run without --no-owner) can reassign ownership to it.
ALTER DATABASE :"db" OWNER TO receipt_index;

-- PostgreSQL 15+ only: schema `public` is no longer world-writable, so its
-- ownership must be handed to the owner role, receipt_index, or the first
-- migration's DDL fails with "permission denied for schema public".
ALTER SCHEMA public OWNER TO receipt_index;
EOSQL

echo "db-init: roles ready (receipt_index owns database '${POSTGRES_DB}'; logins receipt_index_write, receipt_index_read)"
