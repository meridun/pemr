-- 009_record_attestation: human attestation as row provenance (issue #110).
--
-- Sometimes a fact is known to the family - a medication the operator personally manages -
-- but the document that would prove it lives in a portal nobody has pulled yet. Today the
-- only choices are to omit it (a summary that is wrong at the appointment) or to fabricate
-- provenance (forbidden). This migration adds the third representation: "human-attested,
-- source pending".
--
-- **Columns, not a sibling table** - the opposite of 008_curation's shape, deliberately.
-- A curation verdict is a family-level judgment that must outlive the rows it rules on, so
-- it belongs in an overlay keyed by (record_type, dedup_base). An attestation is the row's
-- *own provenance*: it is what stands in for document_id, it means nothing without the row,
-- and it dies with the row. So it lives beside document_id, on the row.
--
-- Every typed table's document_id is already nullable (migrations 001/006: a plain
-- REFERENCES with no NOT NULL); only the Python write path insisted on a live document.
-- Three row states, all derivable - no stored-and-derivable duplication:
--
--   document-sourced   attested_by IS NULL                              (today's only state)
--   live attestation   attested_by IS NOT NULL AND document_id IS NULL  ("needs source")
--   superseded         attested_by IS NOT NULL AND document_id IS NOT NULL
--
-- Supersession is therefore just the attested row acquiring a document_id: the row is never
-- deleted and the attestation columns are never cleared, so "X said so on D before the
-- document arrived" is retained as history on the fact itself. An explicit
-- superseded_at/superseded_by pair was rejected - both are derivable from document_id, and a
-- second source of truth for provenance is exactly what layer-2 cannot afford.
--
-- No CHECK and no FK: SQLite's ALTER TABLE ADD COLUMN can add neither, and the invariants are
-- Python-side (pemr/attestations.py), which is also 007's stated choice. No index: access is a
-- per-table `WHERE attested_by IS NOT NULL` scan over tables `rekey` and `verify` already scan
-- whole.
--
-- The columns are absent from dedup.FIELD_SPECS, so dedup_key/dedup_base/rekey are
-- bit-identical for every stored row, attested or not - "attested rows dedup normally" holds
-- by construction rather than by a parallel code path.
--
-- Semantics:
--   attested_by  the `--attributed-to` value, verbatim; mandatory, no anonymous attestation
--   attested_on  the operator's `--date`: the date the attestation is MADE, not the clinical
--                date (clinical dates stay in the payload columns)
--   attested_at  engine-set ISO8601 UTC, timespec=seconds (as document.ingested_at)

ALTER TABLE lab_result  ADD COLUMN attested_by TEXT;
ALTER TABLE lab_result  ADD COLUMN attested_on TEXT;
ALTER TABLE lab_result  ADD COLUMN attested_at TEXT;

ALTER TABLE medication  ADD COLUMN attested_by TEXT;
ALTER TABLE medication  ADD COLUMN attested_on TEXT;
ALTER TABLE medication  ADD COLUMN attested_at TEXT;

ALTER TABLE procedure   ADD COLUMN attested_by TEXT;
ALTER TABLE procedure   ADD COLUMN attested_on TEXT;
ALTER TABLE procedure   ADD COLUMN attested_at TEXT;

ALTER TABLE appointment ADD COLUMN attested_by TEXT;
ALTER TABLE appointment ADD COLUMN attested_on TEXT;
ALTER TABLE appointment ADD COLUMN attested_at TEXT;

ALTER TABLE observation ADD COLUMN attested_by TEXT;
ALTER TABLE observation ADD COLUMN attested_on TEXT;
ALTER TABLE observation ADD COLUMN attested_at TEXT;

ALTER TABLE allergy     ADD COLUMN attested_by TEXT;
ALTER TABLE allergy     ADD COLUMN attested_on TEXT;
ALTER TABLE allergy     ADD COLUMN attested_at TEXT;

ALTER TABLE condition   ADD COLUMN attested_by TEXT;
ALTER TABLE condition   ADD COLUMN attested_on TEXT;
ALTER TABLE condition   ADD COLUMN attested_at TEXT;
