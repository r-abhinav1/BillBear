-- Supabase serves an auto-generated web API over the public schema, reachable
-- with the project's anon key. Row level security with no policies shuts that
-- door: the API roles (anon, authenticated) can no longer read or write any
-- row. The app is unaffected because it connects as the role that owns these
-- tables, and a table's owner is not subject to its row level security.
--
-- Any table added in a later migration needs the same line.
ALTER TABLE rooms             ENABLE ROW LEVEL SECURITY;
ALTER TABLE participants      ENABLE ROW LEVEL SECURITY;
ALTER TABLE items             ENABLE ROW LEVEL SECURITY;
ALTER TABLE claims            ENABLE ROW LEVEL SECURITY;
ALTER TABLE rate_limits       ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_heartbeat     ENABLE ROW LEVEL SECURITY;
ALTER TABLE schema_migrations ENABLE ROW LEVEL SECURITY;
