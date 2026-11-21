-- Tables and roles for the admin page. Safe to run more than once.
-- Runs at app start-up and from scripts/migrate.py.

-- 1. Who is an admin.
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE;

-- 2. Two sample bank accounts for the concurrency tab. Nothing else reads this table.
--    The CHECK is what makes an overdrawn transfer fail.
CREATE TABLE IF NOT EXISTS concurrency_accounts (
    name    VARCHAR(20) PRIMARY KEY,
    balance NUMERIC(12,2) NOT NULL CHECK (balance >= 0)
);

-- 3. A role that can only read (and never sees password hashes) for the SQL console, which
--    switches to it for every query. If the login can't create roles the console just keeps
--    its other checks.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'resalewise_readonly') THEN
    CREATE ROLE resalewise_readonly NOLOGIN;
  END IF;
  EXECUTE format('GRANT resalewise_readonly TO %I', current_user);
  GRANT USAGE ON SCHEMA public TO resalewise_readonly;
  GRANT SELECT ON ALL TABLES IN SCHEMA public TO resalewise_readonly;
  REVOKE SELECT ON users FROM resalewise_readonly;
  GRANT SELECT (user_id, email, name, created_at, is_admin) ON users TO resalewise_readonly;
EXCEPTION WHEN insufficient_privilege THEN
  RAISE NOTICE 'read-only role not set up (this login may not create roles): the SQL console keeps its other guards';
END $$;

-- 4. pageinspect lets the index tab read how many levels the tree has. Optional, without it
--    the page shows everything except the height.
DO $$
BEGIN
  CREATE EXTENSION IF NOT EXISTS pageinspect;
EXCEPTION WHEN OTHERS THEN
  RAISE NOTICE 'pageinspect not available here: the index analysis will not show tree heights';
END $$;
