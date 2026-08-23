# Personal EMR — Design Doc (v0.1)

A local-first, family-scale medical record system. Source documents are retained
as-is; a **SQLite database is the source of truth** for structured data. Deterministic
work (ingest, dedup, query, analysis, brief-generation) lives in a **Python CLI
engine** wrapped by a **thin MCP server** so AI agents call typed tools instead of
re-inventing logic each request.

## Decisions locked

| # | Decision |
|---|---|
| Store | SQLite is truth; source scans retained on disk, referenced by the DB |
| Schema | Hybrid — typed tables for high-value record types + generic `observations` catch-all |
| Multi-person | Single DB, `person_id` on every row |
| Docs | Journal / master summary / appointment briefs are **generated views**, DB is truth |
| Ingestion | Agent does the vision/extraction step; tools validate + dedup + commit |
| Interface | CLI engine + MCP wrapper (CLI is the tested core) |
| Language | Python |
| Dedup | Two layers — content-hash on documents + semantic keys on extracted rows |
| Backup | `VACUUM INTO` timestamped snapshot → cloud-synced folder; DB itself stays local |

## Non-goals

- No HIPAA/PHI compliance layer, no HL7/FHIR message exchange or provider-system
  integration. Reading a clinical export *file* is not interoperability: document-level
  **text extraction** from formats like CCDA/CCD is in scope (issue #138) — one more
  document format feeding `find` and the owner check.
- No always-on server, no multi-writer concurrency (family scale, one machine).
- No clinical decision-making — the system assists prep/research, doesn't advise.

---

## 1. Repository layout

```
pemr/
  pemr.db                      # SQLite — SOURCE OF TRUTH (local only, gitignored, not synced live)
  pemr/                        # Python package (the engine)
    __init__.py
    cli.py                     # argparse/click entry: `pemr <cmd>`
    db.py                      # connection, migrations runner, pragmas (WAL, foreign_keys)
    models.py                  # dataclasses / typed row shapes
    ingest.py                  # document intake, hashing, staging
    study.py                   # study directories -> one canonical-zip blob (§4)
    dedup.py                   # content-hash + semantic-key matching
    query.py                   # canned + ad-hoc read queries
    render.py                  # generated docs (summary, journal, briefs)
    backup.py                  # VACUUM INTO snapshot + rotation
    mcp_server.py              # thin MCP wrapper over cli/query/ingest
  migrations/                  # dbmate-style or plain numbered .sql files
    001_init.sql
    002_...
  sources/                     # retained original documents (content-addressed)
    <sha256[:2]>/<sha256>.pdf  # dedup-friendly, immutable blob store
    .tmp/                      # staging for study archives (§4); exclude from cloud sync
  inbox/                       # drop zone for new un-ingested scans
  exports/                     # generated docs (disposable): summaries, briefs, journal,
                               #   curation records
  backups/                     # local VACUUM INTO snapshots before they sync
  config.toml                  # paths, cloud backup dir, people roster
  AGENTS.md                    # conventions + tool contract for any agent
```

**Cloud sync**: only `backups/` (and optionally `sources/`, `exports/`) live in the
Drive/OneDrive folder. The live `pemr.db` + `-wal`/`-shm` stay on a local-only path to
avoid the sync corruption you've already hit. `backup.py` writes a consistent snapshot
into the synced folder.

---

## 2. Schema (hybrid)

Core dimension:

```sql
CREATE TABLE person (
  person_id   INTEGER PRIMARY KEY,
  slug        TEXT UNIQUE NOT NULL,      -- 'jane-doe'
  full_name   TEXT NOT NULL,
  dob         TEXT,                       -- ISO date
  sex         TEXT,
  blood_type  TEXT,
  notes       TEXT
);
```

Provenance — every structured row traces to a source document **or to a named human
attestation** (issue #110; the three row states are spelled out below the typed tables):

```sql
CREATE TABLE document (
  document_id   INTEGER PRIMARY KEY,
  sha256        TEXT UNIQUE NOT NULL,     -- content hash → dedup layer 1
  person_id     INTEGER REFERENCES person(person_id),
  doc_date      TEXT,                     -- date the doc pertains to
  category      TEXT,                     -- labs|imaging|visit-note|rx|vaccine|referral|billing
  provider      TEXT,
  source_path   TEXT NOT NULL,            -- sources/<hash>.<ext>
  ocr_text      TEXT,                     -- extracted full text (agent or tesseract)
  text_source   TEXT,                     -- 'engine'|'attached'; NULL = none/pre-015 (#175)
  ingested_at   TEXT NOT NULL
);

-- Opt-in memory of an intentional removal (issue #80). Layer-1 identity is scoped to
-- the live `document` table above, so without this a removed document silently
-- re-ingests on the next sweep. Checked by `ingest`; written by
-- `document rm --tombstone` and `document tombstone add`.
CREATE TABLE document_tombstone (
  sha256      TEXT PRIMARY KEY NOT NULL,  -- content hash; the value document.sha256 holds
  removed_at  TEXT NOT NULL,              -- ISO8601 UTC
  reason      TEXT,                       -- free-text slug, e.g. 'identifiers'; no taxonomy
  note        TEXT,                       -- free text: the only human-recognisable label
  document_id INTEGER                     -- the id it had; forensics only, not a FK
);

-- Recorded human verdicts over records (issues #109, #114). A pure overlay: no record
-- row is ever mutated, and a record with no row here renders exactly as before. A
-- verdict is scoped either to a whole dedup FAMILY (record_id = 0, keyed by the stable
-- family identity of §3) or to a single ROW (record_id > 0). Family scope outlives
-- occurrence renumbering and `record rm` but NOT a dictionary-driven `rekey` that
-- renames the family: that moves `dedup_base` and orphans the verdict. Row scope
-- resolves by (record_type, record_id) alone and therefore DOES survive that rekey -
-- `rekey` never renumbers a row id - so its stored dedup_base is a breadcrumb only,
-- never a resolution key. Row scope exists for the `--keep both` family holding two
-- live rows, where a family verdict would hide the occurrence it says should win. Row
-- wins over family for its own row. Like 007 the table carries no FK to what it
-- annotates, and `pemr verify` warns (never fails) on a verdict whose family - or row -
-- is gone. Written only by `record annotate` and `record reaffirm` (the bulk remedy for
-- that orphaning, issue #126); read at render time by every generated document.
CREATE TABLE curation (
  record_type      TEXT NOT NULL,   -- one of dedup.KNOWN_TYPES; validated in Python
  dedup_base       TEXT NOT NULL,   -- family identity; a breadcrumb when record_id <> 0
  record_id        INTEGER NOT NULL DEFAULT 0,  -- 0 = family scope; else <record_type>_id
  status           TEXT NOT NULL,   -- confirmed|superseded|erroneous-in-source|disputed|merged-into|distinct
  note             TEXT NOT NULL,   -- required: the why, and who said so
  merged_into_base TEXT,            -- set iff status = 'merged-into'; always a FAMILY, and of the
                                    -- SAME person unless --allow-cross-person (issue #161)
  attributed_to    TEXT,
  created_at       TEXT NOT NULL,   -- ISO8601 UTC
  PRIMARY KEY (record_type, dedup_base, record_id)
);
-- One live verdict per row, whatever base it was recorded under (the PK cannot say this:
-- its dedup_base member is a breadcrumb). 0 rather than NULL for family scope because
-- SQLite treats NULLs as distinct in a unique index.
CREATE UNIQUE INDEX idx_curation_row ON curation(record_type, record_id)
  WHERE record_id <> 0;

-- Append-only ledger of in-place field corrections (issue #129). Written only by
-- `record edit`, which corrects a row's NON-KEY fields - a mislabelled unit, say - and
-- leaves document_id, person_id, dedup_key and the source blob untouched. The editable
-- set is DERIVED (dedup.FIELD_SPECS minus dedup.KEY_FIELDS, §3), so identity and
-- provenance are unreachable from an edit by construction rather than by a denylist.
-- `--identity` (issue #152, §3 Episodes) opens the identity half of that split and moves
-- the row's key with the payload; provenance stays unreachable in both modes, and the
-- ledger entry keeps the OLD dedup_base so the move stays reconstructable.
-- One row per changed field, all sharing one edited_at, so a single command's correction
-- reads as one act while each field stays individually legible.
--
-- Nothing RESOLVES THROUGH this table - render, verify, rekey and rekey's collision
-- resolver never read it - which is what makes append-only affordable. Entries therefore
-- outlive their row, the opposite of 010's answer to the same row-id-reuse hazard:
-- retiring one would destroy the audit trail the table exists to create, while a stale
-- entry mis-renders nothing. `record rm` discloses (never deletes) the entries naming the
-- row it removes, and the dedup_base breadcrumb tells a reader whether a later occupant
-- of that id is even the same family. The row-level DISCLOSURE that a correction happened
-- is a separate thing, stamped on the row itself (edited_at/edited_by, migration 016) -
-- see "A corrected row says so" below.
CREATE TABLE record_edit (
  record_edit_id INTEGER PRIMARY KEY,
  record_type    TEXT NOT NULL,    -- one of dedup.KNOWN_TYPES; validated in Python
  record_id      INTEGER NOT NULL, -- <record_type>_id at edit time; no FK (the 007/010 precedent)
  dedup_base     TEXT NOT NULL,    -- breadcrumb, never a lookup key
  field          TEXT NOT NULL,    -- a non-key FIELD_SPECS column
  old_value      TEXT,             -- rendered TEXT; NULL = the column was NULL
  new_value      TEXT,
  note           TEXT NOT NULL,    -- required: why the correction was made
  attributed_to  TEXT,
  edited_at      TEXT NOT NULL     -- ISO8601 UTC; shared across one command
);
CREATE INDEX record_edit_row ON record_edit (record_type, record_id);
```

A `merged_into_base` names a family **of the same person**. A `dedup_base` folds
`person_id` in (§3), so two people's same-named facts never collide — which is exactly why
an accidental cross-person merge is silent: the target family genuinely exists, so `verify`
reports no orphan, while the fact leaves the annotated person's chart and never appears on
the target person's. `record annotate` therefore refuses a cross-person `--merged-into`,
and `record reaffirm` refuses the same shape at plan time (issue #161).
`--allow-cross-person` is the explicit escape hatch for the rare deliberate case: never the
default, and disclosed in the report (`cross_person`) rather than recorded silently. That
guard is forward-only, so `pemr verify` also **flags** a stored `merged_into_base` that
resolves to another person's live family (issue #169) — the shape a verdict written before
#161 can still carry. `cross_person` is not persisted on the row, so a deliberate
`--allow-cross-person` merge shows up in that warning too; the message says as much, and
warning on both beats staying silent on the accidental one.

A correction is **not** a re-attribution. Before `record edit`, a wrong display field
could only be repaired by re-submitting the row through `commit-extraction` (or deleting
and re-committing it), both of which re-file the fact under whichever document is passed
at correction time — so a 2022 measurement would end up sourced to a 2026 chart export.
An edit keeps the row's provenance exactly as it was; what changes is that the row now
*differs* from what that source literally said, and the ledger is the record of that
divergence. The visible consequence: since `unit` is one of the compared payload fields
(§4), re-ingesting the original document after a unit correction stages a **conflict**
rather than deduping. That is the honest outcome, not a bug.

**A corrected row says so** (migration 016, issue #134). Keeping the provenance intact
creates a second obligation: the row is still filed under a document that never stated the
corrected value, so on the page it would read as a verbatim quotation of that document.
`record edit` therefore stamps two columns on the row in the same transaction as the
UPDATE and its ledger rows — `edited_at` (the newest correction's ISO8601 UTC stamp) and
`edited_by` (its `--attributed-to`, nullable) — and every renderer discloses them:
`(corrected by <who> <date>)`, or `(corrected <date>)` when unattributed, appended at every
line builder that already carries `_attest_suffix`, and carried on a `query timeline`
event. This is #110's failure mode in mirror image, so it gets #110's shape: columns on the
row, one predicate, one suffix builder, `dedup.public_row` stripping the pair while it is
NULL so an uncorrected row's `--json`/MCP payload is unchanged.

Columns rather than a read-time join on `record_edit`, which would need no migration and is
unsound: ledger entries deliberately outlive their row and a record id is a reusable rowid
alias, so a stale entry would caveat an unrelated later occupant (the #114 hazard). The
mark on the row closes that by construction. The duplication is deliberate and one-way —
the ledger is the field-level audit trail (`old -> new`, every correction), the columns are
only the row-level "corrected, by whom, when" marker; last correction wins on the row, and
`record edit --list` is where a human reconciles the two. The one-time backfill applies the
same breadcrumb guard: a ledger entry whose `dedup_base` no longer matches the row's is
skipped, so the upgrade can leave a rekeyed family unmarked but can never mark the wrong
fact.

The mark is not a lever: `edited_at`/`edited_by` are outside `dedup.FIELD_SPECS`, so they
are unnameable by `record edit`, invisible to `dedup_key`/`rekey`, and an edit cannot forge
its own disclosure. **Identity-field refinement stays out of scope** — moving one row's
identity in place (a `condition.name` rename) is a rekey, not a correction: it recomputes
`dedup_key`/`dedup_base`, can collide with a live family and re-anchors staged conflicts.
`pemr rekey` owns that operation with full collision resolution when the two names are
synonyms; `record rm` + re-commit owns the case where the stored fact is simply wrong.

The **display-unit overlay** (migration 013, issue #136) — one canonical *display* unit
per person per measurement key:

```sql
-- Display-only lever. Nothing resolves through it: dedup, rekey, commit-extraction, the
-- conflict resolver, verify's identity checks and the render curation overlay never read
-- it. `unit` is a non-key field for lab_result and observation, which is what makes a
-- display lever possible without touching identity at all. Only `render summary` and
-- `query.trends` honour it, and each discloses every conversion on the line it changes.
-- ON DELETE CASCADE, and deliberately absent from `persons._CHILD_TABLES`: a display
-- preference is not medical history and must never block a childless `person remove`.
CREATE TABLE person_unit_pref (
  person_id INTEGER NOT NULL REFERENCES person(person_id) ON DELETE CASCADE,
  key       TEXT NOT NULL,   -- dedup.key_token of the measurement key / analyte
  unit      TEXT NOT NULL,   -- a pemr.units canonical unit id; validated in Python
  set_at    TEXT NOT NULL,   -- ISO8601 UTC seconds
  PRIMARY KEY (person_id, key)
);
```

Canonicalising a unit is **display-time and per-person, and storage never mutates**. The
obvious alternative — normalise at `commit-extraction` and migrate the corpus once — was
rejected: a genuine unit-of-measure difference is not a spelling mistake, so rewriting a
`kg` row into `lb` would mutate a document-sourced fact, and no person's internally
consistent unit system is more correct than another's. The unit's measurement system is
therefore **derived** from the stored unit string through a static registry in
`pemr/units.py` rather than stored in a new column — which is what lets rows committed
long before this shipped convert correctly. That registry is Python, not a
`data/dictionary.toml` section, because the two differ in kind: the dictionary is
user-grown medical *vocabulary* and an identity lever (it feeds `dedup_key`), a unit table
is fixed physics and a display lever. Correcting a genuinely mislabelled unit *in place*
remains `record edit`'s job (above) — a different verb for a different problem.

High-value typed tables (each carries `document_id` provenance + a `dedup_key`; migration
005 added `dedup_base`/`dedup_occurrence` to every one of them — see the occurrence model
in §3, omitted from the DDL below to keep the shapes readable):

```sql
CREATE TABLE lab_result (
  lab_result_id INTEGER PRIMARY KEY,
  person_id     INTEGER NOT NULL REFERENCES person(person_id),
  document_id   INTEGER REFERENCES document(document_id),
  test_name     TEXT NOT NULL,            -- normalized via analyte dictionary
  loinc         TEXT,                     -- optional standard code
  value_num     REAL,
  value_text    TEXT,
  unit          TEXT,
  ref_low       REAL,
  ref_high      REAL,
  flag          TEXT,                     -- H|L|Critical|normal
  collected_at  TEXT NOT NULL,
  dedup_key     TEXT NOT NULL,            -- semantic key → dedup layer 2
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
  ended_on      TEXT,                     -- NULL = current
  prescriber    TEXT,
  status        TEXT,                     -- lifecycle only: active|completed|discontinued|NULL
                                          -- (AGENTS.md §MUST-9; prn/ordered are not lifecycle);
                                          -- a discontinue reason belongs in status_reason
  status_reason TEXT,                     -- verbatim discontinue reason; NULL = none stated
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
  summary        TEXT,                    -- post-visit narrative
  dedup_key      TEXT NOT NULL,
  UNIQUE(dedup_key)
);
CREATE TABLE allergy (                     -- migration 006 (promoted from observation)
  allergy_id    INTEGER PRIMARY KEY,
  person_id     INTEGER NOT NULL REFERENCES person(person_id),
  document_id   INTEGER REFERENCES document(document_id),
  substance     TEXT NOT NULL,
  reaction      TEXT,
  criticality   TEXT,                      -- high|low|unable-to-assess
  noted_on      TEXT,
  dedup_key     TEXT NOT NULL,
  UNIQUE(dedup_key)
);

CREATE TABLE condition (                   -- migration 006 (promoted from observation)
  condition_id  INTEGER PRIMARY KEY,
  person_id     INTEGER NOT NULL REFERENCES person(person_id),
  document_id   INTEGER REFERENCES document(document_id),
  name          TEXT NOT NULL,
  status        TEXT NOT NULL,             -- active|resolved|history|family-history
  onset_on      TEXT,                      -- IDENTITY (§3 Episodes): which episode this
                                           -- row is. Stored at the source's own precision
                                           -- (YYYY | YYYY-MM | YYYY-MM-DD), never padded;
                                           -- correct it with `record edit --identity`
  resolved_on   TEXT,                      -- payload: an episode's end is not its identity
  relation      TEXT,                      -- family-history only: mother|father|...
  note          TEXT,
  dedup_key     TEXT NOT NULL,
  UNIQUE(dedup_key)
);
```

Generic catch-all (new record types with zero migration):

```sql
CREATE TABLE observation (
  observation_id INTEGER PRIMARY KEY,
  person_id      INTEGER NOT NULL REFERENCES person(person_id),
  document_id    INTEGER REFERENCES document(document_id),
  obs_type       TEXT NOT NULL,           -- 'vital' (key = canonical vital token, e.g.
                                           -- 'blood_pressure'/'weight'), 'order',
                                           -- 'screening', 'immunization', 'functional',
                                           -- 'symptom'/'activity' (self-reported, #167)
                                           -- ('functional' requires observed_at;
                                           -- symptom/activity require a `key` and an
                                           -- observed_at carrying a time of day;
                                           -- condition/allergy graduated in 006)
  observed_at    TEXT,
  key            TEXT,
  value_num      REAL,
  value_text     TEXT,
  unit           TEXT,
  dedup_key      TEXT NOT NULL,
  UNIQUE(dedup_key)
);
```

**Attestation columns** (migration 009, issue #110). Every one of the seven typed tables
above additionally carries `attested_by` / `attested_on` / `attested_at`, all nullable and
all omitted from the DDL for readability. They are the second legal provenance: `document_id`
was already nullable at the DB level, and this is what makes a NULL there *meaningful*
rather than a bug. Written only by `record assert` (CLI-only — never an MCP tool), never
accepted from an extraction JSON, and deliberately absent from `dedup.FIELD_SPECS`, so keys
are bit-identical for attested and document-sourced rows alike. Unlike `curation` this is
**not** an overlay: an attestation is the row's own provenance and dies with the row, so it
lives in columns rather than a sibling table. Three states, all derived
(`dedup.attestation_state`), none stored twice:

| state | predicate | renders as |
|---|---|---|
| document-sourced | `attested_by IS NULL` | an ordinary fact |
| live attestation | `attested_by IS NOT NULL AND document_id IS NULL` | tagged `(attested by <who> <date>; no source document)` in **every** section |
| superseded | `attested_by IS NOT NULL AND document_id IS NOT NULL` | an ordinary fact; the attestation is retained as history |

Supersession is therefore just the attested row acquiring a `document_id` — nothing is
deleted and the attestation columns are never cleared. See §3 for when that happens.

Promotion path: an `obs_type` that grows important graduates from `observation`
into its own typed table via a migration. The generic table absorbs the long tail so
you're never blocked waiting on schema work. `condition` and `allergy` are the worked
example (migration 006, issue #63): both graduated on *fields with no legal home* in the
generic shape — a resolved problem needs two dates where `observation` has one, and an
allergy's `criticality` had nowhere to live but free text — not on row volume. Volume
alone is not a reason to promote.

---

## 3. Dedup algorithm (the core determinism win)

**Layer 1 — document identity (content hash).**
On ingest, `sha256` the raw file bytes. If the hash already exists in `document`,
it's a re-scan of a document already filed → skip (or attach as an alternate path).
Catches "I scanned the same lab report twice."

That identity is scoped to the **live** `document` table, so `document rm` erases every
trace that a hash was ever seen and the next sweep over the same source folder re-ingests
the file as new. `document_tombstone` (migration 007, issue #80) is the opt-in memory of an
*intentional* removal: `document rm --tombstone` records the hash in the same transaction
as the delete, and `ingest` checks the table right after the `document` lookup, returning
status `tombstoned` (rc=0, nothing written) instead of re-filing. Opt-in on purpose — most
removals are corrections (wrong owner, bad scan, superseded version) and must stay
re-ingestable, so tombstoning every removed hash would turn an ordinary correction into a
permanent silent block. `ingest --force` overrides one run without lifting the row; the MCP
`ingest` tool cannot (a tombstone is the human's recorded decision, not a heuristic).

**Layer 2 — record identity (semantic key).**
Each typed/observation row computes a deterministic `dedup_key` from normalized fields,
so the *same clinical fact* extracted from two different documents collapses to one row.

```
lab_result.dedup_key   = hash(person_id | key_token(test_name) | date_only(collected_at))
medication.dedup_key    = hash(person_id | norm(name) | dose | started_on)
procedure.dedup_key     = hash(person_id | norm(name) | performed_on)
appointment.dedup_key   = hash(person_id | provider | scheduled_for)
observation.dedup_key   = hash(person_id | obs_type | observed_at | key_token(key))
allergy.dedup_key       = hash(person_id | norm(substance))
condition.dedup_key     = hash(person_id | norm(name) | episode)
    episode = subject + '@' + date_only(onset_on)  when an onset is stated
            = subject                              otherwise
    subject = 'family:' + norm(relation)  when status = 'family-history'
            = 'self'                      otherwise
```

`allergy` is deliberately **date-free**: an allergy is a *standing fact* restated on every
document with inconsistent or absent dates, so a date in the key would fork one allergen
into one row per document. Its dates are payload, and a stated disagreement (in a date or
anywhere else) stages a conflict. A field the incoming document simply omits is silence,
not a change, and never conflicts — the one place the duplicate-vs-conflict comparison
differs by record type (`dedup._SPARSE_TYPES`). `condition` keeps that sparse reading but
left the date-free class in issue #152 (**Episodes**, below): a problem that starts and
stops repeatedly is not one standing fact, so its onset joins the key while its
`resolved_on` stays payload. The `subject` discriminator is a correctness fix, not a
nicety: without it a patient's diabetes and her mother's derive one key and silently merge.

That reading is asymmetric by design. Treating a *stored* NULL as silence too would make the
common terse-then-detailed document sequence lossy: the second document's `criticality` would
count as a duplicate and be dropped, with no conflict to catch it. So a field the incoming row
states over a stored NULL is a **gain** — no competing value, nothing to adjudicate — and fills
the stored row in place, reported as `enriched` (a fourth commit bucket beside
new/duplicate/conflict). A stated value never overwrites a stored one on that path, so
enrichment cannot launder a disagreement into a silent overwrite. The same reading governs
`keep incoming` on these types: it writes the fields the incoming row states and leaves the
rest as stored, so a one-field adjudication doesn't erase the row's other payload.

**On the dated types that reading is deliberately unavailable at commit time, and deliberately
available at review time** (issue #140). A cleared field on a lab or a medication IS news, so
enrichment must not fire automatically there — otherwise a later document could blank-then-refill
a column with nobody looking. But that left no field-level path at all: a portal export that
refines a medication's `status` and sig while carrying no prescriber could only be resolved by
dropping the refinement (`keep existing`), erasing the prescriber (`keep incoming`), or forking one
prescription into two rows (`keep both`). `review-conflicts --resolve --keep merge` is the missing
middle, and it is safe precisely because it is **operator-driven**: a human is already looking at
both rows. Per payload column it takes what the incoming row states over a stored NULL, keeps what
the incoming row is silent about, and leaves agreeing values alone (through the same normalization
as the duplicate-vs-conflict comparison, so a `MG/DL`/`mg/dL` variant is agreement, not a
disagreement).

Where both rows state *different* values, merge **refuses the whole resolution** — nothing written,
the conflict still open, the colliding field names reported. That is the only genuinely lossy
decision in the shape, so it stays explicit rather than being smeared across every field:
`--field NAME=existing|incoming` settles one collision and leaves every other column on the
automatic rule. A field choice naming a column that does not collide is refused too, so a typo or a
stale retry can't quietly leave the real collision unsettled. On the standing-fact types merge
always refuses, and needs no type gate to: a conflict there is present-and-different by
construction, because the NULL-vs-stated case was already absorbed at commit time as a gain.
Provenance is unchanged from `keep incoming` — the row takes the winning document's `document_id`,
and with it the attestation supersession above. The resolution text, the CLI success line and the
MCP payload record which fields came from which side by **name only**, never value.

**Document provenance outranks an attestation** (issue #110). An attested row keys exactly
like a document-sourced one, so a later `commit-extraction` of the same fact lands in the
same identity family and the ordinary duplicate/conflict split decides the outcome — no new
comparison path:

* **payloads agree** — the document confirms what the family attested. This is the
  *duplicate* branch, where by construction there is nothing to adjudicate, so the row is
  **promoted in place**: it takes the `document_id`, keeps its attestation columns as
  history, and the commit reports it under a fifth bucket, `promoted`. Same reading as
  enrichment above — a stored NULL under a stated incoming value is a gain, not a
  disagreement; here the NULL is `document_id`.
* **payloads differ** — unchanged: a conflict is staged for a human. There is no
  justification for auto-resolving a disagreement, only for recording an agreement.
  `keep incoming` then promotes the row (it already writes `document_id`), `keep existing`
  leaves the attestation live, and `keep both` admits the document row as the next
  occurrence beside it. Since issue #190, `keep existing --adopt-source` is the fourth
  reading: the stored payload wins in full **and** the row adopts the incoming document's
  `document_id`, which is how an attested row gains its source without taking the values
  the human ruled against. It **fills** a missing source, never re-points an existing one
  — an already-sourced row is refused, because keeping document A's payload under
  document B's id would misattribute it (the same provenance guarantee `record edit`
  enforces, one layer down); `keep incoming` / `keep merge` are the honest paths there,
  since they take payload and provenance together.

The mirror case — `record assert` onto a family the record already holds — is **refused,
not staged**: an equal payload reports "already recorded" and writes nothing, a differing
one raises and names the stored row. Same reason `commit_extraction`'s pass 1 refuses two
colliding rows of one submission: the human is at the keyboard, and a conflict staged
against oneself has no independent provenance to adjudicate.

Since issue #133, that refusal first consults the `curation` overlay (§2): a colliding row
already carrying a **releasing** verdict — `superseded` / `erroneous-in-source` /
`merged-into` (row scope beats family scope, same precedence rule as §6) — no longer blocks.
Only a `disputed` or unverdicted row still raises, and the error's remedy text only
recommends `record annotate` when annotating that row could actually change the outcome —
a row already released is never named. Once every colliding row is released the attestation
proceeds as an ordinary new occurrence of the identity (the next free `dedup_occurrence`),
not a replacement of what is stored. One consequence worth knowing: a **family**-scoped
release covers that new occurrence too, so the freshly attested row itself leaves the
clinical documents for the curation record (§6) until the verdict is re-scoped to the rows
it meant or lifted — the CLI prints a note when this happens so it is not a silent surprise.

`norm()` = lowercase, trim, collapse whitespace, drop parenthetical qualifiers, map
synonyms via an **analyte/name dictionary** (`data/dictionary.toml`) — e.g. `A1c`,
`HbA1c`, `Hemoglobin A1c` → one canonical `hba1c`. The dictionary is the one place fuzzy
naming gets pinned down deterministically; agents propose additions, you approve.

`key_token()` is the finer **identity** form used by the two analyte-named key parts: the
canonical token plus a *meaningful* parenthetical qualifier, `albumin (spep)`. A
parenthetical is meaningful unless the dictionary says otherwise (issue #71) — the
asymmetry is deliberate, since collapsing two assays is lossy and near-silent while
over-splitting is visible and repaired by one dictionary line plus `pemr rekey`. Two
escapes keep the common cases quiet: a parenthetical that maps to the same canonical
token as its stem is dropped with no entry at all (`Hemoglobin (HGB)` → `hemoglobin`),
and anything else can be declared noise with a full parenthesized key
(`"m-spike (spep)" = "m_spike"`). The qualifier is *folded into* the existing key part
rather than appended as a new one, so unqualified rows keep byte-identical keys and a
rekey moves only the rows the split affects.

The `norm()`/`key_token()` split is visible in the read layer too: `pemr labs --test
albumin` matches on `norm()` and lists the whole analyte family, while `pemr trends`
matches on `key_token()` so a numeric series never interleaves two assays — and reports
the rows it excluded on that basis (`other_assays`) instead of dropping them silently.

`trends` charts a measurement **key**, not a table (issue #176): it reads `lab_result`
rows *and* `obs_type='vital'` `observation` rows through one aliased row shape
(`observed_at` standing in for `collected_at`, `key` for `test_name`), so weight,
temperature and blood pressure reach the same statistics and the same per-person
canonical-unit conversion an analyte does — the four vitals dimensions the units registry
carries exist for exactly this population. A `--test` token matching numeric rows in
*both* tables is **refused**, not merged: the same reasoning as the assay split, since a
silently interleaved lab-and-vital series is a wrong chart even when the tokens coincide.
`pemr labs` remains lab-only.

**A dictionary edit is retroactive only if you make it so.** Stored keys are frozen at
commit time, so a new synonym changes the key a *future* commit derives for a fact already
in the DB: layer-2 dedup misses it and the same fact lands twice. `pemr rekey` re-derives
every stored key under the current dictionary (dry-run by default, `--apply` to write,
values and provenance untouched). Two rows that recompute to one key are a **collision**,
and the message names which of the two causes it is: *different* payloads mean the new
synonym fuses two distinct facts (e.g. a CMP `Albumin` and an SPEP `Protein
Electrophoresis Albumin Fraction` off one draw) and the fix belongs in the dictionary;
*identical* payloads mean one fact was filed twice, once
under a pre-drift key, and the fix belongs in the data.

There is a third case the dictionary cannot fix (issue #122). When the colliding labels
are generic *extraction placeholders* — `diagnosis`, `diagnosis 2`,
`past_medical_history 3` — they are synonyms of nothing, so there is no dictionary entry
to narrow, and nothing in the data to drop either: both rows are real and different. That
one is settled by a `distinct` verdict (below), not by an edit anywhere.

**A collision quarantines its own table, not the run** (issue #92). The record tables are
scanned independently, so a fused pair in `allergy` says nothing about `condition`. The
scan therefore never stops at the first collision: it reports every collision in every
table — a dry run writes nothing by definition, so it has to be usable as a full survey —
and `--apply` writes the tables that came out clean, leaves the colliding ones on their
stored keys, and names them (`skipped: collision` per table, plus a `skipped` list in
`--json`). `--json`'s `changed` list is every key the scan computed as moving, not only
the ones written — a row in a skipped table still shows up there, so a consumer must
cross-reference `skipped` to tell written from withheld. Exit code is 1 whenever any
**blocking** collision was found, in both output modes: partial progress is fine, silent
partial progress is not.

**A collision a human already ruled on is settled, not blocking** (issues #116, #122).
Many collisions are exactly the pair the curation overlay exists to answer. So `rekey`
consults the overlay — through an injected resolver, because `curation` imports `dedup`
and not the reverse — and a clash covered by a **resolving** verdict on **either** row at
**either** scope stops being blocking. The clash row is filed as the next free
*occurrence* of the shared family (the `--keep both` shape; leaving it on its stored key
would strand it permanently drifted while `rekey` itself reported clean).

Resolving verdicts come in two kinds, and they answer the identity question opposite ways:

- **one fact** — `merged-into` / `superseded`: the pair is a single fact filed twice;
- **two facts** — `distinct` (issue #122): the rows are genuinely different, and only
  their generic extracted labels recompute onto one key.

Both take the *same* write, because occurrence numbering is already the representation of
"two live rows on one identity". What differs is rendering, and it differs by omission:
`distinct` is deliberately **not** one of the appendix statuses, so neither row leaves its
clinical section — they render as two live siblings, exactly like a `--keep both` pair.
That absence is the whole guarantee, and `tests/test_curation.py` asserts the two tuples
stay disjoint.

Because the two kinds answer the *same* question opposite ways, a pair carrying a resolving
verdict on **each** row can be **contradictory** — one row ruled `distinct`, the other
`merged-into`/`superseded` (issue #124). Two opposite rulings settle nothing, so such a pair
is **not** resolved: it stays a blocking collision, quarantines its own table like any other,
and its `--json` entry carries a `contradiction` list naming both rulings (row id, status,
scope, `verdict_base`, `settlement`); the text message says which row was ruled which way.
No occurrence is assigned and **no narrowing is written** — pinning one arbitrarily chosen
side would convert a family verdict to row scope to authorize a write that never happened.
The fix is the operator's: re-rule or clear one of the two verdicts and re-run. Rulings that
merely *agree* — or a pair carrying only one — resolve exactly as they always have.

`confirmed`, `disputed` and `erroneous-in-source` resolve nothing — they rule on a row's
content, not on its identity against another row — and a table holding any *unresolved*
collision still withholds every change in it, resolved pairs included (its resolutions say
exactly that, rather than describing a write that did not happen). All four cases are
distinguishable in output: a resolved pair is a `note:` line naming which way it was
settled plus an entry in `--json`'s `resolved` list carrying `settlement`
(`"merged"` | `"distinct"`), a blocked one stays `error:` plus `skipped`, and a
contradictory one is an `error:` naming both rulings plus a non-empty `contradiction` on
its `collisions` entry. A run whose every collision was verdict-resolved therefore exits
**0**.

A resolving verdict is **unary** — it names one row or one family, never a counterpart —
so like `superseded` since #116 it settles any collision its target takes part in,
including one that would otherwise have blocked. Every resolution is reported with both
row ids, the status and the scope, so the reach of a ruling is stated rather than assumed.

**A verdict's scope never widens across that merge.** Resolution joins a judged family to
an unjudged one, so the surviving family is *larger* than the one the human ruled on.
Carrying the family verdict over wholesale would silently extend the ruling to a row nobody
judged — and for the appendix statuses that resolve collisions, that pulls a live row out
of its clinical section (a `merged-into` on one spelling of an allergy taking the other,
canonical spelling out of `## Allergies`). So the authorizing family verdict is **narrowed
to row scope** instead, pinned to exactly the rows whose stored `dedup_base` was its own,
in the same transaction as the keys: the ruling keeps precisely the extension it had when
it was made, the merged-in row keeps rendering where it was, and the verdict that *resolved*
the collision is never orphaned. That guarantee is scoped to the resolving verdict only: a
clash covered by *agreeing* verdicts on **both** colliding families still resolves through
exactly one of them (`covering_verdicts()` reports both — row scope over family scope per
row — and resolution takes the first), and the
*other* family's verdict is not carried anywhere. Its rows move out from under it the same
way an unrelated dictionary-driven rekey has always been able to orphan a family verdict
(documented since migration 008). Since issue #126 `rekey --apply` **reports that fallout
itself** (below) rather than leaving `pemr verify` as the only signal. The
direction is over-reporting, not data loss: no fact disappears, but a row a human had ruled
out of its clinical section can be promoted back into it until the operator re-rules or
clears the stale verdict. Criticality-blind by construction — no live row silently leaves
its rendered section, whatever it records. The conversion is announced, never silent:
`resolved` names the pinned rows in both output modes, and `pemr verify` notices a row
verdict whose family breadcrumb a rekey left stale, pointing at `pemr record annotate --row`
to re-affirm or re-rule. A recorded human ruling is only ever re-pointed or scope-narrowed
within its original extension here — never overwritten, deleted, or auto-cleared; a row that
already carries its own row-scoped verdict is skipped for the same reason.

That re-affirm has to be *runnable*, which is why `--merged-into` may name the annotated
row's own family at **row** scope. A `merged-into` narrowing is the common shape, and the
same rekey that narrows the ruling also moves its row into the merge target — so by the
time the operator answers the notice there is no third family left to name. At row scope
the pointer still means something (this occurrence is absorbed into the family it sits in;
it moves to the appendix while its siblings keep rendering live), so it is accepted. At
**family** scope a self-merge would leave the fact rendering nowhere at all, and stays
refused.

**The orphans a rekey does cause are reported where they happen, and fixable in bulk**
(issue #126). Two additions, both leaving the warn-and-reannotate design above exactly as
it is — `rekey` still never re-points a verdict by itself:

- `rekey --apply` computes, after its write, the verdicts *this run* orphaned — scoped to
  the families it actually moved, so a pre-existing orphan is not blamed on it — and prints
  them plus the follow-up command (`orphans` in `--json`, always present and `[]` on a dry
  run, which wrote nothing to have fallout from). It is a `warning:`, not an `error:`: the
  exit code stays `1 if collisions else 0`.
- `pemr record reaffirm` is the batch remedy beside the per-row `record annotate --clear`.
  The 57-row orphan batch one dictionary edit produced is what forced it. Dry run by
  default, `--json` for an agent, one explicit `--apply`, no prompting.

Both read **one** orphan detector, `curation.orphan_kinds` — which `pemr verify`'s two
orphan warnings now read too. That sharing is the point: a bulk remedy that disagreed with
the warning it answers would re-annotate the wrong rows. It owns exactly two classes:
`no-live-family` (a family-scoped verdict whose `dedup_base` names no live family) and
`dangling-merge-target` (either scope, `merged_into_base` names no live family). The
row-scoped **stale breadcrumb** is deliberately not one of them — that verdict still
resolves by row id, so nothing may re-point it — nor is the removed-row case, whose remedy
is `--clear --row` and which the removal write paths already retire. Nor is the
cross-person merge target (#169): its family is *live*, just the wrong person's, and
neither `record reaffirm` nor `rekey --apply` has a remedy for that — making it a kind
would have them offer to re-point a verdict only a human re-ruling can fix.

The two halves compose **by file**, and have to: a `dedup_base` is a content hash
overwritten in place, so once the run is over nothing in the database records that `F_old`
became `F_new`. Only `rekey` knows, and the mapping is not persisted (no new table, no
migration) — it travels on `RekeyReport.base_maps` and out through the report:

```
pemr rekey --apply --json > rekey.json      # applies, and reports its own orphans
pemr record reaffirm --map-file rekey.json  # dry run: what would be re-pointed, where
pemr record reaffirm --map-file rekey.json --apply
```

Every re-point is an explicit map entry plus `--apply`; the dry run shows the successor
family's **label and size**, so a ruling about to widen over rows nobody judged is visible
before approval; a successor that already carries its own verdict is **skipped**, never
overwritten; and an entry matching no live orphan is reported `already handled` rather than
failing, so re-running the same file is a clean no-op. Writes reuse `annotate_record` /
`clear_curation` per row — annotate **before** clear, so a failure between the two leaves
the ruling duplicated (recoverable) rather than destroyed. `record reaffirm` is CLI-only,
absent from `WRITE_TOOLS` for the same reason `record annotate` is.

**Ingesting against drifted keys is refused, not silently forked.** Until the rekey is
applied, a stored row's frozen key is invisible to layer-2 dedup, so re-filing that same
fact would land a *second* row reported as `new` — no duplicate, no conflict, no signal at
all — and then wedge `rekey` on the collision it had just created. `commit-extraction`
therefore raises `DictionaryDriftError` when a stored row recomputes onto an identity the
submission also derives while carrying a different stored key; the message names the `pemr
rekey --apply` to run. The check is deliberately narrow — unrelated drift elsewhere in the
database is a maintenance chore, not a reason to refuse an ingest.

`rekey` is also the whole **migration for the issue-#71 key change**, and it is safe by
construction: a qualifier can only *add* precision, so two rows can never fuse and a
collision is unreachable from that change alone. Procedure: `pemr rekey`
(dry-run) → read the moved labels → for any that should have stayed collapsed, add a full
parenthesized synonym key → re-run the dry-run → `pemr rekey --apply`. Rows whose names
carry no parenthetical do not move at all (the qualifier is folded into the existing key
part), so a database with no qualified names reports zero changes. Both other write paths
refuse until the rekey is applied — `document reassign` and `commit-extraction` recompute
keys through the same function, so they see the drift first. Rekeying does **not** recover
a value already lost to a pre-fix collision: that needs the source document re-extracted.

`rekey` is also a **required upgrade step after migration 006**, and unlike the #71 case it
is not collision-free. 006 promoted allergy/condition rows out of `observation`, but the new
keys are a sha256 over dictionary-normalized fields — which SQL cannot compute — so the
backfill carried each row's *old*, per-document key forward verbatim. Those rows sit on keys
layer-2 dedup will never derive again, so skipping the rekey **blocks the next ingest rather
than forking it**: a `commit-extraction` that re-states one of those facts hits the drift
guard above, raises `DictionaryDriftError` naming `pemr rekey --apply`, and writes nothing
(issue #94). Signposting and enforcement are two different mechanisms here — `pemr migrate`
*signposts*, printing the follow-up when 006 actually moves rows (a fresh `--create` has
nothing to rekey and prints nothing), while `commit-extraction` *enforces*, but only for the
identity being re-filed: unrelated ingests still pass, so a database can run on stale keys
indefinitely until one of them is restated. Run the `pemr rekey` dry-run and then `pemr rekey
--apply` against any **pre-006** database before its next allergy/condition
`commit-extraction`. Expect collisions here where the #71 migration had none: the old
`observation` keys carried the assertion's `observed_at`, so one condition restated by three
documents is three stored rows that all recompute onto the single date-free key 006 gave the
type — the legacy shape behind #86's apparently duplicated condition bullets, correct storage
under the old identity and a three-way fusion under the new one. A collision quarantines its own
table only (above), so the clean tables are still written and the fusion is settled in the
dictionary or the data before a re-run — or, where a `merged-into`/`superseded` verdict has
already been recorded on the pair, it is settled by that verdict and the table writes
(above, issue #116). That is the common case for this migration's collisions: the rows are
the *same* standing fact restated by several documents, which is precisely what those two
statuses record.

`rekey` is likewise a **required upgrade step for the `lab_result` date-only key** (issue
#117). Every stored lab whose `collected_at` carries a time recomputes to a new key the
moment that change lands, so run the `pemr rekey` dry-run and then `pemr rekey --apply`
before the next `commit-extraction` that restates one of those draws. As with 006 nothing is
silent — the drift guard raises `DictionaryDriftError` naming `pemr rekey --apply` and writes
nothing — and this note is the signpost, not the enforcement. Read the dry-run's collisions
with the key change in mind, because `rekey`'s vocabulary describes the shape, not the cause:

- a `lab_result` **`"doubled"`** collision (same payload, two keys) is exactly the
  mixed-precision duplicate pair this change fixes — one draw stored twice. Resolve it with
  `pemr record rm` on the redundant row, per `rekey`'s own guidance.
- a `lab_result` **`"fused"`** collision (different payloads onto one key) most likely means
  two *genuine* same-day draws already stored as separate rows, **not** a dictionary fault —
  the message's dictionary diagnosis is wrong here. Re-admit the second through
  `review-conflicts --resolve --keep both` rather than editing the dictionary.

Either way the collision quarantines only `lab_result`; the other tables are rekeyed.

The **measured value is deliberately *not* in the key** — temporal identity carries the
draw instead. The two temporal types differ in *how much* of that timestamp is identity.

`observation.observed_at` is used at **full precision** (timestamp when the document gives
one, date when it only gives a date), not truncated to the date. Two readings of the same
measurement on the same day at different times get distinct timestamps → distinct keys →
two rows preserved; a correction or OCR re-read of the *same* reading carries the same
timestamp → collides → surfaces as a conflict (below). When only a date is available,
same-day differing values collide → conflict; that safety bias is intentional (a spurious
conflict on a genuine repeat is human-recoverable, a silent duplicate of a correction
poisons `trends`/brief/`query` irrecoverably). The self-reported lanes
(`obs_type='symptom'`/`'activity'`, issue #167) therefore *mandate* the time component in
`validate_row`, so a same-day repeat is a second row rather than a conflict — a
fluctuating complaint is reported several times a day by design.

`lab_result.collected_at` is **truncated to the date** for key purposes (issue #117); the
column itself still stores the most precise prefix the source gave, and the read layer
(`trends`, `query labs`, render) uses that full value. Full precision in the key forked one
draw into two rows whenever two documents stated it at different precision — `2026-04-01`
in a summary, `2026-04-01T09:15` in the lab report — which is the common real-world case
and the one layer-2 dedup exists to collapse. The cost is that a genuine second draw on the
same day (a GTT timepoint) now collides on the date-only key and **stages a conflict**
rather than landing as a second clean row; it is re-admitted with `review-conflicts
--resolve --keep both`. That trade is this issue's recorded `decision:` — same-day distinct
draws are rare, mixed-precision restatements of one draw are not, and the conflict path
makes the rare case recoverable while the common case is now correct by construction.
`collected_at` is deliberately **not** in `_COMPARE_FIELDS["lab_result"]`: comparing it
would turn every mixed-precision pair into a conflict, recreating the same bug as noise.
The first document to state a draw therefore fixes its stored precision — a later
restatement with a time reports `duplicate` and does not upgrade the column. The same
exclusion means a same-day repeat draw with an *identical* value (e.g. a QC re-run
confirming the prior result) compares equal on every `_COMPARE_FIELDS` entry and so
collapses to one row reported as `duplicate` too — only a *differing* value on a same-day
repeat takes the conflict path above.

**Conflict handling.** On dedup-key collision with *differing* non-key fields (e.g. a
corrected value), don't silently drop — write to a `conflict` staging table and surface
it: `pemr review-conflicts`. Human/agent resolves. This is the safety valve that keeps
"no duplicates" from quietly meaning "lost the corrected result." Because the value is out
of the key, this fires for *any* magnitude of value change on a matching draw — the
headline `Glucose 92 → 95` (or `92 → 130`) case that previously slipped through as a silent
new row now stages a conflict.

**Occurrence model (`--keep both`).** The date-only safety bias above is only recoverable
if a resolution can say "both of these are real." `review-conflicts --resolve --keep both`
admits the incoming row *alongside* the stored one, so every identity is a **family** of
one or more occurrences rather than a single row:

```
dedup_base       = hash(person_id | identity fields…)   -- shared by the family
dedup_occurrence = 0 for the first row, 1, 2, … for admitted repeats
dedup_key        = dedup_base                    when occurrence = 0
                 = hash(dedup_base | occurrence) when occurrence > 0
```

Occurrence 0 reproduces the pre-005 key byte-for-byte, so the migration was a pure column
copy — no rekey, no re-commit. The disambiguator lives in a **column**, not in a
resolution-time suffix, because `pemr rekey` re-derives every key from payload columns: a
suffix it could not see would recompute to the base, collide with its sibling, and abort
the rekey. Commit-time matching is therefore family-aware — an incoming row that is
payload-equal to *any* sibling is a duplicate, and only a genuinely different value on
that identity stages a new conflict (against occurrence 0). That is what stops a third
commit of an already-admitted draw from forking again. `dedup_base` is denormalized on
purpose: the family is one indexed lookup instead of probing `hash(base|1)`, `hash(base|2)`
… which breaks on holes when a sibling is removed.

**A conflict's key is the family base, not a row's key.** `conflict.dedup_key` always
holds the `dedup_base`, and the conflict is anchored to the family's lowest surviving
occurrence. Resolutions therefore address that row by **primary key** — never by
`WHERE dedup_key = conflict.dedup_key`. Once occurrence 0 is gone (`document rm`,
`document reassign` of the document that owned it, or `record rm` of that one row) the
anchor's own key is
`hash(base | n)`, so a key-targeted write would match nothing while the conflict was
stamped `resolved`, silently discarding the staged value. If nothing is left in the family
at all, `keep incoming` **refuses** rather than reporting a success that wrote nothing;
`keep both` still admits the staged row, at occurrence 0.

The three removers treat an anchored open conflict differently, and the difference is
whether the conflict is still resolvable afterwards. `document rm` **deletes** it: the
document its staged payload came from is going away too, so there is nothing left to keep.
`record rm` (issue #107) **refuses** when the row it would delete is the last of the
conflict's family, because there the document survives — `keep both` is still a live
resolution, and no dry run could undo destroying an `incoming_json`. When a sibling
survives, `record rm` allows the removal and reports which conflicts re-anchor.

The stored `conflict.dedup_key` is also only the *current* base while the dictionary holds
still: `rekey` rewrites keys on the record tables and does not touch the `conflict` table,
so a dictionary edit made while a conflict is open strands it on a base no row carries.
Resolutions therefore **re-derive** the base from the conflict's own payload under the
current dictionary (`review-conflicts --dictionary`, mirroring `rekey`) — but the *staged*
key still wins whenever it has rows, and the re-derived base is used only when it does not.
The staged key names the family the conflict was actually staged against; the re-derivation
exists only for the case where `rekey` has already moved that family off it. Preferring the
derived base would misfire on a dictionary edit that **fuses two identities**: the derived
base then holds a different, pre-existing family, and the resolution would overwrite (or
join) an unrelated record while the conflict's own row went untouched — and `rekey` flags
the fused pair as a collision and quarantines that table, leaving it on stored keys rather
than writing the overwrite. Conversely, numbering an admitted row on a stale base that no
row carries would give it a key not derivable from its own columns, which collides the
family on the next `rekey`: a collision blocks only the table that holds it, not the whole
run (issue #92), but that is still enough to block `--apply` and `document reassign` on
that table until the collision is fixed.

**Intra-payload collisions are rejected, not staged.** Two rows in *one* submission that
derive the same key and disagree fail validation (pass 1) and roll the batch back, naming
the identity and the recovery path. A conflict whose "existing" side was inserted
milliseconds earlier in the same batch has no independent provenance to adjudicate
against; for `observation` the overwhelmingly likely cause is a time the source did give and
the extraction dropped, and the rejection message says so. For `lab_result` it is not —
the key holds only the collection date (above), so adding times cannot separate the rows and
the message omits that advice: two same-day draws go through two submissions plus
`--keep both`. Two *identical* rows in one payload stay benign (first inserts, second
reports `duplicate`) — that is an agent listing one fact twice. Genuine same-day
repeats go through two submissions plus `--keep both`, which keeps the human sign-off in
the loop rather than letting an agent self-admit near-duplicates.

Because siblings legitimately share a date, every same-date ordering is tie-broken by row
id (`query labs`, the summary/brief lab sections): the admitted row sorts as the later
point, so `trends` deltas and "latest value" stay deterministic.

### Episodes (issue #152)

Some clinical states **start and stop repeatedly**: kidney stones, UTIs, fractures, DVTs,
cellulitis, seizures; a course of antibiotics; an intermittent symptom. A row identified as
a standing fact cannot hold two of them — the second episode collides with the first, there
is nowhere for its date to live, and every recurrence folds into one row with the count and
every date but one silently lost. This is the **episode primitive**, defined once here and
adopted per record type, so each adopting type extends it rather than inventing a variant.
`condition` adopts it first; `symptom` and medication courses-as-episodes are separate
issues and take *this* shape when they land.

**Identity is name + subject + onset.** The onset is folded into the existing subject key
part (`self@2009-09-15`, `family:mother@2011`), not appended as a fourth part — the
`key_token()` decision applied again, and the reason the migration is cheap: an **undated**
row's `dedup_key` stays byte-identical, so only *dated* rows move and every family-scoped
verdict on an undated family survives untouched. `dedup_occurrence` is **not** replaced by
this; it is demoted to what it is now needed for — genuinely undated repeats and two
episodes a source dates identically.

**Onset stores true precision, FHIR-style.** `1998`, `1998-05`, `1998-05-20` are three
different claims, stored exactly as the source stated them: no sentinel padding to
`-01-01`, and no separate "approximate" flag. Precision is implicit in the value, and
ordering is plain lexical comparison because each accepted form is a prefix of the next —
the same three-precision rule `_is_iso_date` already enforces on every `DATE_FIELDS`
column, so this needed no storage change (the columns are `TEXT`, and no consumer does date
arithmetic on `onset_on`). Dates are *expected* for an episode; a genuinely undated one
falls back to the occurrence tiebreaker.

**Two temporalities, kept apart.** An episode's clinical validity window lives on the row
(`onset_on` opens it, `resolved_on` closes it — the end is payload, not identity: a
resolution date is news about an episode, not a different episode). *When we learned it,
and from which source* stays owned by the provenance and curation layers —
`document_id`/`attested_*`, `dedup_occurrence`, the verdict overlay. There is deliberately
no SCD-2 effective-dating (`effective_from`/`effective_to`/`is_current`) on clinical rows:
that would fuse the two temporalities into one column set and make every read ask which
kind of time it meant.

**One consequence to know about.** An undated umbrella claim and a dated episode are two
identities, so a later document that dates a stored undated problem lands as a **new row**
beside it rather than enriching it in place (`onset_on` left `_COMPARE_FIELDS`, so
`_sparse_gains` cannot fill it). That is the intended semantics — auto-absorbing one into
the other is exactly the silent fold this change ends — but unannounced it would trade
silent folding for silent duplication, so `pemr verify` **warns** when a person has an
undated condition row beside dated episodes of the same problem, naming both remedies.

**Migration: rekey, then unfold.** The identity change is a key change, so it is a `pemr
rekey`, not a SQL migration (SQL cannot compute the sha256 — the migration-006 precedent
above). It is collision-free by construction: adding a discriminator to a key only ever
*splits* families, never fuses two, and a split sibling keeps its stored
`dedup_occurrence`, so no two rows can land on one key. A family split leaves a hole (the
former occurrence 1 sits on `hash(base|1)` of its new base with no occurrence 0); that is
deliberate, for the same reason deletion does not renumber — renumbering rewrites sibling
keys out from under any staged conflict.

```
pemr rekey --apply --json > rekey.json      # dated conditions move; undated ones do not
pemr record reaffirm --map-file rekey.json  # dry run, then --apply
```

**Unfolding an umbrella row is an operator sequence, not an inference.** Nothing in a
stored row says it holds two episodes, and the free-text `note` must never be parsed for
dates. The operator gives the umbrella row the episode it actually is, then files the other:

```
pemr record edit condition <id> --identity --set onset_on=2009-09-15 --note "..." --apply
pemr record assert condition --person <slug> --field name=... --field onset_on=1998-05 ...
```

**Correcting an onset is itself a rekey, and carries its verdicts across.** With onset in
the identity, fixing a wrong onset date moves the row's key — so it is `record edit
--identity` (§5), a one-row rekey rather than an in-place correction, and its consequences
are the ones `pemr rekey` already has, reported the same way rather than by a new mechanism:

- **row-scoped** verdicts follow the row for free — `curation` resolves them by
  `record_id`, and the `dedup_base` a row verdict also carries is a breadcrumb nothing
  consults (that exclusion from the orphan kinds is load-bearing, above);
- a **family-scoped** verdict on the base the row vacated is orphaned exactly when a
  `rekey` base move would orphan it — only if the row was the family's last, which is
  `no-live-family`'s own rule — and the report carries it in `rekey --apply --json`'s
  `orphans` shape, so `record reaffirm --map-file` re-points it unmodified;
- payload, provenance and row id are untouched (which is the whole reason this is not
  `record rm` + re-commit: that re-attributes the row to whichever document is passed at
  correction time), the correction mark and the edit ledger land as on any correction, and
  the ledger entry keeps the **old** base as its breadcrumb so the move stays
  reconstructable;
- an open conflict anchored to the row is **refused** when the move would empty its family
  and disclosed as re-anchoring when a sibling survives — `record rm`'s rule, for its
  reason;
- moving onto an identity a row already occupies with the same payload is **refused**: that
  is a merge, and merging is a recorded human judgment (`record annotate --status
  merged-into`, or `record rm`), not something a correction may do silently.

---

## 4. Ingestion pipeline

```
inbox/scan.pdf
  → pemr ingest inbox/scan.pdf --person jane-doe
      0. refuse Google Drive pointer stubs (`.gsheet`/`.gdoc` — a ~1 KB JSON link,
         not the document); pre-hash, so a refusal writes nothing
      1. hash bytes; if known → report duplicate, stop
      2. resolve document text (agent-supplied, or --ocr), then verify the owner:
         text naming a different roster person, or — in prose only — a patient-
         identity header naming nobody → refuse pre-write (--force overrides)
      3. move blob → sources/<sha>/<sha>.pdf   (immutable)
      4. insert document row (category/provider left null for now)
  → AGENT step (vision): read the source, emit proposed rows as JSON
      matching the record schemas (lab_result[], medication[], observation[]...)
  → pemr commit-extraction --document <id> --json extracted.json
      5. validate JSON against schema (types, required fields)
      6. compute dedup_keys; split into {new, duplicate, enriched, conflict}
      7. insert new; report the rest
  → pemr review-conflicts   (if any)
```

Division of labor: **the LLM only does the fuzzy vision-to-structure step.** Hashing,
validation, dedup, insertion, conflict detection are all deterministic Python the agent
can't get subtly wrong. That's the whole point of lifting them out.

Optional `--ocr auto` flag pre-fills `document.ocr_text`, giving the agent text to work
from instead of re-reading the source every time. It extracts by whatever route the file
type allows (issue #66), with only the image and PDF routes reaching outside the stdlib:
`.txt/.md/.csv/.tsv/.json/.log` read directly, `.docx`/`.xlsx` unzipped and their OOXML
parsed, `.html`/`.htm` parsed with `html.parser` (issue #173 — tags stripped,
`<script>`/`<style>` dropped, tables as tab-delimited rows), **everything else** through
`tesseract` (a soft dependency) — no image-suffix
allowlist, so `.jfif`, `.jpe` and extension-less scans OCR like any other image. Every
OOXML member goes through one guarded parse helper that applies the same encoding-agnostic
`<!DOCTYPE` refusal described for CCDA below before `ElementTree` sees the bytes (issue
#155): the byte cap bounds a member's *declared* uncompressed size, not entity expansion,
so a 355-byte `.docx` expanded to 4,194,304 stored chars before the guard. A DOCTYPE on
any member refuses the whole file, degrading exactly like a malformed one — stderr note,
no `ocr_text`, document kept.

A **CCDA** `.xml` (C-CDA / HL7 CDA R2 — what a US portal's "download my record" produces)
is read natively too (issue #138): each `structuredBody` section's title and narrative,
tables as tab-delimited rows, prefixed with the `recordTarget` patient name and birth
time so the owner check has an identity to match. Detection is the parsed **root element**
(`{urn:hl7-org:v3}ClinicalDocument`), never the suffix — `.xml` is a container, so any
other XML keeps the OCR route. A `<!DOCTYPE` is refused before parsing (stdlib
`ElementTree` expands internal entities, and the size cap does not bound expansion: a
1.4 KB file rendered 1 MB of text, a 2 MB one 210 MB). That refusal is **encoding-agnostic
by construction** — it asks expat, through a probe that aborts at whichever comes first,
the DOCTYPE declaration or the root start tag, rather than scanning for byte patterns.
Two hand-rolled scanners were bypassed before it: a fixed head window (a leading comment
pads the DOCTYPE past it while the CCDA markers stay inside), then a whole-prolog ASCII
scan (in UTF-16 every marker is `<\x00!\x00…`, so the scan read the first `<` as a start
tag and never saw the DOCTYPE, while ASCII marker bytes smuggled into a CJK comment kept
the file sniffing as a CCDA). Establishing the encoding is exactly what a byte scanner
must re-implement to be correct, and expat has already done it — from the BOM and the
`encoding=` pseudo-attribute — before it reports either event, so asking it is both
cheaper and the version that cannot be re-bypassed. Detection itself stays a *cheap ASCII
negative* over the first 8 KB, which means a genuine UTF-16 CCDA is not read natively and
keeps today's OCR route: a deliberate narrowing, since a negative filter can be
conservative for free while the refusal cannot. A malformed file is simply "not detectably
a CCDA" and falls through. The narrative walk
is **iterative, not recursive**: nesting depth is document-controlled and ~1500 levels fit
in 30 KB, so a recursive walk hit `RecursionError` — a `RuntimeError`, outside the
best-effort handler — and cost the whole document. `RecursionError` is caught there now
as well, since the stdlib's own `itertext()` walk is a recursive generator. Only the narrative
is read: CCDA's coded entries are out of scope, since in real exports they are only
selectively trustworthy (medication codes arrive `nullFlavor="UNK"` with the drug name
only in the narrative, and historical entries carry a synthetic placeholder prescriber).

`.pdf` takes its own branch inside `run_ocr` (issue #70), because `tesseract` alone cannot
read a PDF at all — its Leptonica backend has no PDF decoder, so a scanned PDF is no better
off than a text-layer one. Instead a PDF is read page by page: a page with an embedded text
layer (≥ 20 characters) contributes it verbatim; a page without one is rendered at 300 dpi
grayscale and OCR'd. The decision is per *page*, so a scan appended to a searchable report is
still read. Pages are joined by a form feed (`\f`) — tesseract's own page separator, and a
token separator to FTS5, so it can never produce a false `find` hit. At most `OCR_MAX_PAGES`
(20) pages are read; a longer document gets a stderr note naming the shortfall. The PDF
backend (PyMuPDF, behind the `_load_pdf_backend()` seam in `ingest.py`) is the optional
`pip install pemr[ocr]` extra; without it a PDF stores no text and says so on stderr.

Formats that need a third-party parser are still deliberately out — `.rtf`, `.msg`, `.doc`.
For those you get a stderr note telling you to transcribe it yourself and pass
`--ocr-text-file`. Every route is best-effort and never fatal; a malformed file, an absent
`tesseract`, and a missing PDF backend all cost you the text, not the document. Extraction is
capped at 32 MiB per file — `ocr_text` is mirrored into the FTS index, so an unbounded read is
both a database-size problem and a decompression-bomb surface (a small `.docx` can declare a
gigabyte of `word/document.xml`).

Whichever route produced it, the *provenance* of the stored text is recorded alongside it in
`document.text_source` (issue #175, migration 015): text pemr extracted itself — any of the
routes above, a `document reocr` re-derivation, or a study's DICOM-header summary — is
`engine`; text the caller handed over verbatim (`--ocr-text-file`, `pemr document set-text`,
the `document_set_text` MCP tool) is `attached`. It follows the text's *origin*, not the verb
that wrote it, because engine text carries OCR-typical noise that a downstream consumer may
want to weigh differently, and the distinction is unrecoverable afterwards — a transcription
can be byte-identical to what OCR would have produced. `NULL` means no text, or text written
before 015; pre-015 rows are deliberately **not** backfilled, since hand-attached rows already
exist and a blanket `engine` would misclassify exactly the rows the column exists to find.

Extraction route feeds the owner check, and the route vocabulary has **three** words for
it: `native` (natively extracted, *structured*), `native-prose` (natively extracted,
*prose* — HTML, issue #173) and `ocr` (a tesseract pass or an agent transcription, prose
by definition). The identity-anchor (`suspect`) verdict is applied on the two prose
routes and withheld on `native` — the whole `.txt/.md/.csv/.tsv/.json/.log/.docx/.xlsx`
set, CCDA `.xml` included. In a
structured export
`Patient`/`DOB`/`MRN` are column labels and field keys, and counting them as an identity
header refuses ordinary lab exports as belonging to a stranger. A saved portal page is
the other case: tags aside it is a printed page, so its `Patient:` header **is** a claim
and the anchor stays armed — which is the whole reason the boolean `route != "native"`
grew into `_trusts_anchors()`, one predicate both call sites share. The line is the *route*
rather than how prose-like the format is, because the route is what the extractor actually
knows; the cost is that a prose transcript saved as `.txt` and ingested with `--ocr auto`
loses the anchor check too. That is no worse than before native extraction existed (such a
file went to tesseract, which declined, so there was no text and no check either), and the
`--ocr-text-file` path keeps full coverage. A CCDA is the same trade with a better floor:
its rendered `recordTarget` header is an affirmative name/DOB signal, so an export naming
the person ingesting it verdicts `match` — only the "names a stranger nobody on the roster
knows" case softens to `unverified`. `mismatch` — an affirmative name/DOB match on a
*different* roster person — is the half that actually prevents misfiling, and it blocks on
every route.

`document reocr` (issue #143) re-runs this same dispatch against a blob already in
`sources/`, closing the gap for documents ingested before an extractor fix (#70, #138)
landed — same routing, so the two paths cannot drift. Its `--force` carries two meanings at
once: it overrides both the has-text refusal (replace a populated `ocr_text`) *and* a
`mismatch` owner-check refusal on the recovered text, and the exit code stays 0 when the
latter fires. The verb's own backlog use case, `--where-empty`, needs neither sense of
`--force` — the population is unpopulated by definition, so a `mismatch` there still
refuses on its own and names `document reassign` as the remedy. `suspect` (no roster match
either way) stores and warns rather than refusing, since the recovered text cannot name the
wrong household member — refusing would only withhold the evidence that the document is
misfiled at the row level.

Both of `--force`'s meanings stop at the **shrinkage guard** (issue #174): re-derived text
shorter than the `ocr_text` already stored is refused (`shorter-text`, a `refused` status,
so rc=1), and `--allow-shrink` is the separate override. Separate deliberately — a
populated corpus needs `--force` just to reach the write at all, so it cannot also mean
"and discard most of it", and the only read-only owner audit there is (`reocr --force
--dry-run`, since `check_owner`'s three call sites are all write paths) would otherwise be
one missing flag away from losing text. The threshold is any shrinkage rather than a
percentage: it is unreachable without `--force` — the has-text skip returns first — so
`--where-empty` never trips it, and one predicate drives the refusal, the human line and
the `--json` `shrunk` key alike. `shrunk` also rides the writes that *are* permitted, so a
shorter replacement warns on its own line instead of reading as an ordinary success.
Truncation against `OCR_MAX_PAGES` is one *cause* of a shorter replacement, not the
condition — a document that is both reports both.

### Study directories (issue #69)

A burned imaging disc is clinically *one* document but physically one folder holding
`DICOMDIR`, thousands of extension-less slices, and a Windows viewer payload. `pemr ingest
<dir> --person <slug> --study dicom` (module: `study.py`) folds it into the pipeline above
rather than beside it:

```
disc/                                        (DICOMDIR + IM000001… + VIEWER.EXE + report.pdf)
  → include every file whose bytes 128..132 are `DICM`     (content, not extension:
      the viewer payload is excluded by construction; document-like drops are named
      on stderr, because a radiology report is its own document)
  → pack them into a *canonical* zip: entries sorted by POSIX relative path, ZIP_STORED,
      timestamps/host-system/mode pinned  ⇒ same disc, any machine, same bytes
  → sha256 of the archive IS document.sha256  ⇒ step 1 dedup and `pemr verify` work
      unchanged; the blob lands at sources/<sha[:2]>/<sha>.dcm.zip
```

The archive is staged in `sources/.tmp/` and `os.replace`d into the store, so a crash can
never leave a truncated blob under a valid content-hash name; the temp is unlinked on every
path (a re-ingest repacks before it can know it is a duplicate — the accepted cost of
hashing the bytes rather than a manifest). Studies over 4 GiB need `--allow-large`, since
`sources_dir` is cloud-synced by default.

`doc_date` (from `StudyDate`), `category` (`imaging`), and `ocr_text` (a short derived
summary: modality, date, per-series slice counts) are **defaults only** — anything the
caller passes wins. Header tags are read by a minimal stdlib parser, best-effort like
`run_ocr`: the zero-runtime-dependency rule (`pyproject.toml`) rules out `pydicom`, and any
parse failure yields no metadata rather than a failed ingest. Every length the file declares
is bounded before it is used, since a scratched disc's corrupt length field is otherwise an
unbounded allocation and a value spliced straight into `ocr_text`. Per-slice rows, pixel
decoding, and thumbnails are out of scope.

The step-2 owner check applies here too, and reads `PatientName`/`PatientBirthDate` from the
header rather than the derived summary — engine output carries no patient identity, and a
`StudyDescription` like "PATIENT POSITIONING" would trip the identity anchor into a spurious
refusal. Caller-supplied text is checked as well; the more consequential verdict wins, and
the refusal point is pre-pack, so a refused study costs nothing. **The identity tags are
verification input only** — they are never written to `ocr_text`, so they never reach the
FTS index or an agent's context. This matters most here: a 2,000-slice binary folder is the
one document type a human cannot eyeball to catch a misfile.

---

## 5. Tool surface

### CLI (the tested engine)

```
pemr person add|list|show|edit|deactivate|reactivate|remove
pemr person unit-pref set <slug> --key <key> --unit <unit> [--dictionary <toml>]
                                                         # the canonical unit `render summary` and `trends`
                                                         # DISPLAY this key in (§2 person_unit_pref); stored
                                                         # rows are never rewritten - to correct a genuinely
                                                         # mislabelled unit use `record edit`
pemr person unit-pref clear <slug> --key <key> [--dictionary <toml>]
pemr person unit-pref list <slug> [--json]
pemr ingest <file> --person <slug> [--ocr auto] [--force]        # --force: skip owner verification
pemr ingest <dir>  --person <slug> --study dicom [--allow-large] # a study folder as ONE document (§4)
pemr commit-extraction --document <id> --json <file>
pemr review-conflicts [--resolve <id> --keep existing|incoming|both|merge
                       [--field NAME=existing|incoming ...] [--adopt-source] [--note ...]]
                      [--dictionary <toml>]   # --adopt-source: only with --keep existing
pemr document list [--person <slug>]                     # newest first; omit --person for everyone
pemr document show <id> [--json | --text]                # one document's detail; --text dumps stored ocr_text
pemr document edit <id> [--doc-date|--category|--provider ...]   # partial update; "" clears a field
pemr document reassign <id> --person <slug> [--apply]    # move a misfiled document + records; dry run by default
pemr document rm <id> [--apply] [--purge-blob] [--tombstone [--reason ...] [--note ...]]
                                                         # delete a document + records; dry run by default
                                                         # --tombstone: also refuse to re-ingest this content (§3)
pemr record rm <table> <id> [--apply]                    # delete ONE record row; its document and every
                                                         # other record it produced survive; dry run by default
pemr record edit <table> <id> --set NAME=VALUE [--set ...] --note <text>
                 [--attributed-to ...] [--identity] [--apply]
                                                         # correct a row's NON-KEY fields in place (§2 record_edit);
                                                         # document_id/dedup_key/source blob untouched; NAME= clears;
                                                         # identity fields refused (that is a dictionary edit + rekey,
                                                         #        or --identity below);
                                                         # every change ledgered; the row is stamped edited_at/edited_by
                                                         #        so render/query disclose it (§2); dry run by default
                                                         # --identity: name identity fields and MOVE the row onto the
                                                         #        identity they derive - a one-row rekey (§3 Episodes).
                                                         #        Payload/provenance/row id/ledger/row verdicts follow;
                                                         #        a family verdict on an emptied base is reported in
                                                         #        `record reaffirm --map-file` shape; an occupied
                                                         #        target is refused (that is a merge, not a correction)
pemr record edit --list [<table>] [--json]               # recorded corrections, newest first (field: old -> new)
pemr record annotate <table> <base-or-id> --status <s> --note <text> [--attributed-to ...]
                     [--merged-into <base-or-id>] [--allow-cross-person] [--row] [--apply]
                                                         # record a human verdict over a record FAMILY (§2 curation):
                                                         # confirmed|superseded|erroneous-in-source|disputed|merged-into|distinct
                                                         # distinct: two rows that recompute to one dedup_key are TWO facts
                                                         #        (generic extracted labels); unblocks `rekey`, both stay live
                                                         # --merged-into must name a family of the SAME person; a cross-person
                                                         #        merge is refused (the fact would leave one chart without
                                                         #        appearing on the other) unless --allow-cross-person says so
                                                         # --row: scope it to that ROW only (a keep-both family holds
                                                         #        two live rows; row scope beats family scope there)
                                                         # pure overlay - no record row is mutated; dry run by default
pemr record annotate --list [<table>] [--json]           # current verdicts, newest first (scope + orphans flagged)
pemr record annotate <table> <base-or-id> [--row] --clear [--apply]   # lift one verdict, in the named scope
pemr record reaffirm [<table>] [--json]                  # the verdicts a rekey ORPHANED: no live family, or a
                                                         # dangling merged_into target (NOT the row-scope stale
                                                         # breadcrumb - that one still resolves)
pemr record reaffirm [<table>] (--map-file <file> | --clear) [--apply] [--json]
                                                         # bulk remedy (§3): --map-file takes `rekey --apply --json`'s
                                                         # payload and re-points each verdict onto its successor
                                                         # family; --clear lifts them instead. Dry run by default;
                                                         # a successor already carrying its own verdict is skipped
pemr record assert <table> --person <slug> --attributed-to <who> --date <iso>
                   --field NAME=VALUE [--field ...] [--apply]
                                                         # commit a fact attested by a PERSON, with no
                                                         # source document (§2 attestation); dry run by default
                                                         # a colliding family fully released by curation
                                                         # verdicts no longer blocks (§3, issue #133);
                                                         # disputed/unverdicted rows still refuse it
pemr record assert --list [<table>] [--all] [--json]     # attested rows still needing a source document
pemr document tombstone list [--json]                    # recorded intentional removals, newest first
pemr document tombstone add (--file <path> | --sha256 <hex>) [--reason ...] [--note ...]
                                                         # pre-emptive exclusion; ingests and copies nothing
pemr document tombstone rm <sha256>                      # lift one (full hash only)
pemr document set-text <id> --ocr-text-file <path> [--force]     # attach/replace ocr_text after ingest; FTS follows via trigger
pemr document reocr [<id>...] [--where-empty [--person <slug>]] [--dry-run] [--force] [--allow-shrink]
                                                         # re-derive ocr_text from the stored blob using the ingest dispatch
                                                         # --allow-shrink: store text shorter than what is there (refused otherwise)
pemr query labs --person jane --test hba1c --since 2023-01-01 [--raw]
pemr query meds --person jane --active [--raw]           # --active = query.med_is_current per row:
                                                         # a past ended_on or a terminal status ends
                                                         # the course, EXCEPT when status_reason is a
                                                         # renewal (query.RENEWAL_MED_REASONS, e.g.
                                                         # a CCDA's "Discontinued (Reorder)") - a
                                                         # renewed prescription's end date closes an
                                                         # authorization period, not the therapy, so
                                                         # it stays current and prints "(renewed)".
                                                         # Every other reason still ends the course
pemr query timeline --person jane --since 2024-01-01 [--raw] # merged event stream
                                                         # all three (issue #131): filtered at read
                                                         # time against the curation overlay, same
                                                         # rule §6 describes for render - a suppressed
                                                         # row prints a "N hidden; --raw to include"
                                                         # note; --raw restores them, marked; --json
                                                         # always carries the verdict regardless
pemr find --person jane "cholesterol"                    # full-text over ocr_text + records
pemr find "mmr booster"                                  # omit --person: whole-household, slug-prefixed hits
pemr trends --person jane --test hba1c                   # min/max/latest/slope
                                                         # a canonical display unit for the key (above)
                                                         # converts every point BEFORE the stats, so the
                                                         # numbers and the printed unit cannot disagree;
                                                         # a point that cannot be converted is kept and
                                                         # disclosed, never dropped
pemr trends --person jane --test weight                  # a vital key charts too (labs + obs_type='vital');
                                                         # a token present in BOTH tables is refused, not
                                                         # merged - rc=1 naming both sources
pemr due --person jane                                   # screening/vaccine gaps — NOT IMPLEMENTED (phase 7)
pemr render summary --person jane        > exports/jane-summary.md
pemr render brief --appointment <id>     > exports/brief.md
                                                         # --include-self-reported: also show symptom/activity
                                                         # rows in Procedures & Observations (default: hidden, #180)
pemr render journal --person jane        > exports/jane-journal.md
pemr render curation --person jane       > exports/jane-curation.md  # curation audit trail (empty = no verdicts)
pemr backup                                              # VACUUM INTO snapshot
pemr restore latest [--force]                            # install a snapshot back over pemr.db (§8)
pemr verify                                              # integrity + row counts + source-blob resolution
pemr migrate [--create]                                  # apply pending migrations (--create bootstraps a new DB)
pemr rekey [--apply]                                     # re-derive dedup keys after a dictionary edit
                                                         # --apply also reports the curation verdicts THIS run
                                                         # orphaned; `--json` carries them as `orphans`, which is
                                                         # the map file `record reaffirm` reads (§3)
```

Every line above is implemented and parses today **except** the one flagged `NOT IMPLEMENTED`.
`pemr due` is phase 7 (§9) — the `screening`/`immunization` `observation` rows that
`data/dictionary.example.toml` and `AGENTS.md` tell extraction agents to emit are accruing
ahead of their reader, by design. Flag any future entry the same way rather than listing it bare.

Invoke as `pemr <cmd>` (console script) or `python -m pemr <cmd>` (`pemr/__main__.py`,
delegating to `cli.main`) — the latter is the portable fallback when the console-script
launcher isn't generated (e.g. a system Python whose `Scripts`/launcher dir isn't writable
under a PEP 660 editable install); see issue #22.

### MCP tools (thin wrappers, same verbs) — implemented phase 5

Read-only: `person_list`, `person_show`, `query` (`kind` = `labs`/`meds`/`timeline`), `find`,
`trends`, `render_summary`, `render_brief`, `render_journal`, `render_curation`. Write:
`person_add`, `person_edit`,
`ingest` (`study="dicom"` + `allow_large` make `file` a study directory, §4), `commit_extraction`,
`document_set_text` (fills an empty `ocr_text` only — the `--force`
replace is CLI-only), `review_conflicts` (resolution gated on human sign-off). Each returns the
same `--json`-shaped payload as the CLI; the MCP server (`pemr/mcp_server.py`) parses args and
calls the same Python functions the CLI calls — one implementation, two front doors. `readOnlyHint`
annotations expose the read/write split to the client.

Since issue #131, `query`'s MCP payload also carries the `curation` overlay — every row/event
that resolved a verdict is annotated, unfiltered (the MCP surface mirrors `--json`, never the
human view's suppression). The nested verdict's `dedup_base` is a **breadcrumb, never a lookup
key** (§2/§3): it can go stale across a `rekey`, so a caller keys on `status`, not on it. This is
the one place verdict *content* reaches an agent — the write verbs (`record annotate`,
`record reaffirm`, below) stay CLI-only regardless.

`AGENTS.md` documents this contract so any agent (Cowork, Claude Code, local) knows to
**call tools, not reinvent** — and specifically: never write to the DB except through
`commit_extraction`/`person_add`/`person_edit`/`ingest`/`document_set_text`; always `ingest` (with `ocr_text` populated) before
extracting; dictionary additions go through human review, never agent-direct edits.

`document rm`, `record rm` (issue #107), `record annotate` (issue #109), `record reaffirm`
(issue #126), `record assert`
(issue #110) and `record edit` (issue #129) are deliberately **absent** from `WRITE_TOOLS` — this is a rule, not an
oversight. Deletion of PHI stays a human-at-a-terminal action; no MCP tool, and therefore no
agent, can remove a record or a document. `record annotate` joins them for the adjacent
reason: a `curation` verdict is a *human's clinical judgment*, recorded with attribution, and
an agent that could write one could make a record disappear from every generated document
without deleting a row — and `record reaffirm` writes those same verdicts *in bulk*, which
only sharpens the argument. `record assert` is the sharpest case of all — it is a *write*, not a
deletion, and the only path in the system that can put a fact into the record with no
external source. `record edit` is that same boundary from the other side: it changes a stored
clinical value with no new source behind the change. Any future destructive — or render-altering, or unsourced — verb should
default to the same exclusion unless a human explicitly decides otherwise.

---

## 6. Generated documents (DB → disposable output)

`render.py` produces your current deliverables as pure functions of DB state:

- **master summary** — active meds, conditions, allergies, latest vitals, recent
  abnormal labs, open follow-ups. One query bundle → Markdown. Open conflicts are not
  decoration: an open conflict means a stored value is disputed and its correction is
  still staged, so the summary would otherwise print the stale value silently. Since
  issue #168 the summary says so in a single `> [!WARNING]` line under the header,
  emitted only when the count is non-zero — the safety property of issue #59 without an
  always-present `## Open Conflicts` / `_none_` header on the common case. The brief keeps
  the per-conflict section, since it is per-appointment. An
  order in `Orders & Referrals` leaves the section once a matching `lab_result` lands
  (issue #128) — matching is deliberately narrow (exact `key_token` plus a tight,
  edge-tested date window) and biased toward under-suppression, since age alone is never
  a signal and a hidden-but-still-open order would be the worse failure; the window
  constants are guarded by a pinned edge test so a casual widening doesn't slip through.
  A *compound* order key (one order naming several analytes, `cbc,cmp,ldh`) decomposes on
  `,`/`/` at parenthesis depth 0 into component tokens, each still matched exactly, and
  leaves the section only when **every** component resulted in window (issue #145) — the
  order side alone decomposes, and a partially resulted panel is still outstanding. A
  decomposed component drops structural words that name no analyte (`panel`, `profile`,
  `extensive`), so `Immunofixation Panel` can match an `Immunofixation` result; if that
  leaves a component with nothing readable, the whole key is voided rather than the
  component dropped, so the surviving analytes can't suppress an order still naming
  something unread. Those
  separators are content, not structure, inside many single analytes' names (`Glucose,
  fasting`, `Kappa/Lambda Ratio`), so two guards keep #128's behaviour reachable: a key
  the dictionary declares as one analyte is never split, and the whole-key match is tried
  before the per-component one — decomposition can only ever move an order from
  "renders" toward "suppressed", never take away a suppression that already worked.
  `Abnormal Labs` is bounded to the last 12 months of the render's `now` (issue #165) —
  unbounded it renders the entire abnormal history, where a live marker reads exactly like
  one abnormal a decade ago — with a **keep-latest-per-analyte guard** that always retains
  an analyte's most recent abnormal result however old it is. The guard is not politeness:
  a fixed window alone renders the section *empty* for a person on a slow draw cadence,
  and an empty section reads as "nothing flagged", which is worse than the dump it
  replaces. Like the #128 order constants the window is a named constant, and the heading
  text is built from it, so the stated window and the actual filter cannot desync.
  `Procedures` (issue #166) lists `procedure` rows reverse-chronologically, undated last,
  narrowed by a routine-pattern list authored in `dictionary.toml` (`[procedures].routine`)
  — those rows arrive largely from billing documents, so the table mixes genuine
  procedural history with routine service lines (office visits, serial radiographs,
  venipuncture) and rendering all of them recreates the unreadable-section problem #165
  bounds. The list is **default-show** (a name matching no pattern always renders, so
  significance is never established by absence from a list), **suppress-only and
  summary-only** (the brief and the journal stay the complete record and no stored row
  changes), and **disclosed** — the section states how many rows it hid, on #93's
  precedent. Matching is token-boundary on normalized text, both sides, so `cast` cannot
  suppress `Castration`: over-suppression is the failure that matters here, and
  under-suppression only costs a line. Normalization also strips parenthetical qualifiers
  on both sides, so `cast application` also suppresses `Cast application (open reduction
  internal fixation)`, and a pattern written entirely inside parentheses matches nothing.
  Unlike `[synonyms]` the list never reaches a dedup key, so editing it needs no
  `pemr rekey`.
- **appointment brief** — for a given upcoming appointment: relevant history for that
  specialty, recent labs/imaging, current meds, med-interaction flags, suggested
  questions. This is your "walk-in readiness" as a repeatable command. Since issue #180,
  `## Procedures & Observations` hides self-attested `symptom`/`activity` rows by
  default (`include_self_reported: bool = False`, `--include-self-reported` /
  `include_self_reported` on the CLI and MCP surfaces, mirroring `render_journal`'s
  identical #167 flag) — a record with none renders byte-identically to before the
  flag existed. Unlike the routine-procedures list (above) and order grouping's `+N
  earlier` (#93), this suppression is **not disclosed on the page**: no `+N hidden`
  line and no collapsed summary section catch the hidden rows in the brief itself
  (they remain fully visible via `pemr query timeline`). This is a deliberate,
  human-approved exception to the disclosed-suppression convention this section
  otherwise follows — flagged at audit for the merge reviewer, not a bug.
- **journal** — chronological event stream (documents + appointments + procedures)
  rendered as a narrative timeline.
- **curation record** (issue #168) — the audit trail: every recorded verdict that removed a
  row from the three documents above, grouped **by ruling** rather than by row, so one merge
  session's note against forty families is one block with a count. Read from the stored
  `curation` table (not from a render pass), so it is person-scoped and complete rather than
  "whatever sections happened to select" — a deliberate superset of the
  `## Superseded / corrected` appendix it replaced. Empty output when the person has no
  verdicts, which is what keeps the additive-only guarantee below true.

Every section is filtered at read time against the `curation` overlay (§2, issues #109 and
#114): `superseded` / `erroneous-in-source` / `merged-into` leave their section entirely
(their trail is the curation record), `disputed` renders in place with a
`[DISPUTED: <note>]` marker and reaches the brief's `## Questions for the Clinician`, and
`confirmed` — and `distinct` (§3, issue #122), whose whole point is that both rows stay
live — render unchanged. The questions section is omitted entirely when empty, so a record
with no verdicts renders byte-identically to before the overlay existed. This does not weaken
the purity rule: the filter is a read, and output changes after a verdict because the
*database* changed. Resolution is **per row**: a row-scoped verdict affects only its own
occurrence — the sibling of a `--keep both` pair renders untouched — and beats the family
verdict for that row.

The read layer's three `query` verbs (`meds`/`labs`/`timeline`, §5) apply the same rule as of
issue #131, via shared stampers in `pemr/curation.py` (`annotate_rows`/`annotate_events`) that
`render` now delegates to rather than duplicates: their default human-readable output suppresses
appendix-status rows the same way, `--raw` opts back into the unfiltered view (marked), and
`--json` — like the MCP `query` tool, §5 — always carries the verdict under a `_curation` key
regardless of suppression, so a programmatic caller can filter for itself. That verdict payload
is now part of the `--json` **and MCP** read contract: its `dedup_base` is a breadcrumb (§2/§3),
not a lookup key, since a dictionary `rekey` can move it out from under a stale reference — a
consumer should key on `status`, not on it.

One consequence of the read-time join: if a dictionary-driven `rekey`
(§3) has renamed a family since its verdict was recorded, a *family*-scoped verdict's join
misses and the family renders as if unannotated (`pemr verify` flags the orphan; nothing
re-attaches it automatically). A *row*-scoped verdict is immune — it joins on the row id,
which `rekey` never renumbers — and is orphaned only by the removal of its row.

That immunity has a price, and it is a rule rather than a caveat: **a row-scoped verdict is
identified by a reusable row id, so removing the row must retire the verdict.** Record ids
are plain rowid aliases (no `AUTOINCREMENT`), so deleting the highest-id row frees that id
for the next insert, and a verdict left behind would re-attach to an unrelated new record —
pulling a live clinical fact into the superseded appendix under a note about a different one,
with `verify`'s orphan warning silent because the id resolves again. Both write paths that
delete a record row (`record rm`, `document rm`) therefore name any row-scoped verdict on the
doomed rows in their dry run and lift it in the same transaction as the delete. Family scope
needs no such rule: `dedup_base` is content-derived, so re-attaching to a re-ingest of the
same fact is the intended behaviour.

**Display-time unit canonicalisation** (issue #136) is a second read-time overlay, and it
rests on exactly the same purity argument. When a person has recorded a canonical display
unit for a measurement key (`person_unit_pref`, §2), `render summary` converts that key's
Latest Vitals and Abnormal Labs into it — value *and* reference interval together,
since a value in `lb` beside a `(ref ...)` still in kg is a clinical misread — and `trends`
converts every point of the series **before** computing min/max/latest/slope, so the stats
and the printed unit cannot disagree. Every converted number is disclosed where it prints
(`[converted from 77.6 kg]` in the summary, a `note` line in `trends`), and unit conversion
is arithmetic, never a relabel: temperature is affine, and an unknown unit, an absent unit,
a non-numeric value or a cross-dimension preference all resolve to "print the stored value
untouched" rather than guess at a scale. Two deliberate limits keep it inert everywhere
else: **abnormality is still decided on stored values**, so no preference can change which
labs appear in a section; and only the two read paths named here honour it — `render brief`,
`render journal`, `query labs` and the MCP write surface are untouched. A person with no
preference set renders byte-identically to before the overlay existed.

Because they regenerate from truth, they never drift. Old exports are disposable.

---

## 7. Multi-person extensibility

- One DB, `person_id` everywhere → adding a family member is `pemr person add`, nothing
  else. Same tools, same dictionary, zero code duplication.
- Cross-person queries fall out for free: `pemr due --all` (phase 7, not implemented),
  "who's overdue for a physical," family-wide med lists.
- New record *type*: add rows to `observation` immediately; promote to a typed table
  with a migration only when it earns its keep. Neither requires touching agents.

---

## 8. Backup & safety

- Live `pemr.db` on a **local-only** path (out of the sync root) — WAL mode means
  `-wal`/`-shm` sidecars that cloud sync loves to corrupt mid-write.
- `pemr backup` → `VACUUM INTO backups/pemr-YYYYMMDD-HHMM.sqlite` (a clean,
  single-file, consistent snapshot), then the file lands in the synced folder. Every
  snapshot is `integrity_check`ed at *write* time, before rotation can run — an
  unreadable backup discovered at restore time is the classic failure mode, so a bad
  snapshot is unlinked and the command fails instead of pruning good snapshots to make
  room for a worthless one.
- Rotation: keep last N daily + M weekly; prune older.
- Optional: run `pemr backup` from Windows Task Scheduler nightly.
- `sources/` (content-addressed originals) can also sync — they're immutable blobs, safe
  for sync, and give you off-machine copies of the irreplaceable scans.
- Exposure posture per your call: private-ish, not encrypted-at-rest. Easy upgrade later
  — snapshot to an encrypted 7-Zip/age file before it syncs — without touching the schema.

### Restore

`pemr restore <snapshot|latest> [--force]` is the other direction. It is a command
rather than a runbook because every failure mode here is operator error under pressure,
and a command is testable (§5, "the tested engine"):

1. **Validate the source first.** Opens as SQLite, passes `integrity_check`, and has a
   pemr schema. Any failure aborts with the live DB untouched.
2. **Guard the destination.** No live DB (the actual disaster) → proceeds with no flag;
   the ergonomics are deliberately easiest when the user is panicking. A live DB present
   → requires `--force`.
3. **Rescue copy.** With `--force`, the current database is snapshotted to
   `pemr-prerestore-YYYYMMDD-HHMMSS.sqlite` *before* anything is overwritten; if that
   fails, the restore aborts. Restore is therefore non-destructive by construction.
4. **Install atomically.** Staged as `pemr.db.restore-tmp` in the destination directory,
   then `os.replace`d — a crash mid-install never leaves a half-written database, and a
   failed *copy* has destroyed nothing.
5. **Clear the sidecars**, and only now. `pemr.db-wal` / `pemr.db-shm` are deleted, so
   SQLite cannot replay a stale WAL over the restored file. Deliberately after the
   install rather than before: a stale `-wal` holds committed-but-uncheckpointed
   transactions, so deleting it on a path that then failed to install would be the one
   way this command could lose data. Nothing opens the database in between.
6. **Migrate.** A snapshot older than the code is the *normal* case; making the operator
   remember this step is exactly the trap.
7. **Verify.** Prints the `pemr verify` report: integrity, migrations, per-table row
   counts, and blob resolution.

Every abort path up to and including step 4 leaves the live database exactly as it was.

Between steps 3 and 4 a read-only diff names any **`document_tombstone` rows** (§3, issue
#80) the live database holds and the snapshot does not. A whole-database replace drops
them — correct, since it equally drops every document and person edit made since, and
merging them would break both the atomic single-file install and the "every abort leaves
the live database as it was" property. But the *consequence* is issue #80's own bug
resurrected (the next sweep silently re-ingests an excluded document), so the loss is
reported before the replace, naming each hash, the rescue copy that still holds them, and
`pemr document tombstone add --sha256 <hash>` to re-apply. Reported, never merged, never
blocking.

`pemr verify` runs step 7 on its own, any time. Blob checking is exact rather than
heuristic — `document` stores both `sha256` and a relative content-addressed
`source_path`, so verify resolves `sources_dir/source_path`, re-hashes, and reports
*missing* separately from *mismatched* (a corrupted blob is a different problem from an
unsynced one). Blob problems are a **warning at rc=0** during restore: the database
restore genuinely succeeded, and `sources/` may simply be mid-sync. Standalone `pemr
verify` is the opposite — it exits **1** whenever the report lists a problem, in `--json`
mode exactly as in console mode, because `--json` is the mode a monitoring job picks and
the exit code is what such a job checks. `verify` reports, it never repairs.

**`migrate` will not create a database.** Before this existed, a user whose `pemr.db`
had vanished was told `run pemr migrate first` by every read command — and following
that advice built a brand-new empty database and reported success, manufacturing a
convincing empty archive. Worse, that empty DB was then a valid backup source, so the
next `pemr backup` snapshotted it and rotation could prune the real snapshots. `migrate`
now refuses when there is no database (or a zero-byte one) and points at
`pemr restore latest`; `pemr migrate --create` is the explicit bootstrap for a genuinely
new archive.

**And an empty database is not a backup source.** Closing the `migrate` route above only
closed the first link of that chain — a zero-byte or schema-less `pemr.db` arriving any
other way (an interrupted copy, cloud-sync debris, `type nul > pemr.db`) would still have
been snapshotted happily, because `VACUUM INTO` on an empty file produces a structurally
valid, integrity-clean 4 KB database that rotation then treats as the newest snapshot of
the day. `pemr backup` therefore refuses a database that does not exist, is zero bytes,
or has no pemr schema, *before* writing anything — so rotation is never reached and the
real snapshots survive.

### What backups do not protect you from

- **Same-day loss.** Rotation keeps the *newest* snapshot per calendar day, so intra-day
  snapshots collapse: "I corrupted the DB an hour ago" is generally **not** recoverable
  from backups alone, because today's earlier snapshots are already gone. Take a
  snapshot before any bulk or destructive operation (`rekey --apply`, bulk ingest) —
  that is necessary, not optional, since it is protected only until the next backup of
  the same day.
- **Pinning a snapshot** is the escape hatch: rotation only ever parses and prunes names
  matching `pemr-<8 digits>-<4|6 digits>.sqlite`, so *renaming* a snapshot off that
  pattern makes it permanent. This is the same mechanism that makes
  `pemr-prerestore-*.sqlite` rescue copies immune to rotation by construction.
- **Blobs.** The DB and `sources/` are separate; a DB restored from a snapshot newer
  than the sources backup references blobs that aren't there. `pemr restore` and
  `pemr verify` report this, but restoring `sources/` itself is out of scope — blobs are
  immutable and separately synced.

---

## 9. Build phases

1. **Skeleton** — package, `db.py`, migration `001_init.sql`, `person add/list`, config.
2. **Ingest + dedup core** — hashing, blob store, `document` rows, `commit-extraction`,
   both dedup layers, conflict table. (The determinism payload.)
3. **Query layer** — `query`, `find` (FTS5 over `ocr_text` + records), `trends`.
4. **Render** — master summary + appointment brief + journal.
5. **MCP wrapper** + `AGENTS.md` contract. **Done.**
6. **Backup** command + scheduled task.
7. **Care-gap rules** (`due`) — vaccines/screenings by age/sex; lowest priority, highest
   ongoing value.

Migrate existing Cowork CSVs/journal in as the first real dataset once phase 2 lands —
it's the best possible dedup/extraction test corpus.

---

## Open questions

- **Analyte dictionary seed**: ~~start from your existing lab CSVs' column headers, or a
  standard LOINC subset?~~ **Resolved (phase 4.5):** seed from the real-corpus report
  vocabulary (CSV `test_name` headers + observed report abbreviations), not LOINC. The
  starter `data/dictionary.example.toml` now carries CMP/CBC panel codes, serum free
  light chains and SPEP naming variants; `norm()` additionally strips parenthetical
  qualifiers (`(HGB)`, `(SPEP)`, `(calculated)`) and compares units case-insensitively
  so the dictionary only needs bare canonical spellings. **Amended (issue #71):**
  stripping is right for `norm()` (the analyte family) but was wrong for the *key* — it
  collided a CMP `Albumin` with an SPEP `Albumin (SPEP)`. Keys now use `key_token()`,
  which keeps a parenthetical unless it is a redundant alias of its stem or the
  dictionary declares it noise via a full parenthesized key (§3).
- **FTS**: ~~SQLite FTS5 is plenty; confirm you don't need semantic/vector search over
  notes (could add a sidecar later).~~ **Resolved (phase 3):** plain SQLite FTS5, no
  vector sidecar. `migrations/003_fts.sql` adds a standalone `record_fts` index over
  `document.ocr_text` + record text fields, kept current by triggers on the base tables
  (ingest/commit paths unchanged) and backfilled on migrate. A semantic/vector sidecar
  can bolt on later without schema changes if keyword search proves insufficient.
- **Med interactions in briefs**: ~~rules-based flags only, or call an external drug DB?~~
  **Resolved (phase 5):** neither — an external API would send meds off-machine, and an
  in-engine rules DB isn't a pure function of DB state. `render brief`'s placeholder section is
  filled by the **agent's own general knowledge**, under fixed framing `AGENTS.md` mandates
  verbatim (AI-generated, not a drug-interaction DB, verify with pharmacist; never claims safety
  or gives dosing/start-stop advice).
```
