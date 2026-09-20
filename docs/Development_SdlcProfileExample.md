# SDLC profile: acme — worked example (`gh-issue`)

The minimal shape most forks start from: one GitHub repository, issues as the tracker, the
reference CLI installed, the collapsed spine tail. Distilled from a live single-repo adoption;
the project name is genericized to `acme`. A filled copy of [`sdlc/PROFILE.md`](../sdlc/PROFILE.md);
bindings per [Development_SdlcComposability.md](Development_SdlcComposability.md).

Spec version: `v2.0.0`

## Keys

| Key | Value |
|---|---|
| `BINDING` | `gh-issue` |
| `SDLC_CLI` | `node sdlc/bindings/gh-issue/sdlc.mjs` |
| `SPEC_VERSION` | `v2.0.0` |
| `PROJECT` | `acme` |
| `REPO_PATH` | `C:\work\acme` |
| `WORKER_AGENT` | `acme-sdlc-worker` |
| `DEFAULT_BRANCH` | `dev` |
| `PROD_BRANCH` | `main` |
| `WORKTREE_ROOT` | `../acme-wt` |
| `BUILD_CMD` | `npm run build` |
| `TEST_CMD` | `npm run test:file <path>` |
| `FULL_SUITE_CMD` | `npm test` |
| `LINT_CMD` | `npm run lint:baseline` (ratchet — no new ESLint errors vs `sdlc/tools/lint-baseline.json`, touched files clean; `npm run lint` is the interactive/human command) |
| `SMOKE_CMD` | `npm run e2e` |
| `LANG_CONVENTIONS` | eslint ratchet passing, prettier applied, jest green |
| `INVARIANTS` | multi-actor safety (never assume single-user state); defensive at boundaries (guard `undefined`, especially DB results); data access only in the repository layer |
| `DECISION_RECORD` | `docs/Decisions.md` |
| `DOCS_SINKS` | `README.md` + the `docs/` tree |
| `DESIGN_ARTIFACTS` | competing storyboards under `docs/mockups/` per its README |
| `KNOWN_ENV_LIMITS` | *(none declared)* |
| `DEP_AUDIT_CMD` | `npm audit --json` |
| `MIGRATIONS_DIR` | `db/migrations/` |
| `MIGRATE_DOWN_CMD` / `MIGRATE_UP_CMD` | `dbmate down` / `dbmate up` |
| `SCHEMA_DUMP` | `db/schema.sql` |
| `DOCS_ROOT` / `DOC_DOMAINS` | `docs/` / `Architecture_*, Testing_*, UserGuide_*` |
| `TOKEN_TOOL` | *(none)* |

## Variation points

- **Spine:** `intake → design → queued → build → verify → audit → ship` with the **collapsed
  tail** — ship opens the PR, the human merge **is** the `ready` gate, and `shipping → complete`
  collapse into merge-and-close. Design is standard (spec track always; UX track bound); the only
  bypass is intake's already-built → `stage:verify` floor.
- **VP1 tracker:** exactly as [`gh-issue/BINDING.md`](../sdlc/bindings/gh-issue/BINDING.md) —
  labels per its `labels.md`; claim = `sdlc:wip` + claim comment; evidence in issue body
  sections + comments; native dependency edges; **all marker math via the CLI**, never
  hand-typed.
- **VP2 topology:** single repo — each issue is simultaneously the Feature and its only child.
- **VP3 modules:** design UX track **bound** (the product is UI-facing): competing storyboards,
  human A/B/C pick parked in-phase, decision graduated to `docs/Decisions.md`. PSI lane **off**.
- **VP4 dispatcher:** Claude Code scheduled task (hourly) → `sdlc/dispatch.md` → one
  `acme-sdlc-worker` subagent per non-empty lane. No dispatcher singleton — per-issue claims,
  idempotent verify-before-write GitHub writes, per-machine `.git/sdlc-maint.lock` (atomic mkdir,
  30-min stale reap); issue-scoped worktrees at `../acme-wt/<issue#>`. Lane tiers per
  `dispatch.md`'s table.
- **VP5 quality bars:** the commands above; lint gate is the **ratchet** form; no known env
  limits; migrations bound (verify migration checks + audit diff-shape check).
- **Deterministic core:** `sdlc.mjs` — owns claim/emit/advance transition validation, the wip
  gate, the maintenance lock, the edge-based eligibility gate + derived readiness labels
  (`deps`), the close-sweep work-list/ack, and the cycle-prep report.

## Known deviations from spec

*(none)* — this is the template's own default shape, declared so drift audits have a baseline to
diff against.
