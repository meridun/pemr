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

- No HIPAA/PHI compliance layer, no provider interoperability (HL7/FHIR/CCD).
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
  exports/                     # generated docs (disposable): summaries, briefs, journal
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

Provenance — every structured row traces to a source document:

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
```

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
  status        TEXT,                     -- active|discontinued|prn
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
  onset_on      TEXT,
  resolved_on   TEXT,
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
                                           -- 'screening', 'immunization'
                                           -- (condition/allergy graduated in 006)
  observed_at    TEXT,
  key            TEXT,
  value_num      REAL,
  value_text     TEXT,
  unit           TEXT,
  dedup_key      TEXT NOT NULL,
  UNIQUE(dedup_key)
);
```

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
lab_result.dedup_key   = hash(person_id | key_token(test_name) | collected_at)
medication.dedup_key    = hash(person_id | norm(name) | dose | started_on)
procedure.dedup_key     = hash(person_id | norm(name) | performed_on)
appointment.dedup_key   = hash(person_id | provider | scheduled_for)
observation.dedup_key   = hash(person_id | obs_type | observed_at | key_token(key))
allergy.dedup_key       = hash(person_id | norm(substance))
condition.dedup_key     = hash(person_id | norm(name) | subject)
    subject = 'family:' + norm(relation)  when status = 'family-history'
            = 'self'                      otherwise
```

The last two are deliberately **date-free**: allergies and problem lists are *standing
facts* restated on every document with inconsistent or absent dates, so a date in the key
would fork one allergy into one row per document. Their dates are payload, and a stated
disagreement (in a date or anywhere else) stages a conflict. A field the incoming document
simply omits is silence, not a change, and never conflicts — the one place the
duplicate-vs-conflict comparison differs by record type (`dedup._SPARSE_TYPES`). The
`subject` discriminator is a correctness fix, not a nicety: without it a patient's
diabetes and her mother's derive one key and silently merge.

That reading is asymmetric by design. Treating a *stored* NULL as silence too would make the
common terse-then-detailed document sequence lossy: the second document's `criticality` would
count as a duplicate and be dropped, with no conflict to catch it. So a field the incoming row
states over a stored NULL is a **gain** — no competing value, nothing to adjudicate — and fills
the stored row in place, reported as `enriched` (a fourth commit bucket beside
new/duplicate/conflict). A stated value never overwrites a stored one on that path, so
enrichment cannot launder a disagreement into a silent overwrite. The same reading governs
`keep incoming` on these types: it writes the fields the incoming row states and leaves the
rest as stored, so a one-field adjudication doesn't erase the row's other payload.

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

**A dictionary edit is retroactive only if you make it so.** Stored keys are frozen at
commit time, so a new synonym changes the key a *future* commit derives for a fact already
in the DB: layer-2 dedup misses it and the same fact lands twice. `pemr rekey` re-derives
every stored key under the current dictionary (dry-run by default, `--apply` to write,
values and provenance untouched). Two rows that recompute to one key are a **collision**,
and the message names which of the two causes it is: *different* payloads mean the new
synonym fuses two distinct facts (e.g. a CMP `ALB` and an SPEP `Albumin` off one draw) and
the fix belongs in the dictionary; *identical* payloads mean one fact was filed twice, once
under a pre-drift key, and the fix belongs in the data.

**A collision quarantines its own table, not the run** (issue #92). The record tables are
scanned independently, so a fused pair in `allergy` says nothing about `condition`. The
scan therefore never stops at the first collision: it reports every collision in every
table — a dry run writes nothing by definition, so it has to be usable as a full survey —
and `--apply` writes the tables that came out clean, leaves the colliding ones on their
stored keys, and names them (`skipped: collision` per table, plus a `skipped` list in
`--json`). Exit code is 1 whenever any collision was found, in both output modes: partial
progress is fine, silent partial progress is not.

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

The **measured value is deliberately *not* in the key** — temporal identity carries the
draw instead. `collected_at`/`observed_at` are used at full precision (timestamp when the
document gives one, date when it only gives a date), not truncated to the date. Two draws
of the same analyte on the same day at different times (serial glucose, peri-op, inpatient
q6h) get distinct timestamps → distinct keys → two rows preserved; a correction or OCR
re-read of the *same* draw carries the same timestamp → collides → surfaces as a conflict
(below). When only a date is available, same-day differing values collide → conflict; that
safety bias is intentional (a spurious conflict on a genuine repeat is human-recoverable, a
silent duplicate of a correction poisons `trends`/brief/`query` irrecoverably).

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
`WHERE dedup_key = conflict.dedup_key`. Once occurrence 0 is gone (`document rm` or
`document reassign` of the document that owned it) the anchor's own key is
`hash(base | n)`, so a key-targeted write would match nothing while the conflict was
stamped `resolved`, silently discarding the staged value. If nothing is left in the family
at all, `keep incoming` **refuses** rather than reporting a success that wrote nothing;
`keep both` still admits the staged row, at occurrence 0.

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
join) an unrelated record while the conflict's own row went untouched — and `rekey` refuses
to run in that state, so the database stays there. Conversely, numbering an admitted row on
a stale base that no row carries would give it a key not derivable from its own columns,
which collides the family on the next `rekey` and — because `rekey` is all-or-nothing across
every table — blocks every later dictionary edit, `document reassign` included.

**Intra-payload collisions are rejected, not staged.** Two rows in *one* submission that
derive the same key and disagree fail validation (pass 1) and roll the batch back, naming
the identity and the recovery path. A conflict whose "existing" side was inserted
milliseconds earlier in the same batch has no independent provenance to adjudicate
against; the overwhelmingly likely cause is a collection time the source did give and the
extraction dropped. Two *identical* rows in one payload stay benign (first inserts, second
reports `duplicate`) — that is an agent listing one fact twice. Genuine untimestampable
repeats go through two submissions plus `--keep both`, which keeps the human sign-off in
the loop rather than letting an agent self-admit near-duplicates.

Because siblings legitimately share a date, every same-date ordering is tie-broken by row
id (`query labs`, the summary/brief lab sections): the admitted row sorts as the later
point, so `trends` deltas and "latest value" stay deterministic.

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
parsed, **everything else** through `tesseract` (a soft dependency) — no image-suffix
allowlist, so `.jfif`, `.jpe` and extension-less scans OCR like any other image.

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

Extraction route feeds the owner check: the identity-anchor (`suspect`) verdict is applied
only to an agent transcription or a tesseract pass, never to natively-extracted text —
the whole `.txt/.md/.csv/.tsv/.json/.log/.docx/.xlsx` set. In a structured export
`Patient`/`DOB`/`MRN` are column labels and field keys, and counting them as an identity
header refuses ordinary lab exports as belonging to a stranger. The line is the *route*
rather than how prose-like the format is, because the route is what the extractor actually
knows; the cost is that a prose transcript saved as `.txt` and ingested with `--ocr auto`
loses the anchor check too. That is no worse than before native extraction existed (such a
file went to tesseract, which declined, so there was no text and no check either), and the
`--ocr-text-file` path keeps full coverage. `mismatch` — an affirmative name/DOB match on a
*different* roster person — is the half that actually prevents misfiling, and it blocks on
every route.

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
pemr ingest <file> --person <slug> [--ocr auto] [--force]        # --force: skip owner verification
pemr ingest <dir>  --person <slug> --study dicom [--allow-large] # a study folder as ONE document (§4)
pemr commit-extraction --document <id> --json <file>
pemr review-conflicts [--resolve <id> --keep existing|incoming|both [--note ...]] [--dictionary <toml>]
pemr document list [--person <slug>]                     # newest first; omit --person for everyone
pemr document show <id> [--json | --text]                # one document's detail; --text dumps stored ocr_text
pemr document edit <id> [--doc-date|--category|--provider ...]   # partial update; "" clears a field
pemr document reassign <id> --person <slug> [--apply]    # move a misfiled document + records; dry run by default
pemr document rm <id> [--apply] [--purge-blob] [--tombstone [--reason ...] [--note ...]]
                                                         # delete a document + records; dry run by default
                                                         # --tombstone: also refuse to re-ingest this content (§3)
pemr document tombstone list [--json]                    # recorded intentional removals, newest first
pemr document tombstone add (--file <path> | --sha256 <hex>) [--reason ...] [--note ...]
                                                         # pre-emptive exclusion; ingests and copies nothing
pemr document tombstone rm <sha256>                      # lift one (full hash only)
pemr document set-text <id> --ocr-text-file <path> [--force]     # attach/replace ocr_text after ingest; FTS follows via trigger
pemr query labs --person jane --test hba1c --since 2023-01-01
pemr query meds --person jane --active
pemr query timeline --person jane --since 2024-01-01     # merged event stream
pemr find --person jane "cholesterol"                    # full-text over ocr_text + records
pemr find "mmr booster"                                  # omit --person: whole-household, slug-prefixed hits
pemr trends --person jane --test hba1c                   # min/max/latest/slope
pemr due --person jane                                   # screening/vaccine gaps — NOT IMPLEMENTED (phase 7)
pemr render summary --person jane        > exports/jane-summary.md
pemr render brief --appointment <id>     > exports/brief.md
pemr render journal --person jane        > exports/jane-journal.md
pemr backup                                              # VACUUM INTO snapshot
pemr restore latest [--force]                            # install a snapshot back over pemr.db (§8)
pemr verify                                              # integrity + row counts + source-blob resolution
pemr migrate [--create]                                  # apply pending migrations (--create bootstraps a new DB)
pemr rekey [--apply]                                     # re-derive dedup keys after a dictionary edit
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
`trends`, `render_summary`, `render_brief`, `render_journal`. Write: `person_add`, `person_edit`,
`ingest` (`study="dicom"` + `allow_large` make `file` a study directory, §4), `commit_extraction`,
`document_set_text` (fills an empty `ocr_text` only — the `--force`
replace is CLI-only), `review_conflicts` (resolution gated on human sign-off). Each returns the
same `--json`-shaped payload as the CLI; the MCP server (`pemr/mcp_server.py`) parses args and
calls the same Python functions the CLI calls — one implementation, two front doors. `readOnlyHint`
annotations expose the read/write split to the client.

`AGENTS.md` documents this contract so any agent (Cowork, Claude Code, local) knows to
**call tools, not reinvent** — and specifically: never write to the DB except through
`commit_extraction`/`person_add`/`person_edit`/`ingest`/`document_set_text`; always `ingest` (with `ocr_text` populated) before
extracting; dictionary additions go through human review, never agent-direct edits.

---

## 6. Generated documents (DB → disposable output)

`render.py` produces your current deliverables as pure functions of DB state:

- **master summary** — active meds, conditions, allergies, latest vitals, recent
  abnormal labs, open follow-ups, open conflicts. One query bundle → Markdown. The
  conflicts section is not decoration: an open conflict means a stored value is disputed
  and its correction is still staged, so the summary would otherwise print the stale
  value silently (the brief carries the same section, but it is per-appointment).
- **appointment brief** — for a given upcoming appointment: relevant history for that
  specialty, recent labs/imaging, current meds, med-interaction flags, suggested
  questions. This is your "walk-in readiness" as a repeatable command.
- **journal** — chronological event stream (documents + appointments + procedures)
  rendered as a narrative timeline.

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
