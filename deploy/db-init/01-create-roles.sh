#!/bin/bash
#
# Production role bootstrap for the compose Postgres instance.
#
# The official postgres image runs every *.sh and *.sql in
# /docker-entrypoint-initdb.d exactly once, on first boot of an empty data
# directory. This is a .sh (not a .sql) so the database name and the
# application password come from the container environment instead of being
# hardcoded — the defect that makes db/init/01-create-roles.sql (dev) unusable
# in production, where it would abort init under ON_ERROR_STOP by granting on a
# database name that does not exist.
#
# Roles created:
#   receipt_index            LOGIN  — the application role. Owns the database
#                                     and, after the cutover restore, every
#                                     object in it. DATABASE_URL connects here.
#   receipt_index_dev_all    NOLOGIN — grant target of migrations 000002/000004.
#   receipt_index_dev_write  NOLOGIN — grant target of migrations 000002/000004+.
#   receipt_index_dev_read   NOLOGIN — grant target of migration 000002+.
#
# The three _dev_* roles are NOLOGIN members of nothing; receipt_index is
# granted membership in each, so it inherits every privilege the migrations
# hand out. _dev_all in particular is NOT optional: a fresh (non-restored)
# database cannot pass `make migrate-up` without it, because 000002 and 000004
# GRANT to that role by name.
#
# The "_dev_" naming is a historical artifact of the docker dev setup that the
# migration files bake in by name. Renaming them is a migration change, not a
# deployment change.

set -euo pipefail

: "${POSTGRES_DB:?POSTGRES_DB must be set (database name)}"
: "${POSTGRES_USER:?POSTGRES_USER must be set (superuser name)}"
: "${RECEIPT_INDEX_PASSWORD:?RECEIPT_INDEX_PASSWORD must be set (password for the receipt_index application role)}"

echo "db-init: creating application roles in database '${POSTGRES_DB}'"

psql \
    --set ON_ERROR_STOP=1 \
    --username "${POSTGRES_USER}" \
    --dbname "${POSTGRES_DB}" \
    --set app_password="${RECEIPT_INDEX_PASSWORD}" \
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

-- 3. Database-level privileges and ownership.
GRANT CONNECT ON DATABASE :"db" TO receipt_index;
GRANT CONNECT ON DATABASE :"db" TO receipt_index_dev_all;
GRANT CONNECT ON DATABASE :"db" TO receipt_index_dev_write;
GRANT CONNECT ON DATABASE :"db" TO receipt_index_dev_read;

-- receipt_index owns the database so it can create schemas, and so the
-- cutover restore (run without --no-owner) can reassign ownership to it.
ALTER DATABASE :"db" OWNER TO receipt_index;

-- PostgreSQL 15+ only: schema `public` is no longer world-writable, so its
-- ownership must be handed to the application role or the first migration's
-- DDL fails with "permission denied for schema public".
ALTER SCHEMA public OWNER TO receipt_index;
EOSQL

echo "db-init: roles ready (receipt_index owns database '${POSTGRES_DB}')"
