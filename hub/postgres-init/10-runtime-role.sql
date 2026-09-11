-- The role the Hub SERVES traffic as, which is deliberately not the role
-- that OWNS the schema.
--
-- WHY THIS FILE EXISTS
-- --------------------
-- Postgres skips every row-level-security policy for a superuser or a role
-- holding BYPASSRLS, silently: no error, no warning, no log line. The
-- policies still exist, pg_policies still lists them, an audit still finds
-- them, and they do nothing. The official Postgres image makes POSTGRES_USER
-- the cluster superuser, and this project's docker-compose.yml pointed
-- HUB_DATABASE_URL at exactly that role -- so the shipped evaluation stack
-- installed the tenant-isolation policies from migration d5c8b3a91e77 and
-- bypassed all of them.
--
-- This script creates the missing half: a login role that owns nothing, is
-- not a superuser, and has no BYPASSRLS, holding exactly the DML rights the
-- server needs and no DDL rights at all. Connect the app as that role and
-- the policies bite. hub/db.py:check_row_level_security refuses to start
-- when they cannot.
--
-- WHEN THIS RUNS
-- --------------
-- The postgres image runs everything in /docker-entrypoint-initdb.d ONCE,
-- at first initialisation of an empty data directory, as POSTGRES_USER --
-- before the `migrate` service creates any table. That ordering is what
-- makes ALTER DEFAULT PRIVILEGES below the right tool rather than a GRANT
-- over existing tables: there are no tables yet, and every table alembic
-- creates afterwards picks the grants up automatically. A GRANT written
-- against today's schema would silently fail to cover tomorrow's migration.
--
-- FOR A DEPLOYMENT THAT IS NOT THIS COMPOSE STACK (a managed Postgres, an
-- existing cluster) run the same statements once by hand as the owner, with
-- a real password from your secret store. hub/DEPLOYMENT.md has the
-- walkthrough. An existing database with tables already in it additionally
-- needs the one-off GRANT over what is already there:
--
--     GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
--         TO commontrace_app;
--     GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO commontrace_app;

\set app_password `echo "${POSTGRES_APP_PASSWORD:-change-me-not-for-production}"`

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'commontrace_app') THEN
        CREATE ROLE commontrace_app LOGIN;
    END IF;
END
$$;

ALTER ROLE commontrace_app WITH PASSWORD :'app_password';

-- Explicitly NOT superuser and NOT bypassrls. Stated rather than assumed:
-- CREATE ROLE defaults to both off, but this is the single property the
-- whole file exists to guarantee, so it is written down where an operator
-- reviewing the deployment can see it.
ALTER ROLE commontrace_app WITH NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;

-- :"DBNAME" is psql's own built-in variable for the database this script is
-- connected to, which the postgres image's entrypoint sets to POSTGRES_DB --
-- so this stays correct if the database is renamed, with nothing to keep in
-- sync by hand.
GRANT CONNECT ON DATABASE :"DBNAME" TO commontrace_app;

-- USAGE, not CREATE: the runtime role may resolve names in the schema and
-- may not add objects to it. Migrations are the owner's job, run as a
-- separate one-shot service.
GRANT USAGE ON SCHEMA public TO commontrace_app;

-- Every table and sequence the OWNER creates from here on -- i.e. everything
-- `alembic upgrade head` is about to create, and everything a future
-- migration adds -- grants these rights to the runtime role automatically.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO commontrace_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO commontrace_app;

-- hub_rate_limit_buckets is the one table the application itself creates at
-- runtime rather than alembic (hub/abuse.py:PostgresRateLimiter, used when
-- HUB_RATE_LIMIT_BACKEND=postgres). A role with no CREATE on the schema
-- cannot run that statement -- Postgres checks the schema privilege before
-- the IF NOT EXISTS existence check, so it is refused even when the table is
-- already there. Created here by the owner so the runtime role never needs
-- to; the DDL is kept identical to _RATE_LIMIT_DDL in hub/abuse.py.
CREATE TABLE IF NOT EXISTS hub_rate_limit_buckets (
    limiter_name text NOT NULL,
    bucket_key text NOT NULL,
    tokens double precision NOT NULL,
    last_refill timestamptz NOT NULL,
    PRIMARY KEY (limiter_name, bucket_key)
);
GRANT SELECT, INSERT, UPDATE, DELETE ON hub_rate_limit_buckets TO commontrace_app;
