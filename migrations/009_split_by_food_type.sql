-- Splitting by food type, an optional second way to split a bill.
--
-- rooms.split_mode:
--   'items'  dish by dish, as before (every existing room)
--   'types'  everyone taps the food types they had; each type's dishes are
--            shared equally by the people who tapped it
--   'counts' the host types how many people had each type and gets a rate card;
--            nobody picks
-- rooms.type_counts: for 'counts', {"veg": 4, "nonveg": 3, ...}.
-- participants.categories: for 'types', the food types a person tapped. Kept
-- per person rather than as claims on dishes, so a dish the host adds or
-- retypes later lands in the right pot.
ALTER TABLE rooms
    ADD COLUMN IF NOT EXISTS split_mode text NOT NULL DEFAULT 'items'
    CHECK (split_mode IN ('items', 'types', 'counts'));

ALTER TABLE rooms
    ADD COLUMN IF NOT EXISTS type_counts jsonb
    CHECK (type_counts IS NULL OR jsonb_typeof(type_counts) = 'object');

ALTER TABLE participants
    ADD COLUMN IF NOT EXISTS categories text[]
    CHECK (
        categories IS NULL
        OR (categories <@ ARRAY['veg', 'nonveg', 'egg', 'alcohol']::text[] AND cardinality(categories) <= 4)
    );
