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

## vtk (bring your own binary)

A git/gh/package-manager output-filtering wrapper — cuts noisy CLI output (passing test runs,
clean lint, verbose diffs) before it reaches agent context, while keeping failures/summaries
inline. This template doesn't ship a `vtk` binary; if you build or adopt one:

1. Route the commands you want filtered through it from your shell profile (e.g. `~/.bashrc`
   aliasing `git`/`gh`/`npm` to the wrapper) so agents don't need to remember a prefix.
2. Document the exact routing rule in `CLAUDE.md` and `.github/copilot-instructions.md` — which
   commands are auto-wrapped vs. need an explicit prefix — so agents don't double-wrap or bypass
   it by accident.
3. Verify machine-parseable output (e.g. `git diff` piped into a patch apply) isn't mangled by the
   wrapper before relying on it for destructive operations.

## Caveman mode

A `UserPromptSubmit` hook in `.claude/settings.json` that injects a terseness instruction into
every turn — no unnecessary preamble or trailing summaries, but full sentences preserved for
security warnings, irreversible actions, and ambiguous multi-step plans. For Copilot (no hook
support), restate the instruction in `.github/copilot-instructions.md` (already done) and at the
top of a session if it drifts.
