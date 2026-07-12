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
layout, `migrations/001_init.sql`, `pemr migrate`, `pemr person add|list|show`, config, CI.
**Phase 2 (ingest + two-layer dedup) is done**: content-hash blob store + commit-extraction
(`pemr ingest`), semantic dedup keys with conflict staging (`migrations/002_conflict.sql`,
`pemr review-conflicts`), starter analyte/name dictionary.

## Data / privacy posture

Local-first. The live `pemr.db` sits on a **non-synced** local path (WAL sidecars corrupt
under cloud sync); only clean `VACUUM INTO` snapshots sync to Drive/OneDrive. Private-ish,
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
