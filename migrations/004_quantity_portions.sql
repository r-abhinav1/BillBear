-- How many of a dish the bill lists ("5 x Masala Dosa"), and how many of them
-- each person says they had. The price on `items` stays the line total; a
-- person's share of it is their portions over all portions claimed.
ALTER TABLE items
    ADD COLUMN IF NOT EXISTS quantity integer NOT NULL DEFAULT 1
    CHECK (quantity BETWEEN 1 AND 99);

ALTER TABLE claims
    ADD COLUMN IF NOT EXISTS portions integer NOT NULL DEFAULT 1
    CHECK (portions BETWEEN 1 AND 99);
