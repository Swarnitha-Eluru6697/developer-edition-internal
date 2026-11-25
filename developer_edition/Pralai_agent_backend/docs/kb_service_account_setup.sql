-- Least-privilege role setup for the KB ingestion/retrieval service
-- account (§0.67, GAP_REPORT).
--
-- Real, concrete, run-as-is (against a real target Postgres database —
-- the SAME database the Knowledge Base's own "host/port/dbname" config
-- points at, run by someone who already has admin rights there, e.g.
-- the customer's own DBA for an externally-managed database).
--
-- Why this is now genuinely possible, not just aspirational: since
-- §0.67, this service's own DDL footprint is confined to ONE schema
-- (inaiera_kb) — CREATE SCHEMA IF NOT EXISTS inaiera_kb, plus
-- CREATE/ALTER TABLE and CREATE INDEX only ever inside it. It never
-- creates, alters, or drops anything in "public" or any other schema
-- a customer's own tables might live in (see routers/knowledge_bases.py's
-- own KB_MANAGED_SCHEMA comment for the full reasoning). That's what
-- makes a role scoped to exactly this schema actually sufficient —
-- before that fix, the service could be pointed at an arbitrary table
-- name in any schema, so no meaningfully-scoped role could have worked
-- without breaking ingestion for some configurations.
--
-- Run this ONCE per target database, as a role with CREATEROLE/CREATEDB
-- (or superuser) rights there — then put the new role's own credentials
-- into the Knowledge Base's connection config (host/port/dbname/user/
-- password) instead of a broader admin account.

-- 1. A dedicated login role for the service. Replace the password.
CREATE ROLE inaiera_kb_svc LOGIN PASSWORD 'CHANGE_ME_TO_A_REAL_SECRET';

-- 2. The one schema this service ever actually needs, created here
--    (or left to the service's own real CREATE SCHEMA IF NOT EXISTS on
--    first ingestion — either order works; this makes the ownership
--    explicit up front instead).
CREATE SCHEMA IF NOT EXISTS inaiera_kb AUTHORIZATION inaiera_kb_svc;

-- 3. Real, minimal grants — USAGE (see the schema at all) + CREATE
--    (make new tables/indexes inside it) on inaiera_kb ONLY. No grants
--    anywhere else at all: no USAGE on "public", no CREATEDB, no
--    CREATEROLE, no superuser. A role with only these grants genuinely
--    cannot create, alter, or even see the structure of any table
--    outside this one schema.
GRANT USAGE, CREATE ON SCHEMA inaiera_kb TO inaiera_kb_svc;

-- 4. Real, standing privileges on whatever tables/indexes/sequences
--    this service creates inside inaiera_kb from now on (it already
--    owns anything it creates itself via AUTHORIZATION above, but
--    these DEFAULT PRIVILEGES cover objects created by a different
--    admin/migration role on this service's behalf, if that's ever how
--    this schema gets provisioned instead).
ALTER DEFAULT PRIVILEGES IN SCHEMA inaiera_kb
    GRANT ALL ON TABLES TO inaiera_kb_svc;
ALTER DEFAULT PRIVILEGES IN SCHEMA inaiera_kb
    GRANT ALL ON SEQUENCES TO inaiera_kb_svc;

-- 5. The pgvector extension needs superuser (or a role with
--    CREATE on the database) to install the first time. This is the
--    one real, standing exception to "least privilege" this setup
--    needs — install the extension ONCE, as an admin, then this
--    service's own CREATE EXTENSION IF NOT EXISTS calls become
--    genuine no-ops (Postgres allows a non-owner to run CREATE
--    EXTENSION IF NOT EXISTS against an already-installed extension
--    without erroring, but installing it fresh does need the
--    elevated right) — run once, by an admin, not by inaiera_kb_svc:
--
--   CREATE EXTENSION IF NOT EXISTS vector;
--
-- If your Postgres provider disallows even this (some managed
-- providers pre-install popular extensions, or provide a dedicated
-- "trusted extension" grant path — check your provider's own docs),
-- have that done by whoever manages the database before pointing a
-- Knowledge Base at it.

-- Real verification, after the above — confirms the role genuinely
-- cannot touch anything outside inaiera_kb:
--
--   SET ROLE inaiera_kb_svc;
--   CREATE TABLE inaiera_kb.smoke_test (id int);           -- should succeed
--   DROP TABLE inaiera_kb.smoke_test;                       -- should succeed
--   CREATE TABLE public.should_fail (id int);                -- should FAIL: permission denied for schema public
--   RESET ROLE;
