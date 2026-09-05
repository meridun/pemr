# SDLC profile: pemr

**The one file adoption fills.** Every `<KEY>` a prompt in this tree names resolves to a row
below. The core (`README.md`, `lanes/`, `dispatch.md`) and the `gh-issue` binding are upstream
verbatim and are never edited here; every project-specific decision is a row in this file or a
declared deviation at the bottom.

> **Upstream pin:** `meridun/model-repo` **72ceda9** (2026-09-05; agentic-sdlc `34b769e`). To
> re-sync, diff model-repo's `sdlc/`, `test/sdlc.test.mjs`, `.github/agents/sdlc-worker.agent.md`
> against ours, then bump this pin and the one in `docs/Development_AgenticSDLC.md`. Local
> adaptations to preserve: listed under **Known deviations from spec** below.

## Keys

Required:

| Key | Value | Meaning |
|---|---|---|
| `BINDING` | `gh-issue` | `bindings/gh-issue/BINDING.md` |
| `SDLC_CLI` | `node sdlc/bindings/gh-issue/sdlc.mjs` (`npm run sdlc <cmd>`) | the deterministic core (`sdlc …` in any prompt means this) |
| `SPEC_VERSION` | `34b769e` | agentic-sdlc sha this profile tracks, via model-repo `72ceda9` |
| `PROJECT` | pemr | |
| `REPO_PATH` | `C:\Claude\pemr` | |
| `WORKER_AGENT` | `sdlc-worker` | `.github/agents/sdlc-worker.agent.md` (no Agent tool); dispatcher sets `model` explicitly |
| `DEFAULT_BRANCH` | `dev` | integration branch — PRs target it, branches cut from it |
| `PROD_BRANCH` | `main` | prod — off-limits to all workers |
| `WORKTREE_ROOT` | `C:\Claude` | worktrees are `C:\Claude\pemr-wt-<issue#>` (sibling of the checkout, not nested) |
| `BUILD_CMD` | n/a | interpreted Python; `pip install -e .` is the only setup |
| `TEST_CMD` | `pytest <targeted paths>` | |
| `FULL_SUITE_CMD` | `pytest` | full suite over `tests/` |
| `LINT_CMD` | **unbound** | no lint/type gate declared; declare before enabling the scheduled dispatcher |
| `SMOKE_CMD` | **unbound** | declare before enabling the scheduled dispatcher (`pemr verify` on a scratch DB is the candidate) |
| `LANG_CONVENTIONS` | follow existing patterns; `.github/copilot-instructions.md` | |
| `INVARIANTS` | `docs/Architecture.md`; **public repo — no real identities** (`AGENTS.md` §"Privacy posture", `npm run check:pii`) | acceptance criteria on every change |
| `DECISION_RECORD` | in-issue `decision:` one-liner comment | |
| `DOCS_SINKS` | `docs/` (L3), `.github/skills/` (L2), `.github/copilot-instructions.md` (L1) | |

Optional — an unbound key means the lane step it gates is **skipped, not improvised**:

| Key | Value | Meaning |
|---|---|---|
| `DESIGN_ARTIFACTS` | unbound | spec track only; UX track off until UI-facing work appears (engine is CLI/SQLite) |
| `KNOWN_ENV_LIMITS` | Windows 11 / PowerShell host; no display server assumptions beyond CLI | |
| `DEP_AUDIT_CMD` | **unbound** | `pip-audit` is not installed; bind it once it is |
| `MIGRATIONS_DIR` | `migrations/` | numbered forward-only `.sql` files, tracked in `schema_migrations` |
| `MIGRATE_UP_CMD` | `pemr migrate` against a scratch DB (`--db <tmp>`) | forward only |
| `MIGRATE_DOWN_CMD` | unbound | migrations are forward-only by design; the roll-back check is skipped |
| `SCHEMA_DUMP` | unbound | no committed schema dump |
| `DOCS_ROOT` | `docs/` | |
| `DOC_DOMAINS` | `Architecture`, `Development` | |
| `TOKEN_TOOL` | `vtk`, transparent-wrapper mode | call `git`/`gh`/`npm` bare; see L1 `## Token wrappers` |

## Variation points (`docs/Development_SdlcComposability.md`)

- **Spine:** `intake → design → queued → build → verify → audit → ship`, collapsed tail (human
  PR merge is the `ready` gate).
- **VP1 tracker:** GitHub issues per the binding; labels created 2026-07-11. Blocking is native
  issue dependencies (`sdlc deps`); the old pinned `sdlc:dispatch-lock` issue (#2) is retired.
- **VP2 topology:** single repo.
- **VP3 modules:** design UX track off; PSI lane off.
- **VP4 dispatcher:** Claude Code scheduled task (the SDLC dispatch task for pemr) → `dispatch.md` (currently
  **disabled**; re-enable after binding `LINT_CMD`/`SMOKE_CMD`). No dispatcher singleton:
  concurrent runs deconflict via per-issue claims, the per-machine maintenance lock
  (`.git/sdlc-maint.lock`), and idempotent GitHub writes. Workers spawn in the background with
  concrete model tiers; keep Fable-class models off the audit lane.
- **VP5 quality bars:** the commands above; lint gate form is `clean` once bound (no ratchet —
  see deviations).
- **Deterministic core:** `SDLC_CLI` above owns every operation in the binding's table.

## Known deviations from spec

- **Priority tiers:** `priority:medium` is retired; **unlabeled is the default** and sorts between
  `critical` and `future` (`PRIORITY_RANK` in `sdlc.mjs`). Why: two exceptional tiers are enough;
  a default label is noise. `labels.md` still lists `priority:medium` — do not create it.
- **Outbound PII guard:** `guardOutboundBody` in `sdlc.mjs` scans every `gh` comment/create/edit
  body through `scripts/pii-scan.mjs`. Why: this repo is public (`AGENTS.md` §"Privacy posture").
- **CLI invocation:** prompts say `sdlc …`; here that is `node sdlc/bindings/gh-issue/sdlc.mjs …`.
- **`sdlc/tools/` lint ratchet not carried.** Why: ESLint-specific; pemr is Python with no lint
  command bound. Revisit if a ruff-based equivalent is wanted.
- `bindings/ado-feature` / `ado-pbi` not carried (no Azure DevOps).
- `docs/Development_AgenticSDLC.md` is pemr's own operational doc (concurrency variant, CLI
  cheat-sheet), not upstream's generic model doc — that content lives in `sdlc/README.md`.
