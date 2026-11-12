-- Dietary class of each line item, set by the bill scanner and editable by the host.
-- "other" = not food/drink, or the scanner could not tell.
ALTER TABLE items
    ADD COLUMN IF NOT EXISTS category text NOT NULL DEFAULT 'other'
    CHECK (category IN ('veg', 'nonveg', 'egg', 'alcohol', 'other'));
