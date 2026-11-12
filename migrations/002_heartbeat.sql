-- Written by the daily cron (/api/cron/cleanup). Supabase pauses free projects
-- after ~7 days without activity; a real write each day keeps it active and
-- lets you check when the cron last ran:
--   SELECT * FROM app_heartbeat;
CREATE TABLE IF NOT EXISTS app_heartbeat (
    id         integer PRIMARY KEY CHECK (id = 1),
    last_run   timestamptz NOT NULL DEFAULT now(),
    run_count  bigint NOT NULL DEFAULT 1
);
