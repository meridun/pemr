-- 007_document_tombstone: removal memory for layer-1 dedup (issue #80).
--
-- Layer-1 identity is a lookup against the *live* `document` table, so `document rm`
-- erases all memory that a hash was ever seen and the next bulk-intake sweep over the
-- same source folder re-ingests it as brand new. This table is the opt-in record of
-- "a human decided against this content", checked by `ingest` alongside the `document`
-- lookup. Opt-in on purpose: most removals are corrections (wrong owner, bad scan,
-- superseded version) and must stay re-ingestable, so suppressing every removed hash
-- would turn an ordinary correction into a permanent, silent block.
--
-- No CHECK on the hash shape (no migration here uses one) - validation lives in
-- pemr/tombstones.py, which produces a better message than a constraint violation.
-- No FK on document_id: the row it names is gone by construction. No index beyond the
-- PK: the table is small and every access is a PK lookup or a full list.

CREATE TABLE document_tombstone (
  sha256      TEXT PRIMARY KEY NOT NULL,  -- content hash; the value `document.sha256` holds
  removed_at  TEXT NOT NULL,              -- ISO8601 UTC, timespec=seconds (as document.ingested_at)
  reason      TEXT,                       -- free-text slug, e.g. 'identifiers'; no taxonomy
  note        TEXT,                       -- free text: the only human-recognisable label
  document_id INTEGER                     -- the id it had, forensics only; deliberately not a FK
);
