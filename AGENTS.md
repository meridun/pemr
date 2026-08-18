# AGENTS.md — the pemr agent contract

`pemr` is a **local-first personal EMR engine**: the SQLite database is the single source of
truth, and every clinically meaningful transformation is a deterministic, pure-Python function of
DB state. An LLM agent drives the workflow (reads scans, proposes extractions, writes summaries),
but the agent is a *client* of the engine — it never edits the database directly and never
substitutes its judgment for the engine's deterministic rails.

This file is the binding contract for that agent layer. It governs how agents call the MCP wrapper
(`pemr/mcp_server.py`, run as `python -m pemr.mcp_server`). The MCP tools are a thin mirror of the
CLI verbs (`docs/Architecture.md` §5/§9): each parses args, calls the same engine function the CLI
calls, and returns the `--json`-shaped payload as-is.

## Client configuration

The server speaks stdio. Install the optional SDK (`pip install pemr[mcp]`) and point your MCP
client at it, resolving the DB the same way the CLI does — via env or `config.toml`:

```json
{
  "mcpServers": {
    "pemr": {
      "command": "python",
      "args": ["-m", "pemr.mcp_server"],
      "env": { "PEMR_DB": "/path/to/pemr.db" }
    }
  }
}
```

DB/config resolution is identical to the flagless CLI: `PEMR_DB` > `[paths].data_dir/pemr.db` in
`PEMR_CONFIG`/`./config.toml`. No path logic lives in the wrapper.

## Tool surface

Read-only tools (never mutate the DB; safe to call freely):

- `person_list` — roster.
- `person_show` — one person by slug.
- `query` — structured reads; `kind` = `labs` | `meds` | `timeline`.
- `find` — full-text search over `ocr_text` + record fields.
- `trends` — min/max/latest/slope for one analyte.
- `render_summary` — master summary Markdown for a person.
- `render_brief` — walk-in brief Markdown for one appointment.
- `render_journal` — narrative chronology Markdown for a person.
- `render_curation` — curation audit trail Markdown for a person: every recorded verdict that
  removed a row from the three documents above, grouped by ruling. Empty Markdown means the
  person has no verdicts, not a failure.

Write tools (mutate the DB; the only tools that do):

- `person_add` — add a person to the roster.
- `person_edit` — update a person's fields (partial; `slug` is not editable, pass `""` to
  clear a nullable field).
- `ingest` — hash + blob-store + layer-1 dedup a document. Returns `status: "tombstoned"` for
  content a human deliberately excluded, and `force` cannot override that (see §3).
- `commit_extraction` — validate + dedup + commit extracted rows for a document.
- `document_set_text` — attach a document's text (`ocr_text`) after ingest, when the ingest itself
  didn't carry it (see §3). Fills an empty `ocr_text` only; **replacing** an existing transcription
  is a human action at the CLI (`pemr document set-text <id> --ocr-text-file <path> --force`), so
  the tool takes no `force` and refuses a populated document.
- `review_conflicts` — lists conflicts read-only; **writes only when given a `resolve` id**, and
  then only with human sign-off (see below).

Read/write separation is also declared to the client via MCP `readOnlyHint` annotations.

**Deletion — and clinical verdicts, and unsourced writes — are never an MCP tool.** `document rm`,
`record rm` (the CLI's destructive verbs — the latter added by issue #107), `record annotate`
(issue #109), `record reaffirm` (issue #126), `record assert` (issue #110), and `record edit`
(issue #129) are deliberately
excluded from the tool surface
above. This is what keeps "an agent cannot delete PHI" true: removing a document or a single
record row is a human-at-a-terminal action only, never something an agent can reach through this
server. `record annotate` joins them for the adjacent reason — a `curation` verdict is a human's
clinical judgment, and writing one can drop a record out of every generated document without
deleting a row; `record reaffirm` writes those same verdicts in bulk, so it is excluded for the
same reason (an agent may still *read* the orphan list from `--json` and summarize it). `record assert` is the sharpest case of all — it is a *write*, not a deletion, and
the only path in the system that can put a fact into the record with no external source; only a
human at the CLI may vouch for one. `record edit` sits on the same trust boundary from the other
side: it mutates a *stored clinical value* with no new source backing the change, so the row stops
matching what its document says on one person's say-so — which is exactly the kind of write that
must carry a named human, not an agent.

The verbs above are CLI-only; verdict *content* is not. Since issue #131, `query`
(`kind` = `labs`/`meds`/`timeline`) carries each row's `curation` verdict on the MCP read
payload, same as `--json` — an agent reading `query` sees which rows a human has marked
`superseded`, `erroneous-in-source`, `merged-into`, or `disputed`, and why. That is unchanged
from the rule above: an agent may read a verdict, never write or clear one.

## MUST rules

### 1. Extraction naming

Agents MUST use **canonical analyte-dictionary names** in `commit_extraction` payloads, not the
report's verbatim label.

- For any analyte present in `data/dictionary.toml`, use the canonical token (e.g. `hba1c`, not
  "Hemoglobin A1c" or "A1c"). The deterministic dictionary + `dedup.py` normalization remain the
  backstop, not the only defense — getting the name right at the source keeps trends, `query`, and
  dedup coherent.
- **Unknown analyte** (no canonical match after normalization): commit the verbatim report name
  **including any parenthetical qualifier** and **propose the synonym to the human** in your
  response. Agents never edit `data/dictionary.toml` directly — dictionary additions are
  human-approved (`docs/Architecture.md` §3).
- **Never drop a parenthetical qualifier** (issue #71). `Albumin (SPEP)` and a CMP's `Albumin`
  are two different assays off one draw; the qualifier is what keeps their dedup keys distinct,
  and an agent that strips it at the source destroys a distinction no later `pemr rekey` can
  recover. Commit the label as printed. If the parenthetical really is noise for that analyte,
  that is a dictionary entry for the human to approve (`"m-spike (spep)" = "m_spike"`), not an
  edit for you to make in the payload.
- MUST NOT invent abbreviations or "helpful" renames.

### 2. Observation rows — vitals, orders, screenings, immunizations, functional

`render_summary` populates its **Orders & Referrals** and **Latest Vitals** sections purely from
`observation` rows, keyed by `obs_type`. An extraction that omits them leaves those sections
permanently `_none recorded_`, so a visit note carrying a vital reading or a non-medication order
MUST commit the matching `observation` rows in `commit_extraction`. (Conditions and allergies are
**no longer observations** — they are typed rows, see §3.)

- **Vital** → `obs_type='vital'`, `key=<canonical vital token>` (below), the reading in `value_num`
  (+ `unit`) or `value_text`. "Latest vitals" is the most recent `vital` row per normalized `key`.
- **Order** → `obs_type='order'`, `key=<item/order name>`, optional `value_text=<instructions
  and/or prescriber/target specialty>`; set `observed_at` to the order/referral date when the source
  gives one. This is the home for **non-medication orders** mined from the med-list section —
  durable medical equipment (e.g. a cervical collar), outpatient PT, referrals, and pre-op consults.
  A non-medication item surfacing in the med-list section MUST be committed as an `order`
  observation — never dropped (surviving only in `ocr_text`), and never miscommitted as a
  `medication` or `procedure` row. Unlike vitals, `key` is free-text: emit the verbatim item name,
  there is no canonical `order` vocabulary.

Canonical vital `key` tokens — emit these verbatim (same discipline as rule 1): `blood_pressure`,
`bmi`, `weight`, `height`, `temperature`, `pulse`, `spo2`, `respiratory_rate`. `dedup.norm()`
treats underscores as spaces, so `blood_pressure` matches "blood pressure"; the analyte dictionary
also maps common synonyms (`bp` → `blood_pressure`), but agents SHOULD emit the canonical token
directly. Set `observed_at` (ISO date) whenever the source gives one — it drives timeline order and
the "latest" selection.

Three more observation families have **no dedicated summary section** — they surface only via the
generic `observation` loop in the appointment brief and timeline. Commit them anyway: for the first
two the structured consumer is the future `pemr due` (care-gap) command and the last-done date is
what phase 7 needs; re-extraction to recover any of them later is expensive:

- **Screening** → `obs_type='screening'`, `key=<snake_case screening name>` (e.g. `mammogram`,
  `colonoscopy`, `diabetes_screening`), `observed_at=<last-done ISO date>` (the load-bearing
  field), optional `value_text=<the source table's stated frequency / next-due, verbatim>`. The
  frequency/next-due is context only — `pemr due` recomputes "due" from its own cadence rules.
- **Immunization** → `obs_type='immunization'`, `key=<snake_case vaccine name>` (e.g. `influenza`,
  `tdap`, `mmr`, `pneumococcal`), `observed_at=<date administered>`, optional `value_text=<detail:
  dose #, lot, site>`. One row per administration; multiple rows over time = vaccination history.

- **Functional** → `obs_type='functional'`, `key=<snake_case IADL / self-management token>` (e.g.
  `financial_self_management`, `meal_regularity`, `medication_self_administration`, `iadl_bathing`),
  `observed_at=<ISO date the fact was observed>`, optional `value_num` (an ordinal/scale rating) or
  `value_text` (the observed fact in words). This is the home for a caregiver-observed fact about
  daily function — the class of evidence that is not a vital sign, not an order and not a diagnosis
  (issue #132).
  - `observed_at` is **required** here, unlike every other observation family: a functional
    observation's entire value is being dated (a decline is a date, not a state), and an undated one
    is the unqueryable prose this family exists to replace. A commit without it is **rejected**.
  - Attribution: set `document_id` implicitly by committing against the source document when one
    states the fact (e.g. a transcribed ledger or letter). A fact known only from family knowledge
    with no document goes through `pemr record assert` (CLI-only, never an MCP tool — see the tool
    surface above; issue #110) — **never** invent a document to hang it on.
  - **MUST NOT imply a diagnosis.** A `functional` row records an observed fact ("stopped balancing
    the register in July"), never a coded finding. Never promote one to a `condition` row and never
    emit a paired `condition` because the pattern suggests one — no diagnostic code without
    clinician documentation. The inverse rule lives in §3: conditions are typed rows, not
    observations.
  - `key` is lightly controlled: emit sensible `snake_case` tokens, no approval gate (same
    discipline as screening/vaccine tokens). One row per observation date — a series of rows on the
    same `key` over time *is* the trend.

Vaccine overlap (influenza appears in both worlds): an **administered** vaccine → an `immunization`
row; a health-screening-table *"due / last-done"* line → a `screening` row **only when the table is
the sole evidence of last-done** (no administration record to capture). Don't double-count the same
event as both. Canonical screening/vaccine vocabulary is owned by the care-gap phase — for now emit
sensible `snake_case` tokens (same discipline as vitals) and let `dedup.norm()` handle case/spacing.

### 3. Allergy and condition rows (typed, not observations)

`allergy` and `condition` are **first-class record types** (migration 006), not `obs_type` values —
the generic `observation` shape has one date and no criticality, so it could not hold a resolved
problem or a machine-readable allergy severity. `render_summary` feeds its **Active Problems**,
**Past Medical History**, **Family History** and **Allergies** sections from them, so a visit note
carrying a diagnosis or an allergy MUST commit these rows:

- **Allergy** → `allergy` with `substance=<allergen>` (required), optional `reaction`,
  `criticality` ∈ {`high`, `low`, `unable-to-assess`}, and `noted_on` (ISO date). Emit
  `criticality` whenever the source states one — it is what sorts a dangerous allergen to the top
  of the list, and free-text severity buried in `reaction` cannot do that.
- **Condition** → `condition` with `name=<condition name>` and `status` (both required), optional
  `onset_on` / `resolved_on` (ISO dates), `relation`, and `note=<free-text detail>`.
  `status` is a closed vocabulary — a value outside it is rejected:
  - `active` — a current problem-list entry;
  - `resolved` — a past problem with an end (set `resolved_on` when the source gives one);
  - `history` — a past-medical-history line with no stated resolution date;
  - `family-history` — a **relative's** diagnosis.
- A family-history item MUST carry `status='family-history'` and the relative in `relation`
  (`mother`, `father`, `sibling`, ... free text) — **never** an `active` condition row. The subject
  is part of the dedup key, so this is what keeps a mother's diabetes out of the patient's own
  problem list; miscommitting it there is a clinical-safety error, not a cosmetic one.
- Both are **standing facts**: their dedup keys are date-free, so restating the same allergen or
  problem across documents collapses to one row. A stated disagreement (`penicillin: rash` vs
  `penicillin: anaphylaxis`, or `active` -> `resolved`) stages a **conflict** for human
  adjudication (§5); a field the new document simply doesn't mention is silence, not a change, and
  never conflicts. Do not "helpfully" restate a value the source omitted.
- Silence only reads that way in one direction. A field the new document **does** state over a
  stored NULL is new information with nothing to adjudicate: it fills the stored row in place and
  the commit reports it as `enriched` rather than `duplicate`. So emit every field the source
  states even for an allergen or problem you know is already on file — a terse first document
  followed by a detailed one is the normal case, and this is what makes the detail land.

### 4. OCR text at ingest

Every `ingest` MUST end with `document.ocr_text` populated. This is what makes a document visible
to `find` (FTS5); an ingest without it is silently unsearchable.

- **Default path: agent-supplied transcription.** You already read the document to extract from it;
  pass that text as the `ocr_text` tool param (CLI: `--ocr-text-file <path>`). A vision transcript
  beats tesseract on messy scans.
- **Fallback:** `ocr=true` (CLI: `--ocr auto`) only when you cannot read the file type yourself.
  It extracts by whatever route the type allows — plaintext/`.csv`/`.json` read directly,
  `.docx`/`.xlsx` parsed from their OOXML, a CCDA `.xml` (a portal "download my record"
  export) rendered from its section narrative, a saved `.html`/`.htm` page parsed with the
  stdlib HTML parser (tags stripped, scripts and styles dropped), everything else (images,
  unknown suffixes)
  through tesseract. A `.pdf` is read page by page — embedded text layer where there is one, a
  300-dpi render OCR'd where there isn't (first 20 pages, joined by `\f`); that route needs the
  optional `pip install pemr[ocr]` extra, and without it a PDF stores no text and says so on
  stderr. `.rtf`, `.msg` and `.doc` have no route at all: you get a stderr note, and must
  transcribe those yourself. Extraction is also capped at 32 MiB per file.
- **Pointer stubs are refused.** A `.gsheet`/`.gdoc` from a synced Drive folder is a ~1 KB JSON
  link, not the document; `ingest` fails pre-write. Export it from Drive and ingest the export.
- Self-check: the `ingest` response includes `ocr_text_populated: bool`. If it is `false`, treat
  the ingest as incomplete and supply text before moving on. (The engine only *warns* here rather
  than hard-failing, because a human at the CLI may legitimately defer — but the agent MUST not.)
- **Remediation:** call `document_set_text(document_id, text)`. Re-ingesting will not work — the
  layer-1 content hash matches, so `ingest` returns the existing document and writes nothing.

**Study directories** (`ingest(file=<dir>, study="dicom")` — a burned imaging disc, ingested as one
document). The engine seeds `ocr_text` with a derived summary (modality, study date, per-series
slice counts), so `ocr_text_populated` is already true and the study is findable. That is a floor,
not the contract met: if the disc or the portal carries the **radiology report**, transcribe it and
pass it as `ocr_text` — it replaces the summary and is the only text that carries findings. The
report often ships as a PDF beside the slices; it is not packed into the study (the engine names
what it dropped), so ingest it as its own document too. Owner verification below applies to
studies as well, but reads the disc's own `PatientName`/`PatientBirthDate` header tags — *not*
the derived summary, which is engine output and names nobody. Those tags are never stored, so
they will not appear in `document_show` text or `find` hits.

**Owner verification.** Because you supply the text, you are the primary consumer of the
ingest-time owner check: it scans that text for the claimed person's name/DOB and returns
`owner_check: {verdict, matched_slug, evidence}` — `match`, `mismatch` (the text names a
*different* roster person), `suspect` (a patient-identity header naming nobody on the roster), or
`unverified` (no text, no identity anchor in it, or a claimed person whose name is too short to
carry a signal — their absence from the text is ignorance, not evidence). `mismatch`/`suspect`
**refuse the ingest** before anything is written. `suspect` is scoped by **route**, and the split
is *structured vs prose*, not native vs OCR: it applies to the text you supply, to a tesseract
pass, and to a natively-extracted `.html`/`.htm` page (route `native-prose` — a saved portal page
is a printed page, so its `Patient:` header is a real claim). It never applies to the
**structured** native routes — the whole `.txt`/`.md`/`.csv`/`.tsv`/`.json`/`.log`/`.docx`/`.xlsx`
set, CCDA `.xml` included — because in a
structured export `Patient`/`DOB`/`MRN` are column labels rather than an identity header. The
route is the line, not how prose-like the format is: a transcript you save as `.txt` and ingest
with `--ocr auto` gets no identity-header check either, so pass your transcription as `ocr_text` /
`--ocr-text-file` (the default path above) and keep the check. `mismatch` holds on every route.

- On a refusal, the agent MUST surface the verdict and the `evidence` snippet to the human and get
  an **explicit go-ahead** before retrying with `force=true`. Never force on your own judgment.
- Unlike §5 conflict resolution, this is not mechanically gated on a `signoff` param. The
  asymmetry is deliberate: a wrongly-forced ingest is recoverable (`pemr document reassign`),
  whereas a wrongly-resolved conflict destroys the losing value.

**Tombstones — never force past one.** Content whose hash carries a `document_tombstone` row
comes back as `status: "tombstoned"` with `document: null` and the `tombstone` row (reason,
removal date, note). That is not an error and not a failure of your call: a human recorded that
this content is deliberately kept out of the store, and nothing was written. Report the skip and
its reason to the human and **stop**. `force=true` does *not* override a tombstone — the tool
refuses it. That is deliberately stricter than owner verification above: the owner check is a
heuristic a human may reasonably ask you to override, whereas a tombstone *is* the human's
already-recorded decision, so overriding it is never an agent judgment call. If they want the
document filed after all, they lift it themselves at the CLI
(`pemr document tombstone rm <sha256>`). No MCP tool writes or lifts tombstones.

### 5. Conflict discipline

Agents never resolve staged conflicts silently.

- `review_conflicts` with no `resolve` id **lists** conflicts — call it freely.
- **Resolution requires explicit human sign-off.** The human must have named the specific conflict
  and the chosen resolution (`keep existing` / `keep incoming` / `keep both` / `keep merge`) in the
  current session. "Clean this up", silence, or a standing general instruction is **not** sign-off.
- Mechanically: `review_conflicts(resolve=…)` requires a non-empty `signoff` param quoting the
  human's instruction verbatim; the wrapper refuses the write otherwise and stores the sign-off
  text with the resolution.
- `keep both` is the odd one out: it **admits a row** rather than choosing between two. Use it only
  for a genuine repeat the source cannot distinguish — two same-day draws on a report that prints
  no collection times (see §7). It inserts the incoming row alongside the stored one as the next
  *occurrence* of that identity; a later re-commit of that same payload then dedups against it. If
  the source *did* give distinct times and the extraction dropped them, the fix is a corrected
  extraction, not `keep both`.
- The listing reports `occurrences` — how many rows already sit under that identity. More than one
  means a repeat was admitted before, so check whether the incoming row is a re-read of one of them
  before proposing anything.
- `keep incoming` overwrites the stored row the conflict is anchored to. If every row under that
  identity was removed in the meantime (the source document was deleted or reassigned), it is
  **refused** — never silently applied to nothing. Report the refusal to the human; `keep both`
  admits the staged row as a fresh record if they want the value kept.
- On the standing-fact types (§3) `keep incoming` overwrites only the fields the incoming row
  actually states — an unstated field there means "this document didn't say", so adjudicating one
  disagreement (`criticality: high` vs `low`) does not also erase a `reaction` the incoming
  document simply didn't repeat. On the dated types an unstated field IS a clearing and is written
  as one.
- `keep merge` is the field-level resolution for exactly that dated-type case: a document that
  **refines** some fields and is silent about others (a portal export restating a medication with a
  better sig and no prescriber). It takes what the incoming row states over a stored NULL, keeps
  what it leaves unstated, and does **not** guess where both rows state different values — it
  refuses the whole resolution and names the colliding fields. Report that refusal to the human and
  ask which side wins per field; `fields={"<field>": "existing"|"incoming"}` (CLI: `--field
  NAME=existing|incoming`) settles one, and only a field that genuinely collides may appear there.
  Provenance moves the same way `keep incoming`'s does — the row takes the winning document's
  `document_id`, which supersedes an existing attestation on that row (issue #110) even when merge
  takes no field, because the row is now filed under a different document.
  On a standing-fact type merge always refuses, because a conflict there is present-and-different by
  construction — use `keep incoming` for those.
- `commit_extraction` **rejects** two rows of one submission that derive the same key and disagree;
  that is an extraction error, not a conflict. For `observation`, re-read the source for times; if
  there genuinely are none, submit them separately and ask the human about `keep both`. For
  `lab_result` the key holds only the collection **date** (issue #117), so a time cannot separate
  two same-day draws — submit them separately and ask the human about `keep both`. The rejection
  message names the applicable path.

### 6. Medication-interaction section

`render_brief` emits a placeholder **"Medication Interaction Review"** section for the agent layer
to fill. The engine deliberately does not compute this: an external drug-interaction API would send
the med list off-machine (violating the local-first posture), and a rules DB inside the engine was
rejected in phase 4 as not a pure function of DB state.

The agent fills it **from its own general knowledge**, under fixed framing it MUST include verbatim:

- A header stating the section is AI-generated from general knowledge, **not** a
  drug-interaction database, and must be verified with a pharmacist or prescriber.
- The exact current-med snapshot considered (so a human can spot staleness).
- Any "no flags raised" statement accompanied by "this is not a clearance."

The agent MUST NEVER: claim safety or the absence of interactions, give dosing advice, or recommend
starting, stopping, or changing a medication.

### 7. Date precision

Every date field (`collected_at`, `started_on`, `ended_on`, `performed_on`, `scheduled_for`,
`observed_at`, `noted_on`, `onset_on`, `resolved_on`) accepts an ISO prefix at **three precisions**: full `YYYY-MM-DD` (optionally + a
`T`/space time), month `YYYY-MM`, or year `YYYY`. Emit the **most precise prefix the source
supports** — a full date when the document gives one, else `YYYY-MM`, else `YYYY` — never invent a
day or month the source didn't state, and never fall back to stashing an imprecise date elsewhere.

- e.g. a prior surgery cited only as "03/2019" commits as `procedure.performed_on = "2019-03"`,
  not stashed in a `condition.note`.
- A time component is only valid with a full date (`2026-03T09:00` is rejected).
- Non-ISO forms (`06/15/2026`, `2026-13`, `2026-3`, `Jan 2026`) are still rejected — reformat to an
  ISO prefix first.
- A partial and a later full date of the same event stay **distinct rows** (the dedup layer never
  guesses that one refines the other); reconciling them is a human conflict-review action.
- Date-only precision is also why two genuine same-day results collide: they derive one key, so the
  second stages a conflict. Emitting a time you invented is never the fix — the recovery path is
  `keep both` under human sign-off (§5).

### 8. Medication discontinue reason

A med-list status cell often states **why** a drug stopped, in the same cell as the status word:
`Discontinued (Reorder)`, `Discontinued (Therapy Completed)`, `Discontinued (Patient Stopped
Taking)`, `Discontinued (Substitution/Alternate Therapy Placed)`. The two halves go to two fields.

- The **lifecycle word** goes in `medication.status` (`discontinued`), exactly as today.
- The **parenthetical** goes in `medication.status_reason`, **verbatim, parentheses stripped**
  (`Reorder`, `Therapy Completed`, ...). Preserve the source's own casing and spacing.
- NEVER concatenate the two into `status` (`discontinued (reorder)`, `discontinued-reorder`) —
  `status` stays a lifecycle word, and the reason is the read layer's own axis.
- OMIT `status_reason` when the source states no reason. Never invent one and never write `none` /
  `n/a` — a bare `Discontinued` row is absent-reason, and behaves exactly as it always has.
- The field is **free text**, not a closed vocabulary: a reason this list doesn't name (`Never
  Started`, `Provider Discontinued`, ...) is emitted **as stated** rather than forced into a
  familiar one.

Why it is worth a field of its own: `(Reorder)` and `(Therapy Completed)` are **opposites**. A
reorder means the prescription was renewed and therapy continues, so its `ended_on` is the end of an
authorization period; a therapy-completed row is a genuine end. The read layer treats only a
*renewal* reason as non-terminal (`query.RENEWAL_MED_REASONS`) and every other reason — recognized
or not — as ending the course, so an unfamiliar reason degrades safely rather than silently
resurrecting a stopped drug.

Re-ingesting the same medication with a *different* `status_reason` stages a **conflict** (§5), like
a differing `status` does — it is a real disagreement between documents, not noise to be smoothed.

**Rows committed before migration 014 have `status_reason IS NULL`** — there is no automated
backfill, because matching an `ocr_text` table line back to an already-committed row is a name/date
heuristic over PHI and belongs to a human-in-the-loop curation pass, not a migration. The reason is
not lost, though: it still sits verbatim in `document.ocr_text` for any already-ingested CCDA, and is
recoverable per row with `pemr record edit medication <id> --set status_reason="Reorder"` (this does
not move `dedup_key` — §5's identity guarantee holds for this field like any other editable one).

## Privacy posture

This repository is **public**. It is framework + documentation only.

- **No PHI anywhere in the repo** — no real names, DOBs, values, encounter dates, or personal-name
  filenames in issues, commits, logs, PRs, or test fixtures. Fixtures are synthetic only.
- **A real identifier reaches GitHub more than once.** The SDLC lanes quote diffs, repro steps and
  issue text back into issue comments, so one real name in a fixture or a repro write-up gets
  republished across every downstream thread. Substitute the persona *before* it is written down,
  not after — a later scrub cannot reach `refs/pull/*`, which GitHub keeps permanently.
- **The roster is the allowlist.** `SYNTHETIC_ROSTER` in `scripts/pii-scan.mjs` is the complete set
  of identities permitted in this repo. Need another persona? Add it there — that edit is the
  review point.
- **Describe the shape, never the value.** Writing *about* a leak is how one keeps spreading: a
  fix's own commit message, PR body, and test fixtures are as public as the code. Say "a person
  slug whose surname is not a placeholder", never the slug itself. The one place this is
  unenforceable is `scripts/pii-scan.mjs` and its test, which the scanner must exempt — every
  example identity there has to be invented.
- **Every publishing surface is gated** (`npm run setup:hooks` installs the git ones; `npm install`
  does it automatically):

  | surface | gate |
  |---|---|
  | tracked files | `npm run check:pii`, in CI |
  | commit message | `.githooks/commit-msg` |
  | push (messages + introduced lines) | `.githooks/pre-push` |
  | PR title and body | `pii-pr-text` CI job |
  | agent shell commands | `.claude/hooks/pii-guard.py` |
  | SDLC lane comments | `guardOutboundBody` in `scripts/sdlc.mjs` |

  All of them shell out to `scripts/pii-scan.mjs`, so there is one set of patterns rather than six
  that drift. Bypass is `--no-verify` and should be rare enough to notice.
- MCP responses stay on the local machine. Agents MUST NOT relay record contents into any remote
  channel (issue comments, PRs, external APIs) beyond what the human explicitly asks for.
- The live database and blobs stay out of git: `pemr.db` (and `*-wal`/`*-shm`), `sources/`,
  `exports/`, `inbox/`, `backups/`, and `config.toml` are all `.gitignore`d.
