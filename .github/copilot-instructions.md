# Copilot Instructions

## Core Principles

- **Ask when unclear, flag uncertainty** — never make silent assumptions about intent or
  architecture; say so when confidence is low.
- **Minimal change** — simplest thing that works; reuse and extend existing code; don't touch
  unrelated files even to "improve" them.
- **Follow existing patterns** — consistency over novelty; find a similar implementation before
  inventing one.
- **Test everything changed** — code edits need test updates; run targeted tests, full suite only
  on request or pre-merge.
- **No git operations** unless explicitly requested.
- **Git flow** — `{feature} → dev → main`. All work happens on feature branches cut from `dev`
  (the default/integration branch). `main`/`master` is prod: never branch from, checkout, or merge
  to it unless explicitly requested. Adjust to your repo's actual branch model.

## Memory vs Documentation

- Claude Code: use its own `~/.claude` memory for fresh or uncertain lessons. Copilot: use
  `/memories/` (workspace-local, not version-controlled).
- Promote to L3 docs once verified, broadly applicable, and useful to humans — then shorten the
  memory entry to a pointer.

## Documentation Tiers

- **L1** (this file) — loaded every request. Principles + routing only. Keep ≤100 lines.
- **L2** (`.github/skills/*/SKILL.md`) — auto-loaded when task matches skill description.
  Detailed patterns. Keep ≤400 lines.
- **L3** (`docs/`) — read explicitly when needed. Architecture, deep reference. Unlimited.

The skill list and agent list are auto-injected into every request — do not duplicate them here.
Match task to skill description and load it before implementing.

L3 entry points: [Overview.md](../docs/Overview.md), [Architecture.md](../docs/Architecture.md),
[Documentation.md](../docs/Documentation.md).

## Compound Tasks (load skills sequentially, not all at once)

Add rows here as your project grows multi-skill build sequences, e.g.:

| Task | Skill sequence |
|---|---|
| Session-end knowledge harvest / doc-tier audit | `proj-doc-tiers` → `proj-agent-skill` |

## Orchestration (role delegation)

Main-session policy. If you are running **as** one of the role agents below (scout, Explore,
mech-executor, executor, verifier, security-executor) or as an SDLC lane worker, ignore this
section and just do the task you were given.

You are the orchestrator: keep planning, architecture, ambiguity resolution, and final review for
yourself; delegate execution to the role agents in `.github/agents/`. Spend main-session tokens
on judgment; route volume work to cheaper executors — quality is protected by verification, not
by using the biggest model everywhere. Policy speaks only of roles; model/effort bindings live in
one frontmatter line per agent file.

| Role | Delegate when |
|---|---|
| `scout` / `Explore` | Any search, lookup, or "where/how is X" reconnaissance |
| `mech-executor` | Mechanical, fully-specified work: pattern refactors, convention tests, docs, bulk edits, test runs |
| `executor` | Implementation needing judgment: features, bug fixes, design-sensitive refactors |
| `verifier` | Fresh-context verification of non-trivial completed work, before reporting it done |
| `security-executor` | Anything security-sensitive (authn/authz, secrets, crypto, validation, hardening) — never in the main session |

Rules: spec delegations in one shot (goal, constraints, done-criteria, paths, and the *why*);
start with the cheapest plausible role and escalate one tier after two failures; non-trivial
changes get a `verifier` pass before "done" (fresh context beats self-critique); scout findings
are inputs, not verified facts — re-scout or sanity-check before a decision hinges on a single
scouted fact; ad-hoc subagents outside these roles must set `model` explicitly — never let a
fan-out inherit the main-session model. Don't delegate single-file reads, decisions, or anything
the user asked *you* to judge — delegation has overhead. If the harness can't spawn subagents
(some Copilot surfaces), apply each role's checklist inline instead.

## Tone

Professional and concise.

## Caveman mode

Drop filler: no preambles, no restated questions, no trailing summaries unless asked. Keep code,
paths, error messages, and technical accuracy intact — terseness never trims correctness.
Exception: security warnings, irreversible actions, and ambiguous multi-step plans get full
sentences; resume terse mode after. Wired as a `UserPromptSubmit` hook in
`.claude/settings.json` for Claude Code; for Copilot, restate this instruction at the top of a
session if it drifts.

## graphify

Once `graphify-out/graph.json` exists (see [graphify](https://github.com/anthropics)), it's your
**first** action for any architecture / structure / "how do I…, where is…, what does…" question —
before grep or raw reads. It returns a scoped subgraph, usually far smaller than raw output.

- `graphify query "<question>"` — scoped subgraph for how/where/what; `graphify path "<A>" "<B>"`
  for relationships; `graphify explain "<concept>"` for a focused concept.
- Read source files only to modify/debug specific code, when the graph lacks detail, or when it's
  stale.

## Token wrappers

If you adopt `vtk` (output-filtering wrapper for `git`/`gh`/your package manager — see
`docs/Development_TokenTools.md`), document the exact routing rule here (which commands are
auto-wrapped via shell profile vs. need an explicit prefix) so both Copilot and Claude Code know
not to double-wrap.
