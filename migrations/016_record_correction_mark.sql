-- 016_record_correction_mark: disclose an in-place correction on the row itself (issue #134).
--
-- `record edit` (012, issue #129) corrects a row's non-key field and ledgers the change,
-- but nothing reads that ledger at read time - by design (012's header: "nothing resolves
-- through this table"). The consequence is the gap #134 names: after a correction, `render`
-- and `query` present the corrected value **identically to a value the document actually
-- stated**. That is the exact twin of #110's failure - a row whose stored value no longer
-- matches its source must never read as verbatim source text.
--
-- **Columns, not an overlay** - the 009 shape, not 008/012's. A correction stamp is the
-- row's *own* provenance caveat: it means nothing without the row, and it must die with the
-- row. So it lives beside document_id and attested_by, on the row.
--
-- **Why not derive it from the ledger at read time.** A `curation.load_verdicts`-style map
-- keyed by (record_type, record_id) would need no new columns, and is unsound. Ledger
-- entries deliberately outlive their row (012 header) and a record id is a reusable rowid
-- alias (001/006), so a stale entry would mark an unrelated later occupant as "corrected" -
-- the #114 hazard, here in its worst form: a false provenance caveat on someone else's
-- fact. Closing it would need a retirement stamp written by both delete paths
-- (records.remove_record, documents.remove_document) plus a join in the render hot path.
-- Storing the mark on the row closes the hazard BY CONSTRUCTION and needs no join.
--
-- The resulting duplication with the ledger is acceptable precisely because the derived
-- form is unsound: the ledger stays the field-level audit trail (old -> new, per field,
-- append-only), and these two columns are only the row-level "this row was corrected, by
-- whom, when" marker that render and query disclose. Last correction wins on the row; the
-- ledger keeps every one. They are reconciled for a human by `record edit --list`, and
-- nothing resolves through either.
--
-- No CHECK and no FK: SQLite's ALTER TABLE ADD COLUMN can add neither, and the invariants
-- stay Python-side (pemr/records.py), which is 007's and 009's stated choice. No index:
-- every reader already has the row in hand.
--
-- The columns are absent from dedup.FIELD_SPECS, so dedup_key/dedup_base/rekey and
-- `record edit`'s own editable set are bit-identical for corrected and uncorrected rows -
-- by construction, not by a denylist. dedup.public_row strips them when NULL, so an
-- uncorrected row's --json / MCP payload keeps the exact key set it had before this
-- migration (the additive-only rule).
--
-- Semantics:
--   edited_at  the newest correction's ISO8601 UTC stamp (as record_edit.edited_at)
--   edited_by  that correction's `--attributed-to`, or NULL - the ledger allows an
--              unattributed correction, so the render has a date-only form
--
-- BACKFILL. Rows corrected before this migration are reconstructed from the newest ledger
-- entry naming them, guarded by the entry's dedup_base breadcrumb (the 010/012 reading of
-- that column: a breadcrumb, never a lookup key). The guard is the one-shot answer to the
-- same row-id-reuse hazard as above - without it the backfill could stamp a later occupant
-- of a recycled id. It trades a false positive for a false negative: a family rekeyed since
-- its correction no longer matches its breadcrumb and is left unmarked. That is the right
-- direction to fail. An unmarked corrected row is today's behaviour and the ledger still
-- holds the truth; a correction caveat on an unrelated fact is a provenance lie, which is
-- the whole class of failure this migration exists to prevent.
--
-- Purely additive: ADD COLUMN + UPDATE, no table rebuild, safe under foreign_keys=ON.
-- record_edit exists since 012 and migrations apply in order, so no guard is needed.
-- Every Python reader goes through .get()/_row_get on a mapping, so a restored pre-016
-- snapshot reads as "not corrected" rather than raising `no such column`.

ALTER TABLE lab_result  ADD COLUMN edited_at TEXT;
ALTER TABLE lab_result  ADD COLUMN edited_by TEXT;

ALTER TABLE medication  ADD COLUMN edited_at TEXT;
ALTER TABLE medication  ADD COLUMN edited_by TEXT;

ALTER TABLE procedure   ADD COLUMN edited_at TEXT;
ALTER TABLE procedure   ADD COLUMN edited_by TEXT;

ALTER TABLE appointment ADD COLUMN edited_at TEXT;
ALTER TABLE appointment ADD COLUMN edited_by TEXT;

ALTER TABLE observation ADD COLUMN edited_at TEXT;
ALTER TABLE observation ADD COLUMN edited_by TEXT;

ALTER TABLE allergy     ADD COLUMN edited_at TEXT;
ALTER TABLE allergy     ADD COLUMN edited_by TEXT;

ALTER TABLE condition   ADD COLUMN edited_at TEXT;
ALTER TABLE condition   ADD COLUMN edited_by TEXT;

UPDATE lab_result SET
  edited_at = (SELECT e.edited_at FROM record_edit e
               WHERE e.record_type = 'lab_result' AND e.record_id = lab_result.lab_result_id
                 AND e.dedup_base = lab_result.dedup_base
               ORDER BY e.edited_at DESC, e.record_edit_id DESC LIMIT 1),
  edited_by = (SELECT e.attributed_to FROM record_edit e
               WHERE e.record_type = 'lab_result' AND e.record_id = lab_result.lab_result_id
                 AND e.dedup_base = lab_result.dedup_base
               ORDER BY e.edited_at DESC, e.record_edit_id DESC LIMIT 1)
WHERE EXISTS (SELECT 1 FROM record_edit e
              WHERE e.record_type = 'lab_result' AND e.record_id = lab_result.lab_result_id
                AND e.dedup_base = lab_result.dedup_base);

UPDATE medication SET
  edited_at = (SELECT e.edited_at FROM record_edit e
               WHERE e.record_type = 'medication' AND e.record_id = medication.medication_id
                 AND e.dedup_base = medication.dedup_base
               ORDER BY e.edited_at DESC, e.record_edit_id DESC LIMIT 1),
  edited_by = (SELECT e.attributed_to FROM record_edit e
               WHERE e.record_type = 'medication' AND e.record_id = medication.medication_id
                 AND e.dedup_base = medication.dedup_base
               ORDER BY e.edited_at DESC, e.record_edit_id DESC LIMIT 1)
WHERE EXISTS (SELECT 1 FROM record_edit e
              WHERE e.record_type = 'medication' AND e.record_id = medication.medication_id
                AND e.dedup_base = medication.dedup_base);

UPDATE procedure SET
  edited_at = (SELECT e.edited_at FROM record_edit e
               WHERE e.record_type = 'procedure' AND e.record_id = procedure.procedure_id
                 AND e.dedup_base = procedure.dedup_base
               ORDER BY e.edited_at DESC, e.record_edit_id DESC LIMIT 1),
  edited_by = (SELECT e.attributed_to FROM record_edit e
               WHERE e.record_type = 'procedure' AND e.record_id = procedure.procedure_id
                 AND e.dedup_base = procedure.dedup_base
               ORDER BY e.edited_at DESC, e.record_edit_id DESC LIMIT 1)
WHERE EXISTS (SELECT 1 FROM record_edit e
              WHERE e.record_type = 'procedure' AND e.record_id = procedure.procedure_id
                AND e.dedup_base = procedure.dedup_base);

UPDATE appointment SET
  edited_at = (SELECT e.edited_at FROM record_edit e
               WHERE e.record_type = 'appointment' AND e.record_id = appointment.appointment_id
                 AND e.dedup_base = appointment.dedup_base
               ORDER BY e.edited_at DESC, e.record_edit_id DESC LIMIT 1),
  edited_by = (SELECT e.attributed_to FROM record_edit e
               WHERE e.record_type = 'appointment' AND e.record_id = appointment.appointment_id
                 AND e.dedup_base = appointment.dedup_base
               ORDER BY e.edited_at DESC, e.record_edit_id DESC LIMIT 1)
WHERE EXISTS (SELECT 1 FROM record_edit e
              WHERE e.record_type = 'appointment' AND e.record_id = appointment.appointment_id
                AND e.dedup_base = appointment.dedup_base);

UPDATE observation SET
  edited_at = (SELECT e.edited_at FROM record_edit e
               WHERE e.record_type = 'observation' AND e.record_id = observation.observation_id
                 AND e.dedup_base = observation.dedup_base
               ORDER BY e.edited_at DESC, e.record_edit_id DESC LIMIT 1),
  edited_by = (SELECT e.attributed_to FROM record_edit e
               WHERE e.record_type = 'observation' AND e.record_id = observation.observation_id
                 AND e.dedup_base = observation.dedup_base
               ORDER BY e.edited_at DESC, e.record_edit_id DESC LIMIT 1)
WHERE EXISTS (SELECT 1 FROM record_edit e
              WHERE e.record_type = 'observation' AND e.record_id = observation.observation_id
                AND e.dedup_base = observation.dedup_base);

UPDATE allergy SET
  edited_at = (SELECT e.edited_at FROM record_edit e
               WHERE e.record_type = 'allergy' AND e.record_id = allergy.allergy_id
                 AND e.dedup_base = allergy.dedup_base
               ORDER BY e.edited_at DESC, e.record_edit_id DESC LIMIT 1),
  edited_by = (SELECT e.attributed_to FROM record_edit e
               WHERE e.record_type = 'allergy' AND e.record_id = allergy.allergy_id
                 AND e.dedup_base = allergy.dedup_base
               ORDER BY e.edited_at DESC, e.record_edit_id DESC LIMIT 1)
WHERE EXISTS (SELECT 1 FROM record_edit e
              WHERE e.record_type = 'allergy' AND e.record_id = allergy.allergy_id
                AND e.dedup_base = allergy.dedup_base);

UPDATE condition SET
  edited_at = (SELECT e.edited_at FROM record_edit e
               WHERE e.record_type = 'condition' AND e.record_id = condition.condition_id
                 AND e.dedup_base = condition.dedup_base
               ORDER BY e.edited_at DESC, e.record_edit_id DESC LIMIT 1),
  edited_by = (SELECT e.attributed_to FROM record_edit e
               WHERE e.record_type = 'condition' AND e.record_id = condition.condition_id
                 AND e.dedup_base = condition.dedup_base
               ORDER BY e.edited_at DESC, e.record_edit_id DESC LIMIT 1)
WHERE EXISTS (SELECT 1 FROM record_edit e
              WHERE e.record_type = 'condition' AND e.record_id = condition.condition_id
                AND e.dedup_base = condition.dedup_base);
