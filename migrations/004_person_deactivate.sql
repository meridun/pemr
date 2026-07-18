-- 004_person_deactivate: soft-deactivate support for person records (issue #26).
-- Retiring a person record must be reversible and must never destroy ingested
-- medical history, so `pemr person deactivate` sets this timestamp instead of
-- deleting. NULL = active; a non-NULL ISO timestamp = deactivated (hidden from the
-- default `person list`, still visible via `person list --all` and `person show`).
-- `pemr person reactivate` clears it. Hard `person remove` stays available only for a
-- childless record (a typo'd roster entry with nothing ingested yet).

ALTER TABLE person ADD COLUMN deactivated_at TEXT;   -- ISO timestamp; NULL = active
