-- When the host lowers how many of a dish the bill lists, a claim people hold
-- that no longer fits is trimmed (storage._fit_held_claims). `lowered_from`
-- remembers what it was before, so the pick screen can say what changed. It
-- stays empty for a claim nobody trimmed, and clears when the person sends
-- again, because sending replaces their claims.
ALTER TABLE claims
    ADD COLUMN IF NOT EXISTS lowered_from integer
    CHECK (lowered_from BETWEEN 1 AND 99);
