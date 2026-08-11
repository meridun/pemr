-- 011_curation_distinct_status: a verdict may rule two colliding rows DISTINCT (issue #122).
--
-- Since #116 a `pemr rekey` collision is settled by a `merged-into` or `superseded`
-- verdict - both of which say "these two rows are one fact". There was no way to record
-- the opposite ruling: "these are two different facts that happen to recompute to one
-- dedup_key because their extracted labels are generic placeholders (`diagnosis 2`,
-- `past_medical_history 3`)". Those labels are synonyms of nothing, so the standing
-- advice for a fused collision - fix the dictionary - has nothing to narrow, and the
-- table stayed quarantined (#92) indefinitely.
--
-- `distinct` is that missing ruling. It is a *resolving* status (it settles the identity
-- question a collision asks, so `rekey` may act on it) but deliberately NOT an appendix
-- status: neither row is superseded or merged, both stay live, and after the rekey they
-- sit in one family at two occurrences - the `--keep both` shape that already renders as
-- two independent siblings. Absence from `curation.APPENDIX_STATUSES` is what guarantees
-- that; see the disjointness assertion in tests/test_curation.py.
--
-- The verdict is **unary**: it names the row (or family) it rules on and no counterpart,
-- exactly as `superseded` has since #116. A pair-shaped column was rejected because the
-- counterpart is as often a row as a family, and every resolution is reported with both
-- row ids, the status and the scope, so the ruling's reach is never silent.
--
-- This is a table REBUILD rather than an ALTER: SQLite cannot alter a CHECK constraint,
-- and the widened status vocabulary lives in one. It follows 010's rebuild verbatim -
-- same columns, same PK, 010's four CHECKs with only the status list widened - and, as
-- there, `curation` has no FK in or out and no dependent view or trigger, so the rebuild
-- is safe under foreign_keys=ON and db.migrate wraps the whole script in one
-- BEGIN/COMMIT. The partial unique index is dropped with the old table and must be
-- re-created below; losing it would let a rekey leave two copies of one row's verdict.
--
-- Still no FK to the row (the 007/010 precedent): `pemr verify` warns on an orphaned
-- verdict rather than the schema erasing one.

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
                    'merged-into', 'distinct')),
  CHECK (trim(note) <> ''),
  CHECK ((merged_into_base IS NOT NULL) = (status = 'merged-into'))
);

INSERT INTO curation_new
  (record_type, dedup_base, record_id, status, note, merged_into_base, attributed_to,
   created_at)
SELECT record_type, dedup_base, record_id, status, note, merged_into_base, attributed_to,
       created_at
FROM curation;

DROP TABLE curation;

ALTER TABLE curation_new RENAME TO curation;

-- Re-created because the DROP took it with the old table (010's own reasoning): one live
-- verdict per row, whatever base it was recorded under, which the PK cannot state since
-- its dedup_base member is a breadcrumb.
CREATE UNIQUE INDEX idx_curation_row ON curation(record_type, record_id)
  WHERE record_id <> 0;
