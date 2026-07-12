-- 002_conflict: dedup layer-2 conflict staging (docs/Architecture.md §3).
-- A dedup_key collision whose non-key fields differ (e.g. a corrected value) lands
-- here instead of silently dropping the incoming row or overwriting the stored one.
-- `pemr review-conflicts` lists and resolves these.

CREATE TABLE conflict (
  conflict_id   INTEGER PRIMARY KEY,
  record_type   TEXT NOT NULL,           -- lab_result|medication|procedure|appointment|observation
  dedup_key     TEXT NOT NULL,           -- the colliding semantic key
  person_id     INTEGER REFERENCES person(person_id),
  document_id   INTEGER REFERENCES document(document_id),
  existing_json TEXT NOT NULL,           -- row currently stored under this dedup_key
  incoming_json TEXT NOT NULL,           -- proposed row that collided
  status        TEXT NOT NULL DEFAULT 'open',   -- open|resolved
  resolution    TEXT,                    -- keep-existing|keep-incoming (+ optional note)
  detected_at   TEXT NOT NULL,
  resolved_at   TEXT
);

CREATE INDEX idx_conflict_status ON conflict(status);
