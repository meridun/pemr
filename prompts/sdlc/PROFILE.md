# SDLC conformance profile: pemr

Bindings per the agentic-sdlc spec (`agentic-sdlc/docs/Composability.md`). Nearest-to-template
fork; some prompt placeholders are still unfilled — flagged below.

- **Spine:** `intake → [design] → queued → build → verify → audit → ship`, collapsed tail (human
  PR merge is the `ready` gate).
- **VP1 tracker:** GitHub issues, standard label taxonomy; labels + pinned `sdlc:dispatch-lock`
  issue (#2) created 2026-07-11.
- **VP2 topology:** single repo.
- **VP3 modules:** design lane — [design.md](design.md) present but still the template stub;
  decide on/off when first UI-facing work appears (engine is CLI/SQLite today). PSI lane **off**.
- **VP4 dispatcher:** Claude Code scheduled task → [dispatch.md](dispatch.md), per-issue
  concurrency with worktrees (`C:\Claude\pemr-wt-<issue>`).
- **VP5 quality bars:** Python engine — `pytest` over `tests/` (full suite); lint/type gate and
  smoke command **not yet declared** — fill before enabling the scheduled dispatcher.
- **Deterministic core:** `scripts/sdlc.mjs` (`npm run sdlc`), with integration-branch detection
  (f6124bb). Flow `feature → dev → main`; `dev` is default.
- **Known deviations:** prompts retain generic template phrasing ("your integration branch") —
  placeholder fill incomplete.
