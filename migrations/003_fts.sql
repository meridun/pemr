-- 003_fts: full-text search over document OCR text + high-value record text fields
-- (docs/Architecture.md §5 `pemr find`). A single standalone FTS5 index, kept in sync
-- by AFTER INSERT/UPDATE/DELETE triggers on the base tables so ingest/commit paths need
-- no code changes. Metadata columns are UNINDEXED (stored, not tokenized) so a match can
-- report its source table/row + document provenance and be filtered by person.
--
-- Design decision (Architecture.md §Open questions): plain FTS5, no vector sidecar. A
-- semantic sidecar can bolt on later without touching this schema.

CREATE VIRTUAL TABLE record_fts USING fts5(
  source_table UNINDEXED,   -- 'document'|'lab_result'|'medication'|'procedure'|'appointment'|'observation'
  source_id    UNINDEXED,   -- primary key of the row in source_table
  person_id    UNINDEXED,   -- owner, for the `pemr find --person` filter
  document_id  UNINDEXED,   -- provenance (self for 'document' rows)
  text                      -- the indexed content
);

-- --- document.ocr_text ------------------------------------------------------
CREATE TRIGGER record_fts_document_ai AFTER INSERT ON document BEGIN
  INSERT INTO record_fts (source_table, source_id, person_id, document_id, text)
  VALUES ('document', NEW.document_id, NEW.person_id, NEW.document_id, COALESCE(NEW.ocr_text, ''));
END;
CREATE TRIGGER record_fts_document_ad AFTER DELETE ON document BEGIN
  DELETE FROM record_fts WHERE source_table='document' AND source_id=OLD.document_id;
END;
CREATE TRIGGER record_fts_document_au AFTER UPDATE ON document BEGIN
  DELETE FROM record_fts WHERE source_table='document' AND source_id=OLD.document_id;
  INSERT INTO record_fts (source_table, source_id, person_id, document_id, text)
  VALUES ('document', NEW.document_id, NEW.person_id, NEW.document_id, COALESCE(NEW.ocr_text, ''));
END;

-- --- lab_result.test_name ---------------------------------------------------
CREATE TRIGGER record_fts_lab_ai AFTER INSERT ON lab_result BEGIN
  INSERT INTO record_fts (source_table, source_id, person_id, document_id, text)
  VALUES ('lab_result', NEW.lab_result_id, NEW.person_id, NEW.document_id, COALESCE(NEW.test_name, ''));
END;
CREATE TRIGGER record_fts_lab_ad AFTER DELETE ON lab_result BEGIN
  DELETE FROM record_fts WHERE source_table='lab_result' AND source_id=OLD.lab_result_id;
END;
CREATE TRIGGER record_fts_lab_au AFTER UPDATE ON lab_result BEGIN
  DELETE FROM record_fts WHERE source_table='lab_result' AND source_id=OLD.lab_result_id;
  INSERT INTO record_fts (source_table, source_id, person_id, document_id, text)
  VALUES ('lab_result', NEW.lab_result_id, NEW.person_id, NEW.document_id, COALESCE(NEW.test_name, ''));
END;

-- --- medication.name --------------------------------------------------------
CREATE TRIGGER record_fts_med_ai AFTER INSERT ON medication BEGIN
  INSERT INTO record_fts (source_table, source_id, person_id, document_id, text)
  VALUES ('medication', NEW.medication_id, NEW.person_id, NEW.document_id, COALESCE(NEW.name, ''));
END;
CREATE TRIGGER record_fts_med_ad AFTER DELETE ON medication BEGIN
  DELETE FROM record_fts WHERE source_table='medication' AND source_id=OLD.medication_id;
END;
CREATE TRIGGER record_fts_med_au AFTER UPDATE ON medication BEGIN
  DELETE FROM record_fts WHERE source_table='medication' AND source_id=OLD.medication_id;
  INSERT INTO record_fts (source_table, source_id, person_id, document_id, text)
  VALUES ('medication', NEW.medication_id, NEW.person_id, NEW.document_id, COALESCE(NEW.name, ''));
END;

-- --- procedure.name ---------------------------------------------------------
CREATE TRIGGER record_fts_proc_ai AFTER INSERT ON procedure BEGIN
  INSERT INTO record_fts (source_table, source_id, person_id, document_id, text)
  VALUES ('procedure', NEW.procedure_id, NEW.person_id, NEW.document_id, COALESCE(NEW.name, ''));
END;
CREATE TRIGGER record_fts_proc_ad AFTER DELETE ON procedure BEGIN
  DELETE FROM record_fts WHERE source_table='procedure' AND source_id=OLD.procedure_id;
END;
CREATE TRIGGER record_fts_proc_au AFTER UPDATE ON procedure BEGIN
  DELETE FROM record_fts WHERE source_table='procedure' AND source_id=OLD.procedure_id;
  INSERT INTO record_fts (source_table, source_id, person_id, document_id, text)
  VALUES ('procedure', NEW.procedure_id, NEW.person_id, NEW.document_id, COALESCE(NEW.name, ''));
END;

-- --- appointment.reason + summary -------------------------------------------
CREATE TRIGGER record_fts_appt_ai AFTER INSERT ON appointment BEGIN
  INSERT INTO record_fts (source_table, source_id, person_id, document_id, text)
  VALUES ('appointment', NEW.appointment_id, NEW.person_id, NEW.document_id,
          TRIM(COALESCE(NEW.reason, '') || ' ' || COALESCE(NEW.summary, '')));
END;
CREATE TRIGGER record_fts_appt_ad AFTER DELETE ON appointment BEGIN
  DELETE FROM record_fts WHERE source_table='appointment' AND source_id=OLD.appointment_id;
END;
CREATE TRIGGER record_fts_appt_au AFTER UPDATE ON appointment BEGIN
  DELETE FROM record_fts WHERE source_table='appointment' AND source_id=OLD.appointment_id;
  INSERT INTO record_fts (source_table, source_id, person_id, document_id, text)
  VALUES ('appointment', NEW.appointment_id, NEW.person_id, NEW.document_id,
          TRIM(COALESCE(NEW.reason, '') || ' ' || COALESCE(NEW.summary, '')));
END;

-- --- observation.key + value_text -------------------------------------------
CREATE TRIGGER record_fts_obs_ai AFTER INSERT ON observation BEGIN
  INSERT INTO record_fts (source_table, source_id, person_id, document_id, text)
  VALUES ('observation', NEW.observation_id, NEW.person_id, NEW.document_id,
          TRIM(COALESCE(NEW.key, '') || ' ' || COALESCE(NEW.value_text, '')));
END;
CREATE TRIGGER record_fts_obs_ad AFTER DELETE ON observation BEGIN
  DELETE FROM record_fts WHERE source_table='observation' AND source_id=OLD.observation_id;
END;
CREATE TRIGGER record_fts_obs_au AFTER UPDATE ON observation BEGIN
  DELETE FROM record_fts WHERE source_table='observation' AND source_id=OLD.observation_id;
  INSERT INTO record_fts (source_table, source_id, person_id, document_id, text)
  VALUES ('observation', NEW.observation_id, NEW.person_id, NEW.document_id,
          TRIM(COALESCE(NEW.key, '') || ' ' || COALESCE(NEW.value_text, '')));
END;

-- --- backfill: index rows that predate this migration (no re-ingest needed) --
INSERT INTO record_fts (source_table, source_id, person_id, document_id, text)
  SELECT 'document', document_id, person_id, document_id, COALESCE(ocr_text, '') FROM document;
INSERT INTO record_fts (source_table, source_id, person_id, document_id, text)
  SELECT 'lab_result', lab_result_id, person_id, document_id, COALESCE(test_name, '') FROM lab_result;
INSERT INTO record_fts (source_table, source_id, person_id, document_id, text)
  SELECT 'medication', medication_id, person_id, document_id, COALESCE(name, '') FROM medication;
INSERT INTO record_fts (source_table, source_id, person_id, document_id, text)
  SELECT 'procedure', procedure_id, person_id, document_id, COALESCE(name, '') FROM procedure;
INSERT INTO record_fts (source_table, source_id, person_id, document_id, text)
  SELECT 'appointment', appointment_id, person_id, document_id,
         TRIM(COALESCE(reason, '') || ' ' || COALESCE(summary, '')) FROM appointment;
INSERT INTO record_fts (source_table, source_id, person_id, document_id, text)
  SELECT 'observation', observation_id, person_id, document_id,
         TRIM(COALESCE(key, '') || ' ' || COALESCE(value_text, '')) FROM observation;
