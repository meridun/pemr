# Development_ModelRouting.md — role-based model routing

Project-level adaptation of the orchestration pattern from
[Nanako0129/pilotfish](https://github.com/Nanako0129/pilotfish) (MIT), restructured so the
configuration is **repo-committed and shared** rather than per-machine global.

> **Upstream pin:** blended from pilotfish **v1.1.2**. To re-sync with upstream, diff its
> `templates/agents/` against `.github/agents/*.agent.md` and its
> `templates/claude-md.orchestration.md` against the `## Orchestration` section of
> `.github/copilot-instructions.md`, then bump this pin. The frontier model
plans, decides, and reviews in the main session; cheaper models execute volume work through role
agents. Quality is protected by fresh-context verification, not by using the biggest model
everywhere.

## The three layers, project-level

| Layer | pilotfish (global) | This template (project) |
|---|---|---|
| Roles | `~/.claude/agents/*.md` | `.github/agents/*.agent.md` (canonical) → synced to `.claude/agents/` |
| Policy | `~/.claude/CLAUDE.md` block | `## Orchestration` in `.github/copilot-instructions.md` (loaded by both Copilot and Claude Code via the `CLAUDE.md` @-include) |
| Machine | `~/.claude/settings.json` (`model: best`, `fallbackModel`) | **Deliberately not in the repo** — main-session model choice and fallback chains are per-user/per-machine; committing them would fight teammates' plans and managed policies. Set them in your own `~/.claude/settings.json`. |

## The six roles

| Role | Model | Effort | Used for |
|---|---|---|---|
| `scout` | haiku | low | Read-only lookups: "where/how is X", symbol usages, config values |
| `Explore` | haiku | low | Overrides Claude Code's built-in Explore (which otherwise inherits the expensive main-session model) |
| `mech-executor` | sonnet | low | Fully-specified mechanical work: pattern refactors, convention tests, docs, bulk edits |
| `executor` | opus | medium | Implementation needing judgment: features, bug fixes, design-sensitive refactors |
| `verifier` | opus | medium | Fresh-context adversarial verification; CONFIRMED/REFUTED, never fixes |
| `security-executor` | opus | high | Anything security-sensitive — kept off the frontier tier, whose safety classifiers can refuse benign defensive-security work |

Bindings are **aliases** (`haiku`/`sonnet`/`opus`), never pinned IDs, and live in exactly one
frontmatter line per agent file — the policy text never names a model, so tier deprecations and
plan changes are a one-line edit (or none).

## Dual-harness behavior

- **Claude Code**: `npm run sync:claude-config` mirrors the agents into `.claude/agents/` with
  `model` / `effort` / `disallowedTools` passed through — the cost-tiering levers are
  Claude-Code-specific frontmatter.
- **GitHub Copilot**: reads the same `.github/agents/*.agent.md` sources; the extra fields are
  ignored gracefully. The orchestration policy still applies role *selection*; on Copilot
  surfaces that can't spawn subagents, the policy says to apply the role's checklist inline —
  you keep the discipline (spec-first, adversarial verify, security stance) even without the
  cost savings.
- **User-memory overhead**: custom subagents load user memory that Claude Code's built-ins skip;
  the roles are written to self-disable orchestration policy when running as a role, keeping
  that overhead small.

## Interaction with the Agentic SDLC pipeline

Two different delegation regimes, deliberately kept separate:

- **Interactive sessions** (this policy): the orchestrator delegates synchronously and waits.
- **Scheduled SDLC workers** (`prompts/sdlc/`): workers must never spawn subagents — a headless
  worker that yields mid-task strands its issue under `sdlc:wip`. There, role names mean "apply
  this stance inline", and cost-tiering happens one level up: the **dispatcher** sets each lane
  worker's `model` (see `prompts/sdlc/dispatch.md`). The verify and audit lanes inline the
  `verifier` / `security-executor` stances respectively.

**Dispatcher sizing:** keep the dispatcher itself sonnet-class even though its steps are mostly
CLI-scripted (`sdlc lock/gate/lanes/git-maint/digest`) — a dispatch failure is systemic (it
corrupts or stalls every lane, not one issue), which is the wrong place to shave cost. Consider
haiku only after the remaining judgment steps (worktree sweep, PR conflict scan) are also
scripted and several clean cycles have been observed.

## Coexistence with a global pilotfish install

Safe. A user-level pilotfish install and this repo-level setup stack: Claude Code loads both
user and project agents; on a name collision (`scout`, `executor`, …) the **project-level agent
wins**, which is what you want — the repo's versions know about the project's skills and
graphify. The global policy block and this repo's `## Orchestration` section say the same thing,
so duplication is harmless. If drift between them ever confuses sessions, prefer deleting the
global copy on machines that mostly work in template-derived repos.

## Escalation rule

Start with the cheapest role that can plausibly succeed; after two failed attempts, escalate one
tier or take over. Don't retry the same tier a third time.
