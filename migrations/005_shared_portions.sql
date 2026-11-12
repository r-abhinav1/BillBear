-- For a dish the bill lists several of, a claim is now either a number of
-- whole units (portions >= 1) or "shared" (portions = 0): splitting whatever
-- nobody claimed outright with the other people who chose shared.
ALTER TABLE claims DROP CONSTRAINT IF EXISTS claims_portions_check;
ALTER TABLE claims ADD CONSTRAINT claims_portions_check CHECK (portions BETWEEN 0 AND 99);
