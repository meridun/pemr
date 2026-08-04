# Overview

**PEMR** (Personal EMR) is a local-first, family-scale medical record system. Original
documents (scans, PDFs) are retained as-is in a content-addressed blob store; a **SQLite
database is the source of truth** for structured data. Deterministic work — ingest,
dedup, query, and document generation — lives in a **Python CLI engine** (`pemr <cmd>`)
wrapped later by a thin MCP server, so AI agents call typed tools instead of re-deriving
logic each request.

Who it's for: one household. One DB, `person_id` on every row — adding a family member
is `pemr person add`, nothing else.

## Main subsystems

- **Engine** (`pemr/` Python package) — `cli.py` entry point, `db.py`
  (connection/pragmas/migrations), `models.py` typed rows; later `ingest.py`, `dedup.py`,
  `query.py`, `render.py`, `backup.py`, `mcp_server.py`.
- **Schema** (`migrations/*.sql`) — hybrid: typed tables (`person`, `document`,
  `lab_result`, `medication`, `procedure`, `appointment`, `condition`, `allergy`) plus a
  generic `observation` catch-all. See [Architecture.md §2](Architecture.md#2-schema-hybrid).
- **Dedup** — two layers: content-hash on documents, deterministic semantic keys on
  extracted rows. The core determinism win: [Architecture.md §3](Architecture.md#3-dedup-algorithm-the-core-determinism-win).
- **Data layout** — the live `pemr.db` stays on a local-only (non-synced) path; only
  `VACUUM INTO` snapshots and immutable source blobs go to the cloud-synced folder.
  Paths are configured in `config.toml` (see `config.example.toml`).

## No data in this repo

This repository is **framework + documentation only**. The live database, scans,
exports, and backups all live outside the tree; `.gitignore` hard-blocks common data
formats as a backstop.

## Where to go next

- [Architecture.md](Architecture.md) — the locked design doc (decisions, schema, phases)
- [Development_AgenticSDLC.md](Development_AgenticSDLC.md) — the issue-driven pipeline this
  repo is built with
- [Documentation.md](Documentation.md) — L3 doc governance
