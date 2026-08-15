-- 012_record_edit: an append-only ledger of in-place field corrections (issue #129).
--
-- Before this, a record row had three mutation paths and none of them could *correct* a
-- field: `record rm` deletes, `record annotate` rules over a row without touching it, and
-- `record assert` writes a new unsourced fact. So a mislabelled display field - the
-- reporting case was `lbs`/`F`/`breaths/min` beside `lb`/`degF`/`/min`, same scale, same
-- stored value - could only be "fixed" by re-committing the row through
-- `commit-extraction`, which re-attributes it to whichever document is passed. Correcting
-- a unit is not a reason to rewrite provenance, and that re-attribution is precisely the
-- invariant (Architecture §2: every row traces to a source document or a named human) a
-- fix must not break.
--
-- `record edit` writes the row in place and records every change here. The ledger is the
-- audit trail: what changed, from what to what, why, who said so, when. Nothing
-- **resolves through** it - render, verify, rekey and rekey's collision resolver never
-- read it - which is deliberate and is what makes append-only affordable (see below).
--
-- Which fields may be edited is not stated here. It is derived in Python as
-- `dedup.FIELD_SPECS[type] - dedup.KEY_FIELDS[type]`, so `document_id`, `person_id`, the
-- `dedup_*` columns and the `attested_*` columns - none of which are in FIELD_SPECS - are
-- unreachable by construction rather than by a denylist that rots.
--
-- No FK to the edited row (the 007 document_tombstone / 010 curation precedent). A record
-- id is a plain rowid alias (no AUTOINCREMENT, migrations 001/006), so a ledger entry can
-- outlive its row and later name the id's next occupant. Curation resolves that hazard by
-- retiring row-scoped verdicts with the row (010); the opposite answer is right here.
-- Retiring an entry would destroy the audit trail this table exists to create, and the
-- hazard is weaker: since nothing resolves through the ledger, a stale entry mis-renders
-- nothing - it is a forensics ambiguity, not a live-row fault. So entries survive their
-- row, `record rm` *discloses* the entries naming the row it is about to delete, and each
-- entry keeps a `dedup_base` breadcrumb (never a lookup key - the 010 reading) so a reader
-- can tell whether a later occupant is even the same family.
--
-- One row per changed field, all sharing one `edited_at`: that is what makes a single
-- command's correction reconstructable as one act while each field stays individually
-- readable. `old_value`/`new_value` are TEXT even for numeric columns - the ledger is a
-- human-readable audit record, not a replayable patch. NULL means the column was (or
-- became) NULL, which is distinct from the empty string.
--
-- Purely additive: CREATE TABLE + CREATE INDEX, no rebuild, safe under foreign_keys=ON.
-- Readers guard with `records.has_edit_table` (the `curation.has_table` precedent) so a
-- restored pre-012 snapshot reports no edits rather than raising `no such table`.

CREATE TABLE record_edit (
  record_edit_id INTEGER PRIMARY KEY,
  record_type    TEXT NOT NULL,    -- one of dedup.KNOWN_TYPES; validated in Python
  record_id      INTEGER NOT NULL, -- <record_type>_id at edit time
  dedup_base     TEXT NOT NULL,    -- BREADCRUMB (the 010 reading), never a lookup key
  field          TEXT NOT NULL,    -- a non-key FIELD_SPECS column
  old_value      TEXT,             -- rendered TEXT; NULL = the column was NULL
  new_value      TEXT,
  note           TEXT NOT NULL,    -- required: why the correction was made
  attributed_to  TEXT,
  edited_at      TEXT NOT NULL,    -- ISO8601 UTC seconds; shared across one command
  -- The 008/010 `curation` precedent for the identical rule: a correction with no stated
  -- reason is not an audit trail. Python refuses it first; this is the backstop.
  CHECK (trim(note) <> '')
);

CREATE INDEX record_edit_row ON record_edit (record_type, record_id);
