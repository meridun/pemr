-- 005_dedup_occurrence: occurrence numbering on the dedup identity family (issue #58).
--
-- `review-conflicts --resolve --keep both` admits a genuine repeat (two same-day draws on
-- a report that prints no collection times) alongside the row it collided with. The two
-- rows share an identity but cannot share a `dedup_key` (it is UNIQUE), so the family gets
-- an explicit numbering:
--
--   dedup_base       = hash(person_id | identity fields...)   -- shared by the whole family
--   dedup_occurrence = 0 for the first row, 1, 2, ... for admitted repeats
--   dedup_key        = dedup_base                    when occurrence = 0
--                    = hash(dedup_base | occurrence) when occurrence > 0
--
-- Occurrence 0 reproduces today's key byte-for-byte, so this migration is a pure column
-- copy: no rekey, no re-commit, no invalidated data.
--
-- `dedup_base` is denormalized (it is recomputable from the payload) on purpose: it makes
-- "the occurrence family" one indexed lookup instead of probing hash(base|1), hash(base|2)
-- ... which breaks on holes if a sibling is ever removed. `pemr.dedup` maintains the
-- invariant `dedup_base IS NOT NULL` on every write path (insert / rekey / reassign), and
-- the commit-time family lookup relies on it.

ALTER TABLE lab_result  ADD COLUMN dedup_base       TEXT;
ALTER TABLE lab_result  ADD COLUMN dedup_occurrence INTEGER NOT NULL DEFAULT 0;
UPDATE lab_result  SET dedup_base = dedup_key;
CREATE INDEX idx_lab_result_dedup_base  ON lab_result(dedup_base);

ALTER TABLE medication  ADD COLUMN dedup_base       TEXT;
ALTER TABLE medication  ADD COLUMN dedup_occurrence INTEGER NOT NULL DEFAULT 0;
UPDATE medication  SET dedup_base = dedup_key;
CREATE INDEX idx_medication_dedup_base  ON medication(dedup_base);

ALTER TABLE procedure   ADD COLUMN dedup_base       TEXT;
ALTER TABLE procedure   ADD COLUMN dedup_occurrence INTEGER NOT NULL DEFAULT 0;
UPDATE procedure   SET dedup_base = dedup_key;
CREATE INDEX idx_procedure_dedup_base   ON procedure(dedup_base);

ALTER TABLE appointment ADD COLUMN dedup_base       TEXT;
ALTER TABLE appointment ADD COLUMN dedup_occurrence INTEGER NOT NULL DEFAULT 0;
UPDATE appointment SET dedup_base = dedup_key;
CREATE INDEX idx_appointment_dedup_base ON appointment(dedup_base);

ALTER TABLE observation ADD COLUMN dedup_base       TEXT;
ALTER TABLE observation ADD COLUMN dedup_occurrence INTEGER NOT NULL DEFAULT 0;
UPDATE observation SET dedup_base = dedup_key;
CREATE INDEX idx_observation_dedup_base ON observation(dedup_base);
