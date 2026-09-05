# pemr

**Personal EMR** — a local-first, family-scale medical record framework. Source documents
(scanned labs, visit notes, etc.) are retained as-is; a **SQLite database is the source of
truth** for structured data. Deterministic work — ingest, deduplication, query, analysis,
and brief-generation — lives in a **Python CLI engine** wrapped by a **thin MCP server**, so
AI agents call typed tools instead of re-inventing the logic on every request.

> ⚠️ **This repository is framework + documentation only. No personal or medical data lives
> here.** The live database, source scans, generated exports, and backups all reside outside
> the repo in a local data directory. `.gitignore` hard-blocks databases, documents, and the
> `data/ inbox/ sources/ exports/ backups/` dirs as a backstop.

## What it does

- **Ingest without duplication** — content-hash on source files (catches re-scans) plus
  semantic dedup keys on extracted rows (same clinical fact from two documents → one row).
- **Query fast** — canned + ad-hoc reads over a typed schema (labs, meds, procedures,
  appointments) with a generic `observations` catch-all for the long tail.
- **Generate on demand** — master health summary, per-appointment "walk-in readiness"
  briefs, and a chronological journal, all rendered from the DB so they never drift.
- **Extend to the whole family** — one DB, `person_id` on every row; adding a member is one
  command, not a fork of the tooling.

Full design — schema, dedup algorithm, ingest pipeline, tool surface, backup — in
[docs/Architecture.md](docs/Architecture.md).

## Design decisions

| Area | Choice |
|---|---|
| Structured store | SQLite (source of truth); source scans retained on disk |
| Schema | Hybrid — typed tables + generic `observations` |
| Multi-person | Single DB, `person_id` everywhere |
| Generated docs | Rendered views from the DB (disposable) |
| Ingestion | Agent does vision→structure; tools validate + dedup + commit |
| Interface | Python CLI engine + thin MCP wrapper |
| Dedup | Content-hash (documents) + semantic keys (rows) |
| Backup | `VACUUM INTO` snapshot → cloud-synced folder; live DB stays local |

## Status

Design is locked; build proceeds in phases (skeleton → ingest/dedup → query → render → MCP →
backup → care-gap rules) per the Architecture doc. **Phase 1 (skeleton) is done**: package
layout, `migrations/001_init.sql`, `pemr migrate --create` (bootstrap a new archive; plain
`pemr migrate` applies migrations to an existing one and will never create a database),
`pemr person add|list|show`, config, CI.
**Phase 2 (ingest + two-layer dedup) is done**: content-hash blob store + commit-extraction
(`pemr ingest`), semantic dedup keys with conflict staging (`migrations/002_conflict.sql`,
`pemr review-conflicts`), starter analyte/name dictionary. **Phase 3 (query layer) is done**:
structured reads (`pemr query labs|meds|timeline`), full-text search over OCR text + record
fields (`migrations/003_fts.sql`, `pemr find`), and `pemr trends` over lab analytes and vital
signs alike (a key present in both is refused, not merged) — all with `--json`.

## Data / privacy posture

Local-first. The live `pemr.db` sits on a **non-synced** local path (WAL sidecars corrupt
under cloud sync); only clean `VACUUM INTO` snapshots sync to Drive/OneDrive. Going the
other way is `pemr restore latest` — it validates the snapshot, banks a rescue copy of
whatever it replaces, clears stale WAL sidecars, migrates forward, and reports row counts
plus source-blob resolution (`pemr verify` runs that report on its own). What backups do
*not* cover — same-day loss, and pinning a snapshot against rotation — is spelled out in
[Architecture §8](docs/Architecture.md#8-backup--safety). Private-ish,
not encrypted-at-rest by default — an encrypted-snapshot upgrade is a drop-in later. No
HIPAA/PHI compliance layer and no provider interoperability; this is a personal archive, and
it assists appointment prep and research — it does not give clinical advice.

---

## Framework scaffolding (from the template)

This repo was generated from
[meridun/model-repo](https://github.com/meridun/model-repo) and carries its
documentation-tier system, token-optimizer hooks, role-based model routing, and the agentic
SDLC pipeline. See [docs/Documentation.md](docs/Documentation.md) and
[docs/Development_AgenticSDLC.md](docs/Development_AgenticSDLC.md). The skill/agent
prefix has been renamed from the template default to `pemr-`.

## Shared config

Adoption record for [meridun/model-repo](https://github.com/meridun/model-repo) components (see
`.github/skills/pemr-upstream-sync/SKILL.md`). Last pull: model-repo **72ceda9** (2026-09-05).
Declined rows are deliberate and revisitable.

| Component | Status | Pin | Notes |
|---|---|---|---|
| Doc-tier system (L1/L2/L3) | adopted | 72ceda9 | prefix `pemr-`; L1 local trims: no `## Tone`, no template placeholder prose, graphify section is a two-line pointer, Token wrappers describes the shell-wrapper mode only |
| Config sync + meta-drift guard | adopted | 72ceda9 | local: `pemr-wt` worktree prefix allowlisted in `check-meta-drift.mjs` |
| Caveman mode hook | adopted | 72ceda9 | L1 canonical; hook drift-checked |
| graphify nudge hook + vtk notes | adopted | 72ceda9 (vtk fc4b1a5) | vtk in transparent-wrapper mode |
| Role-based model routing | adopted | pilotfish v1.1.2 via 72ceda9 | pin in `docs/Development_ModelRouting.md` |
| Agentic SDLC pipeline | partial | 72ceda9 | core + `gh-issue` binding + CLI adopted; `sdlc/tools/` lint ratchet **declined** (ESLint-only, Python repo); ADO bindings declined; deviations in `sdlc/PROFILE.md` |
| Upstream sync procedure | adopted | 72ceda9 | `pemr-upstream-sync` |
