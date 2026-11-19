-- "Move them to a new phone" unlinks a guest's device so they can rejoin with
-- the same name. Until now the first person to join with that name took the
-- seat, so anyone holding the room code could get there before the guest did.
-- The host is now given a one-time move code for that guest, and only its
-- hash is kept here; rejoining with the name needs the code. It clears once
-- the seat is reclaimed, and a new reset replaces it.
ALTER TABLE participants ADD COLUMN IF NOT EXISTS move_code_hash text;
