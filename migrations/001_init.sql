-- 001_init: core dimension, provenance, high-value typed tables, generic catch-all.
-- Schema per docs/Architecture.md §2 (hybrid).

CREATE TABLE person (
  person_id   INTEGER PRIMARY KEY,
  slug        TEXT UNIQUE NOT NULL,      -- 'jane-doe'
  full_name   TEXT NOT NULL,
  dob         TEXT,                      -- ISO date
  sex         TEXT,
  blood_type  TEXT,
  notes       TEXT
);

-- Provenance: every structured row traces to a source document.
CREATE TABLE document (
  document_id   INTEGER PRIMARY KEY,
  sha256        TEXT UNIQUE NOT NULL,    -- content hash -> dedup layer 1
  person_id     INTEGER REFERENCES person(person_id),
  doc_date      TEXT,                    -- date the doc pertains to
  category      TEXT,                    -- labs|imaging|visit-note|rx|vaccine|referral|billing
  provider      TEXT,
  source_path   TEXT NOT NULL,           -- sources/<hash>.<ext>
  ocr_text      TEXT,                    -- extracted full text (agent or tesseract)
  ingested_at   TEXT NOT NULL
);

CREATE TABLE lab_result (
  lab_result_id INTEGER PRIMARY KEY,
  person_id     INTEGER NOT NULL REFERENCES person(person_id),
  document_id   INTEGER REFERENCES document(document_id),
  test_name     TEXT NOT NULL,           -- normalized via analyte dictionary
  loinc         TEXT,                    -- optional standard code
  value_num     REAL,
  value_text    TEXT,
  unit          TEXT,
  ref_low       REAL,
  ref_high      REAL,
  flag          TEXT,                    -- H|L|Critical|normal
  collected_at  TEXT NOT NULL,
  dedup_key     TEXT NOT NULL,           -- semantic key -> dedup layer 2
  UNIQUE(dedup_key)
);

CREATE TABLE medication (
  medication_id INTEGER PRIMARY KEY,
  person_id     INTEGER NOT NULL REFERENCES person(person_id),
  document_id   INTEGER REFERENCES document(document_id),
  name          TEXT NOT NULL,
  dose          TEXT,
  route         TEXT,
  frequency     TEXT,
  started_on    TEXT,
  ended_on      TEXT,                    -- NULL = current
  prescriber    TEXT,
  status        TEXT,                    -- lifecycle only: active|completed|discontinued|NULL
                                         -- (AGENTS.md §MUST-8; prn/ordered are not lifecycle)
  dedup_key     TEXT NOT NULL,
  UNIQUE(dedup_key)
);

CREATE TABLE procedure (
  procedure_id  INTEGER PRIMARY KEY,
  person_id     INTEGER NOT NULL REFERENCES person(person_id),
  document_id   INTEGER REFERENCES document(document_id),
  name          TEXT NOT NULL,
  performed_on  TEXT,
  provider      TEXT,
  outcome       TEXT,
  dedup_key     TEXT NOT NULL,
  UNIQUE(dedup_key)
);

CREATE TABLE appointment (
  appointment_id INTEGER PRIMARY KEY,
  person_id      INTEGER NOT NULL REFERENCES person(person_id),
  document_id    INTEGER REFERENCES document(document_id),
  scheduled_for  TEXT,
  provider       TEXT,
  specialty      TEXT,
  reason         TEXT,
  summary        TEXT,                   -- post-visit narrative
  dedup_key      TEXT NOT NULL,
  UNIQUE(dedup_key)
);

-- Generic catch-all: new record types with zero migration. Promotion path: an
-- obs_type that grows important graduates into its own typed table via a migration.
CREATE TABLE observation (
  observation_id INTEGER PRIMARY KEY,
  person_id      INTEGER NOT NULL REFERENCES person(person_id),
  document_id    INTEGER REFERENCES document(document_id),
  obs_type       TEXT NOT NULL,          -- 'blood_pressure','weight','allergy','screening','immunization'...
  observed_at    TEXT,
  key            TEXT,
  value_num      REAL,
  value_text     TEXT,
  unit           TEXT,
  dedup_key      TEXT NOT NULL,
  UNIQUE(dedup_key)
);
