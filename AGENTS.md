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

Write tools (mutate the DB; the only tools that do):

- `person_add` — add a person to the roster.
- `person_edit` — update a person's fields (partial; `slug` is not editable, pass `""` to
  clear a nullable field).
- `ingest` — hash + blob-store + layer-1 dedup a document.
- `commit_extraction` — validate + dedup + commit extracted rows for a document.
- `review_conflicts` — lists conflicts read-only; **writes only when given a `resolve` id**, and
  then only with human sign-off (see below).

Read/write separation is also declared to the client via MCP `readOnlyHint` annotations.

## MUST rules

### 1. Extraction naming

Agents MUST use **canonical analyte-dictionary names** in `commit_extraction` payloads, not the
report's verbatim label.

- For any analyte present in `data/dictionary.toml`, use the canonical token (e.g. `hba1c`, not
  "Hemoglobin A1c" or "A1c"). The deterministic dictionary + `dedup.py` normalization remain the
  backstop, not the only defense — getting the name right at the source keeps trends, `query`, and
  dedup coherent.
- **Unknown analyte** (no canonical match after normalization): commit the verbatim report name
  (parentheticals stripped) and **propose the synonym to the human** in your response. Agents
  never edit `data/dictionary.toml` directly — dictionary additions are human-approved
  (`docs/Architecture.md` §3).
- MUST NOT invent abbreviations or "helpful" renames.

### 2. Observation rows — conditions, allergies, vitals

`render_summary` populates its **Conditions**, **Allergies**, and **Latest Vitals** sections purely
from `observation` rows, keyed by `obs_type`. An extraction that omits them leaves those sections
permanently `_none recorded_`, so a visit note carrying a diagnosis, allergy, or vital reading MUST
commit the matching `observation` rows in `commit_extraction`:

- **Condition** → `obs_type='condition'`, `key=<condition name>`, optional `value_text=<detail>`.
- **Allergy** → `obs_type='allergy'`, `key=<allergen>`, optional `value_text=<reaction>`.
- **Vital** → `obs_type='vital'`, `key=<canonical vital token>` (below), the reading in `value_num`
  (+ `unit`) or `value_text`. "Latest vitals" is the most recent `vital` row per normalized `key`.

Canonical vital `key` tokens — emit these verbatim (same discipline as rule 1): `blood_pressure`,
`bmi`, `weight`, `height`, `temperature`, `pulse`, `spo2`, `respiratory_rate`. `dedup.norm()`
treats underscores as spaces, so `blood_pressure` matches "blood pressure"; the analyte dictionary
also maps common synonyms (`bp` → `blood_pressure`), but agents SHOULD emit the canonical token
directly. Set `observed_at` (ISO date) whenever the source gives one — it drives timeline order and
the "latest" selection.

### 3. OCR text at ingest

Every `ingest` MUST end with `document.ocr_text` populated. This is what makes a document visible
to `find` (FTS5); an ingest without it is silently unsearchable.

- **Default path: agent-supplied transcription.** You already read the document to extract from it;
  pass that text as the `ocr_text` tool param (CLI: `--ocr-text-file <path>`). A vision transcript
  beats tesseract on messy scans.
- **Fallback:** `ocr=true` (tesseract) only when you cannot read the file type yourself.
- Self-check: the `ingest` response includes `ocr_text_populated: bool`. If it is `false`, treat
  the ingest as incomplete and supply text before moving on. (The engine only *warns* here rather
  than hard-failing, because a human at the CLI may legitimately defer — but the agent MUST not.)

### 4. Conflict discipline

Agents never resolve staged conflicts silently.

- `review_conflicts` with no `resolve` id **lists** conflicts — call it freely.
- **Resolution requires explicit human sign-off.** The human must have named the specific conflict
  and the chosen resolution (`keep existing` / `keep incoming`) in the current session. "Clean this
  up", silence, or a standing general instruction is **not** sign-off.
- Mechanically: `review_conflicts(resolve=…)` requires a non-empty `signoff` param quoting the
  human's instruction verbatim; the wrapper refuses the write otherwise and stores the sign-off
  text with the resolution.

### 5. Medication-interaction section

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

## Privacy posture

This repository is **public**. It is framework + documentation only.

- **No PHI anywhere in the repo** — no real names, DOBs, values, encounter dates, or personal-name
  filenames in issues, commits, logs, PRs, or test fixtures. Fixtures are synthetic only.
- MCP responses stay on the local machine. Agents MUST NOT relay record contents into any remote
  channel (issue comments, PRs, external APIs) beyond what the human explicitly asks for.
- The live database and blobs stay out of git: `pemr.db` (and `*-wal`/`*-shm`), `sources/`,
  `exports/`, `inbox/`, `backups/`, and `config.toml` are all `.gitignore`d.
