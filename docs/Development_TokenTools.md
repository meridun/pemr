# Development_TokenTools.md — vtk / graphify setup

## graphify

Turns the codebase into a queryable knowledge graph (`graphify-out/graph.json` +
`graphify-out/wiki/`). Once built:

- `graphify query "<question>"` — scoped subgraph
- `graphify path "<A>" "<B>"` — relationship between two concepts
- `graphify explain "<concept>"` — focused concept summary
- `graphify-out/wiki/index.md` — broad navigation
- `graphify-out/GRAPH_REPORT.md` — broad architecture review

The `.claude/hooks/graphify-nudge.py` `PreToolUse` hook (wired in `.claude/settings.json`) nudges
Claude Code toward `graphify query` before raw grep/read, once `graphify-out/graph.json` exists.
Rebuild the graph after structural changes (new subsystems, major refactors).

## vtk

> **Upstream pin:** [meridun/vtk](https://github.com/meridun/vtk) **fc4b1a5** (2026-09-05). To
> re-sync, diff its `README.md` (Usage, Shell integration, Agent hooks) and `docs/` against this
> section and the `## Token wrappers` section of `.github/copilot-instructions.md` / `CLAUDE.md`,
> then bump this pin. Local adaptations to preserve: none.

A command-output compaction wrapper for AI coding agents (an `rtk` workalike). `vtk <cmd>` filters
noisy CLI output (passing test runs, clean lint, verbose diffs) down to failures, diffs, and
deltas before it reaches agent context. Design contract: it never changes the wrapped command's
semantics or exit code; unfiltered passthrough is logged (`vtk gaps`) so coverage gaps are
measurable; filtered output prints an `OK <id>` line and the full raw output stays retrievable
via `vtk show <id>` — no rerun needed for non-idempotent commands like `git commit`.

Install from source (.NET 9 SDK): `dotnet publish dotnet/Vtk.Cli -c Release -o <install-dir>`,
then put the executable on `PATH`. Two wiring modes, both self-locating and idempotent:

- **Agent hook (preferred):** `vtk hooks init` splices one managed `PreToolUse` entry (matcher
  `Bash`) into `~/.claude/settings.json`. Each Bash tool call that is a plain, top-level command in
  an intercepted family (`git`, `gh`, `npm`, `winget`, `choco`, `reg`, plus `grep`/`ls`/`find`) is
  rewritten to run through vtk; pipes, redirects, chains, substitutions, already-wrapped and
  malformed commands pass through untouched. `vtk hooks init --copilot` does the same for GitHub
  Copilot CLI (`~/.copilot/hooks/vtk.json`, `bash|powershell` matcher; the `powershell` flavor
  skips `ls`/`find`). `vtk hooks verify [--copilot]` exits nonzero on a missing, duplicated, or
  desynced hook — run it after moving the binary. Copilot hooks are fail-closed: a stale
  `vtk.json` denies shell calls until `vtk hooks init --copilot` is re-run.
- **Shell wrappers:** `vtk install` adds `$CLAUDECODE`-guarded wrapper functions for
  `git`/`gh`/`npm`/`winget`/`choco`/`reg` to `~/.bashrc` and the PowerShell profile, so the
  families are wrapped inside Claude Code sessions only. `--print`, `--dry-run`, `--uninstall`.

Either mode means agents must **not** prefix `vtk` themselves — the routing rule in the L1
`## Token wrappers` section exists to prevent double-wrapping. Other commands worth knowing:
`vtk gain` (cumulative savings, `--daily`/`--session`), `vtk learn` (mines session JSONL for
fail→succeed corrections into `.claude/rules/cli-corrections.md`), `vtk discover` (ranked
missed-optimization report), `vtk gaps --file-issues` (files recurring gap families as issues).

## Caveman mode

A `UserPromptSubmit` hook in `.claude/settings.json` that injects a one-paragraph terseness rule
into every turn. The canonical text is the `## Caveman mode` section of
`.github/copilot-instructions.md`; the hook carries the same paragraph verbatim and
`npm run check:meta-drift` fails if they diverge. Copilot has no hook support, so the L1 copy is
its only surface; restate it at the top of a session if it drifts.

Provenance: originated here. The rule set is the "lite" tier of the idea popularised by the
public caveman skill (no filler, no tool-call narration, no invented abbreviations or arrow glyphs,
keep negations, keep the session language, normal prose in anything persisted), written
independently. Deliberately **no upstream dependency**: hook-injected, no skill file, no intensity
levels, no subagents, no proxy. The per-turn overhead is one sentence rather than a multi-KB skill
file, which is what keeps it from going net-negative on terse coding work. No token saving is
claimed; measure with and without the hook on the same task if you need a number. Re-read the
public skill occasionally and cherry-pick rule refinements by hand.
