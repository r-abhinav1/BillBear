-- BillBear initial schema (Postgres / Supabase).
-- Normalized so concurrent writers touch different rows; the room row is the
-- per-room lock (SELECT ... FOR UPDATE) for anything that must be atomic.

CREATE TABLE IF NOT EXISTS rooms (
    code               text PRIMARY KEY CHECK (code ~ '^[A-Z0-9]{6}$'),
    room_name          text NOT NULL CHECK (char_length(room_name) BETWEEN 1 AND 60),
    num_people         integer NOT NULL CHECK (num_people BETWEEN 1 AND 50),
    host_recovery_hash text NOT NULL,
    ocr_source         text NOT NULL DEFAULT 'manual',
    -- Totals and receipt metadata only (subtotal, taxes, total, restaurant...).
    -- Line items live in `items`.
    ocr_data           jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at         timestamptz NOT NULL DEFAULT now(),
    last_activity_at   timestamptz NOT NULL DEFAULT now(),
    finalized_at       timestamptz
);

CREATE INDEX IF NOT EXISTS rooms_last_activity_idx ON rooms (last_activity_at);

CREATE TABLE IF NOT EXISTS participants (
    id            bigserial PRIMARY KEY,
    room_code     text NOT NULL REFERENCES rooms(code) ON DELETE CASCADE,
    name          text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 30),
    -- casefolded name; uniqueness is case/whitespace-insensitive
    name_key      text NOT NULL,
    -- sha256 of the device's random user id; NULL after a host "reset device"
    user_id_hash  text,
    is_host       boolean NOT NULL DEFAULT false,
    joined_at     timestamptz NOT NULL DEFAULT now(),
    submitted_at  timestamptz,
    submission_id text,
    UNIQUE (room_code, name_key)
);

-- Exactly one host per room.
CREATE UNIQUE INDEX IF NOT EXISTS participants_one_host_idx
    ON participants (room_code) WHERE is_host;
CREATE INDEX IF NOT EXISTS participants_uid_idx
    ON participants (room_code, user_id_hash);

CREATE TABLE IF NOT EXISTS items (
    id        bigserial PRIMARY KEY,
    room_code text NOT NULL REFERENCES rooms(code) ON DELETE CASCADE,
    position  integer NOT NULL,
    name      text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 80),
    -- NULL = price unknown (shown as N/A, counts as 0)
    price     numeric(12, 2) CHECK (price BETWEEN -10000000 AND 10000000)
);

CREATE INDEX IF NOT EXISTS items_room_idx ON items (room_code, position);

CREATE TABLE IF NOT EXISTS claims (
    participant_id bigint NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
    item_id        bigint NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    PRIMARY KEY (participant_id, item_id)
);

CREATE INDEX IF NOT EXISTS claims_item_idx ON claims (item_id);

-- Fixed-window rate limiting (replaces Redis).
CREATE TABLE IF NOT EXISTS rate_limits (
    bucket       text NOT NULL,
    window_start timestamptz NOT NULL,
    count        integer NOT NULL DEFAULT 0,
    PRIMARY KEY (bucket, window_start)
);

CREATE INDEX IF NOT EXISTS rate_limits_window_idx ON rate_limits (window_start);
