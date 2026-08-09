-- 008_curation: overlay table for recorded human verdicts on record families (issue #109).
--
-- Some record states cannot be settled by deterministic tooling over documents: two
-- sources that contradict each other, a row that is real in the source but known to be
-- wrong, a diagnosis superseded by later clinical understanding, a restated fact the
-- dedup keyer must never collapse. The engine's only durable outcomes today are
-- keep/drop/keep-both, so a resolved human judgment has nowhere to live and the same
-- question reopens on every re-render or re-extraction.
--
-- This is a pure **overlay**: no record row is ever mutated, single-provenance is
-- untouched, and a family with no row here renders exactly as it does today. The
-- renderer resolves verdicts at read time, so re-rendering after a verdict changes the
-- output because the *database* changed - `render` stays a pure function of DB state.
--
-- Keyed by (record_type, dedup_base), NOT by row id and NOT by dedup_key. `dedup_base`
-- is the stable per-family identity across occurrence renumbering (Architecture.md §3),
-- so a verdict survives `pemr rekey`, a re-ingest of the same document, and the
-- occurrence shifts `record rm` (#107) produces. A row-id or dedup_key join would not.
--
-- No FK to the row it annotates (the 007 document_tombstone precedent): the verdict has
-- to outlive key churn and removals, including the removal of the family itself - an
-- orphaned verdict is reported by `pemr verify` as a warning, not erased by the schema.
-- No index beyond the PK: access is a PK lookup, a per-type scan, or a full list.
--
-- Unlike 007, this table carries CHECKs. 007 chose Python validation for better
-- messages, and Python still raises first for every operator-facing case here. The
-- CHECKs guard a different threat: this is the only table whose contents silently change
-- what a clinical document renders, so a hand-edited or corrupted status/coupling would
-- *misrender* records rather than error.

CREATE TABLE curation (
  record_type      TEXT NOT NULL,   -- one of dedup.KNOWN_TYPES; validated in Python
  dedup_base       TEXT NOT NULL,   -- family identity: NOT dedup_key, NOT a row id
  status           TEXT NOT NULL,
  note             TEXT NOT NULL,   -- required: the why, and who said so
  merged_into_base TEXT,            -- set iff status = 'merged-into'
  attributed_to    TEXT,
  created_at       TEXT NOT NULL,   -- ISO8601 UTC, timespec=seconds (as document.ingested_at)
  PRIMARY KEY (record_type, dedup_base),
  CHECK (status IN ('confirmed', 'superseded', 'erroneous-in-source', 'disputed',
                    'merged-into')),
  CHECK (trim(note) <> ''),
  CHECK ((merged_into_base IS NOT NULL) = (status = 'merged-into'))
);
