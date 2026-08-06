# SDLC conformance profile: pemr

Bindings per the agentic-sdlc spec (`agentic-sdlc/docs/Composability.md`). Prompts are the
upstream templates verbatim (resynced 2026-08-06 from agentic-sdlc `3e0db2d`, with
`tools/sdlc.mjs` → `scripts/sdlc.mjs` path rewrites); this file is the placeholder binding.

- **Spine:** `intake → [design] → queued → build → verify → audit → ship`, collapsed tail (human
  PR merge is the `ready` gate).
- **VP1 tracker:** GitHub issues, standard label taxonomy; labels created 2026-07-11. The pinned
  `sdlc:dispatch-lock` issue (#2) is **retired/obsolete** as of 2026-07-16 (dispatcher singleton
  removed) — can be closed.
- **VP2 topology:** single repo.
- **VP3 modules:** design lane — upstream now treats design as a **standard phase** (spec track
  always; spec-lite for small items). UX track off until UI-facing work appears (engine is
  CLI/SQLite today). PSI lane **off**.
- **VP4 dispatcher:** Claude Code scheduled task → [dispatch.md](dispatch.md); **no dispatcher
  singleton** — concurrent dispatch runs deconflict via per-issue claims + a per-machine
  maintenance lock (`.git/sdlc-maint.lock`) + idempotent GitHub writes. Per-issue concurrency
  with worktrees (`C:\Claude\pemr-wt-<issue>`). Lane workers spawn **in the background** (see
  dispatch.md); use concrete model tiers, keep Fable-class models off the audit lane.
- **VP5 quality bars:** Python engine — `pytest` over `tests/` (full suite); lint/type gate and
  smoke command **not yet declared** — fill before enabling the scheduled dispatcher.
- **Deterministic core:** `scripts/sdlc.mjs` (`npm run sdlc`), upstream reference CLI with
  `DEFAULT_BRANCH = 'dev'`, `PROD_BRANCH = 'main'` (static constants; the f6124bb origin/HEAD
  detection was retired in the 2026-08-06 resync to stay diffable against upstream). Planner
  tests: `npm test` → `test/sdlc.test.mjs`. Flow `feature → dev → main`; `dev` is default.

## Placeholder bindings

| Placeholder | Binding |
|---|---|
| `<PROJECT>` | pemr |
| `<REPO_PATH>` | `C:\Claude\pemr` |
| `<DEFAULT_BRANCH>` | `dev` |
| `<PROD_BRANCH>` | `main` |
| `<WORKTREE_ROOT>` | `C:\Claude` (worktrees `pemr-wt-<issue#>`) |
| `<WORKER_AGENT>` | `general-purpose` subagent with explicit `model` (no dedicated sdlc-worker agent yet) |
| `<TEST_CMD>` | `pytest <targeted paths>` |
| `<FULL_SUITE_CMD>` | `pytest` |
| `<LINT_CMD>` | **unbound** — declare before enabling the scheduled dispatcher |
| `<SMOKE_CMD>` | **unbound** — declare before enabling the scheduled dispatcher |
| `<BUILD_CMD>` | n/a (interpreted Python; no build step) |
| `<LANG_CONVENTIONS>` | follow existing patterns; see `.github/copilot-instructions.md` |
| `<INVARIANTS>` | see `docs/Architecture.md` |
| `<DESIGN_ARTIFACTS>` | in-issue `## Implementation plan` comment (no external design store) |
| `<DECISION_RECORD>` | in-issue `decision:` one-liner comment |
| `<DOCS_SINKS>` | `docs/` (L3), `.github/skills/` (L2), `.github/copilot-instructions.md` (L1) |
| `<TOKEN_TOOL>` | none wired (see `docs/Development_TokenTools.md`) |
| `<KNOWN_ENV_LIMITS>` | Windows 11 / PowerShell host; no display server assumptions beyond CLI |
