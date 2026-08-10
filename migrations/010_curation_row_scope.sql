-- 010_curation_row_scope: a verdict may name one ROW, not only a family (issue #114).
--
-- 008 keyed every verdict by (record_type, dedup_base) - the whole dedup family. That is
-- right for the common case (one fact, however many occurrences of it were filed) and
-- wrong for the family that a `--keep both` conflict resolution left holding two *live*
-- rows: superseding the loser hid the winner too, because both share the base. Applying a
-- real curation ledger hit exactly that, and the verdict had to be withdrawn.
--
-- So scope becomes an explicit second dimension of the key:
--
--   record_id = 0  -> FAMILY scope: today's verdict, over every row sharing dedup_base.
--   record_id > 0  -> ROW scope: this one <record_type>_id, and no sibling of it.
--
-- A row-scoped verdict resolves by (record_type, record_id) *alone*. The dedup_base stored
-- alongside it is a breadcrumb - which family it ruled in, at annotate time - never a
-- resolution key: `pemr rekey` rewrites dedup_key/dedup_base but never renumbers a row id
-- (dedup.rekey: "Values, provenance and row ids are untouched"), so a row-scoped verdict
-- genuinely survives the dictionary-driven rekey that orphans a family-scoped one. The
-- breadcrumb may go stale after such a rekey; every reader re-reads the live base off the
-- row instead, and re-annotating deletes by (record_type, record_id) so the stale copy
-- collapses.
--
-- 0 rather than NULL for family scope, deliberately: SQLite treats NULLs as *distinct* in a
-- unique index, so a nullable scope column would silently permit duplicate family verdicts
-- on one base - the one thing 008's PK exists to forbid.
--
-- This is a table REBUILD, not ADD COLUMN: SQLite's ALTER TABLE ADD COLUMN can add neither
-- a PK member nor a CHECK, and 008's PK (record_type, dedup_base) forbids exactly the states
-- this issue needs - a family verdict and a row verdict coexisting on one family, and two
-- row verdicts inside one family. `curation` has no FK in or out and no dependent view or
-- trigger, so the rebuild is safe under foreign_keys=ON, and db.migrate wraps the whole
-- script in one BEGIN/COMMIT. Every 008-era verdict is family-scoped by definition, so the
-- backfill (record_id = 0) is lossless and nothing needs re-annotating.
--
-- 008's three CHECKs are carried verbatim; the orphan story is unchanged (no FK, `pemr
-- verify` warns rather than the schema erasing), and now covers "names no live row" too.

CREATE TABLE curation_new (
  record_type      TEXT NOT NULL,   -- one of dedup.KNOWN_TYPES; validated in Python
  dedup_base       TEXT NOT NULL,   -- family identity; a BREADCRUMB when record_id <> 0
  record_id        INTEGER NOT NULL DEFAULT 0,  -- 0 = family scope; else <record_type>_id
  status           TEXT NOT NULL,
  note             TEXT NOT NULL,   -- required: the why, and who said so
  merged_into_base TEXT,            -- set iff status = 'merged-into'; always a FAMILY
  attributed_to    TEXT,
  created_at       TEXT NOT NULL,   -- ISO8601 UTC, timespec=seconds (as document.ingested_at)
  PRIMARY KEY (record_type, dedup_base, record_id),
  CHECK (record_id >= 0),
  CHECK (status IN ('confirmed', 'superseded', 'erroneous-in-source', 'disputed',
                    'merged-into')),
  CHECK (trim(note) <> ''),
  CHECK ((merged_into_base IS NOT NULL) = (status = 'merged-into'))
);

INSERT INTO curation_new
  (record_type, dedup_base, record_id, status, note, merged_into_base, attributed_to,
   created_at)
SELECT record_type, dedup_base, 0, status, note, merged_into_base, attributed_to,
       created_at
FROM curation;

DROP TABLE curation;

ALTER TABLE curation_new RENAME TO curation;

-- The real uniqueness rule for row scope, which the PK cannot state (its dedup_base member
-- is a breadcrumb): one live verdict per row, whatever base it was recorded under. Also the
-- guard against a second copy of a row verdict appearing under a new base after a rekey.
CREATE UNIQUE INDEX idx_curation_row ON curation(record_type, record_id)
  WHERE record_id <> 0;
