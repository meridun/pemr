-- 006_condition_allergy: promote `condition` and `allergy` out of `observation` into
-- typed tables (issue #63).
--
-- Both cleared the Architecture.md §2 promotion bar on *fields with no legal home* in the
-- generic shape, not on volume:
--
--   * a resolved problem needs TWO dates (onset + resolved); `observation` has exactly one
--     (`observed_at`), and AGENTS.md §MUST-7 forbids stashing the other in `value_text` —
--     so a resolved condition was simply unrepresentable;
--   * `observation.dedup_key = hash(person_id | obs_type | observed_at | key)` gave an
--     ACTIVE problem and a FAMILY-HISTORY entry naming the same disease ("diabetes" in the
--     patient, "diabetes" in the mother) the same key: they silently deduped into one row
--     and the summary then presented a relative's diagnosis as the patient's own. The
--     `status`/`relation` discriminator is what fixes that, and it has to be in the key;
--   * an allergy's `criticality` had nowhere to go but free text, where nothing can flag
--     or sort on it.
--
-- `immunization` and `screening` are deliberately NOT promoted — the #43 `obs_type`
-- convention holds them losslessly and their shared consumer is the future `pemr due`.
--
-- Table/PK names match the record-type names (`condition`/`condition_id`,
-- `allergy`/`allergy_id`) because `dedup._insert_record`, `_overwrite_record` and
-- `documents.reassign` interpolate `record_type` as the table name and
-- `f"{record_type}_id"` as the primary key. Neither name is an SQLite keyword.
--
-- Dedup keys (see pemr/dedup.py `_key_parts`) are deliberately DATE-FREE:
--
--   allergy.dedup_key   = hash(person_id | norm(substance))
--   condition.dedup_key = hash(person_id | norm(name) | subject)
--       subject = 'family:' + norm(relation)  when status = 'family-history'
--               = 'self'                      otherwise
--
-- Allergies and problem lists are *standing facts* restated on every document with
-- inconsistent or absent dates; a date in the key would fork one allergy into one row per
-- document. Dates are payload, and a disagreement in them stages a conflict.

CREATE TABLE allergy (
  allergy_id       INTEGER PRIMARY KEY,
  person_id        INTEGER NOT NULL REFERENCES person(person_id),
  document_id      INTEGER REFERENCES document(document_id),
  substance        TEXT NOT NULL,
  reaction         TEXT,
  criticality      TEXT,                    -- high|low|unable-to-assess
  noted_on         TEXT,                    -- ISO date
  dedup_key        TEXT NOT NULL,
  dedup_base       TEXT,
  dedup_occurrence INTEGER NOT NULL DEFAULT 0,
  UNIQUE(dedup_key)
);
CREATE INDEX idx_allergy_dedup_base ON allergy(dedup_base);

CREATE TABLE condition (
  condition_id     INTEGER PRIMARY KEY,
  person_id        INTEGER NOT NULL REFERENCES person(person_id),
  document_id      INTEGER REFERENCES document(document_id),
  name             TEXT NOT NULL,
  status           TEXT NOT NULL,           -- active|resolved|history|family-history
  onset_on         TEXT,                    -- ISO date
  resolved_on      TEXT,                    -- ISO date
  relation         TEXT,                    -- family-history only: mother|father|sibling...
  note             TEXT,                    -- free-text detail (was observation.value_text)
  dedup_key        TEXT NOT NULL,
  dedup_base       TEXT,
  dedup_occurrence INTEGER NOT NULL DEFAULT 0,
  UNIQUE(dedup_key)
);
CREATE INDEX idx_condition_dedup_base ON condition(dedup_base);

-- --- FTS triggers (mirror 003_fts.sql) --------------------------------------
-- Created BEFORE the row move below, so the index maintains itself on the INSERT ...
-- SELECT and no explicit backfill is needed.

CREATE TRIGGER record_fts_allergy_ai AFTER INSERT ON allergy BEGIN
  INSERT INTO record_fts (source_table, source_id, person_id, document_id, text)
  VALUES ('allergy', NEW.allergy_id, NEW.person_id, NEW.document_id,
          TRIM(COALESCE(NEW.substance, '') || ' ' || COALESCE(NEW.reaction, '')));
END;
CREATE TRIGGER record_fts_allergy_ad AFTER DELETE ON allergy BEGIN
  DELETE FROM record_fts WHERE source_table='allergy' AND source_id=OLD.allergy_id;
END;
CREATE TRIGGER record_fts_allergy_au AFTER UPDATE ON allergy BEGIN
  DELETE FROM record_fts WHERE source_table='allergy' AND source_id=OLD.allergy_id;
  INSERT INTO record_fts (source_table, source_id, person_id, document_id, text)
  VALUES ('allergy', NEW.allergy_id, NEW.person_id, NEW.document_id,
          TRIM(COALESCE(NEW.substance, '') || ' ' || COALESCE(NEW.reaction, '')));
END;

CREATE TRIGGER record_fts_condition_ai AFTER INSERT ON condition BEGIN
  INSERT INTO record_fts (source_table, source_id, person_id, document_id, text)
  VALUES ('condition', NEW.condition_id, NEW.person_id, NEW.document_id,
          TRIM(COALESCE(NEW.name, '') || ' ' || COALESCE(NEW.note, '')));
END;
CREATE TRIGGER record_fts_condition_ad AFTER DELETE ON condition BEGIN
  DELETE FROM record_fts WHERE source_table='condition' AND source_id=OLD.condition_id;
END;
CREATE TRIGGER record_fts_condition_au AFTER UPDATE ON condition BEGIN
  DELETE FROM record_fts WHERE source_table='condition' AND source_id=OLD.condition_id;
  INSERT INTO record_fts (source_table, source_id, person_id, document_id, text)
  VALUES ('condition', NEW.condition_id, NEW.person_id, NEW.document_id,
          TRIM(COALESCE(NEW.name, '') || ' ' || COALESCE(NEW.note, '')));
END;

-- --- move the existing observation rows --------------------------------------
-- `status='active'` for every migrated condition is the honest reading: the pre-existing
-- convention had no status field, and those rows were exactly what `render_summary`
-- printed under its (single) Conditions section.
--
-- THE dedup_key CAVEAT. The new keys are sha256 over dictionary-normalized parts, which
-- SQL cannot compute, so the old `observation` key/base/occurrence are carried forward
-- verbatim (guaranteed unique, so UNIQUE(dedup_key) holds and nothing is lost) and the
-- correct key is re-derived by `pemr rekey --apply`. Until that runs, a re-commit of an
-- already-stored allergy/condition inserts a second row instead of deduping —
-- `pemr migrate` prints a one-line reminder when this migration is among those applied.
--
-- One known rough edge: an OPEN conflict staged against one of these rows still carries
-- `record_type='observation'` and a key no observation row holds any more. It still
-- lists, and `keep existing` still closes it; `keep incoming` refuses with the friendly
-- "no stored row left to overwrite" message. Re-extract from the source document if the
-- staged value matters.

INSERT INTO allergy
  (person_id, document_id, substance, reaction, noted_on,
   dedup_key, dedup_base, dedup_occurrence)
SELECT person_id, document_id, key, value_text, observed_at,
       dedup_key, COALESCE(dedup_base, dedup_key), dedup_occurrence
  FROM observation WHERE obs_type = 'allergy' AND key IS NOT NULL;

INSERT INTO condition
  (person_id, document_id, name, status, onset_on, note,
   dedup_key, dedup_base, dedup_occurrence)
SELECT person_id, document_id, key, 'active', observed_at, value_text,
       dedup_key, COALESCE(dedup_base, dedup_key), dedup_occurrence
  FROM observation WHERE obs_type = 'condition' AND key IS NOT NULL;

-- `key IS NULL` rows could not be moved (substance/name are NOT NULL) and are left in
-- `observation` rather than dropped: they carry no identifiable allergen/problem, so a
-- human has to re-extract them from the source document.
DELETE FROM observation
 WHERE obs_type IN ('allergy', 'condition') AND key IS NOT NULL;
