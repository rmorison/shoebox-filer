-- Create application roles on database initialization.
-- These roles are used by migrations and application code.
-- Docker Compose mounts this file into /docker-entrypoint-initdb.d/
-- so it runs once when the database is first created.

DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'receipt_index_dev_all') THEN
        CREATE ROLE receipt_index_dev_all WITH LOGIN PASSWORD 'localpass';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'receipt_index_dev_write') THEN
        CREATE ROLE receipt_index_dev_write WITH LOGIN PASSWORD 'localpass';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'receipt_index_dev_read') THEN
        CREATE ROLE receipt_index_dev_read WITH LOGIN PASSWORD 'localpass';
    END IF;
END $$;

-- Grant database-level connect to all app roles
GRANT CONNECT ON DATABASE receipt_index_dev TO receipt_index_dev_all;
GRANT CONNECT ON DATABASE receipt_index_dev TO receipt_index_dev_write;
GRANT CONNECT ON DATABASE receipt_index_dev TO receipt_index_dev_read;
