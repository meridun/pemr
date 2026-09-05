# Composability: one spec, many forks

How the agentic SDLC stays shared across projects that differ in tracker, topology, runtime, and
quality bars — without a runtime dependency between them. Companion to
[Development_AgenticSDLC.md](Development_AgenticSDLC.md) (the model) and [Development_SdlcAdoption.md](Development_SdlcAdoption.md) (the mechanics).

## The problem this solves

Four live adoptions, four different shapes:

| Project | Tracker | Topology | Dispatcher runtime | Notable extras |
|---|---|---|---|---|
| IsekaiOnline | GitHub issues | single repo | Claude Code scheduled task → subagent lanes | design lane, playtest verify |
| vtk | GitHub issues | single repo | Claude Code scheduled task | Go toolchain quality bar |
| pemr | GitHub issues | single repo | Claude Code scheduled task | minimal — nearest to template |
| work (ADO) | Azure DevOps work items | **multi-repo Features** (core-api / webapp / utility) | GitHub Copilot session dispatcher, scheduler TBD | PSI lifecycle, child PBIs, ADO parent/predecessor links |

The work environment additionally has **read-only access to this repo and cannot install from
it**. Whatever the framework ships must be consumable as *text an agent can read and transcribe*,
not only as code.

## Distribution model: hybrid, fork-per-project

The framework is delivered in three layers of decreasing normativity. Every project **forks**
(copies and diverges); nothing depends on this repo at runtime.

1. **Normative spec** (`docs/` here) — the invariants, the lifecycle spine, and the variation-point
   contracts below. This is the only layer every fork must conform to.
2. **Reference prompts and contracts** — portable text, itself in three layers inside the one
   copyable tree `sdlc/` ([`sdlc/README.md`](../sdlc/README.md)):
   - **core** (`sdlc/README.md`, `sdlc/lanes/`, `sdlc/dispatch.md`, `.github/agents/sdlc-worker.agent.md`) —
     names every tracker/code-host need as an abstract operation and every project-specific value
     as a `<KEY>`; copied unedited;
   - **bindings** (`sdlc/bindings/<name>/BINDING.md`) — one per substrate, each filling the
     operation contract in [`sdlc/bindings/README.md`](../sdlc/bindings/README.md): `gh-issue`,
     `ado-feature`, `ado-pbi`; picked, not edited;
   - **profile** (`sdlc/PROFILE.md`) — the fork's bindings and keys; the only file a fork writes.
   A fork with write access copies the tree (per [Development_SdlcAdoption.md](Development_SdlcAdoption.md)); a **read-only
   consumer transcribes**: an agent reads this repo, compares against the local implementation,
   and ports the contract language by hand into whatever the local harness accepts (Copilot agent
   files, ADO wiki, dispatch prompts).
3. **Reference executable core** (the `gh-issue` binding's `sdlc.mjs`, or a project's own `sdlc`
   CLI / `sdlc.ps1`) — optional determinism. Deterministic state math (transition validation,
   claim/gate/lock rituals) is strongly recommended where installable, and explicitly *not
   required* for conformance — the work fork substitutes its own `sdlc.ps1` implementing the same
   operations, keyed by the same operation names.

**Upstream sync is manual, but mechanical.** There is no version negotiation: when a fork learns
something, port it here by hand; when this spec improves, port it outward when convenient. Because
core and binding files are copied unedited, porting outward is an overwrite from a newer tag plus
a `SPEC_VERSION` bump in the profile; the conformance profile (below) is what makes an ad-hoc
"diff my fork against the spec" pass cheap to run with an agent. This repo tags releases
(`vMAJOR.MINOR.PATCH`); a profile records the tag it was written against.

## The canonical lifecycle spine

The nine-stage Feature state machine is the universal spine. It supersedes the earlier six-stage
form (`intake → queued → build → verify → audit → ship`): `ship` is decomposed into the explicit
tail `ready → shipping → complete` so the second human gate is first-class, and `design` is a
**standard stage** — its spec track (a reviewed implementation plan on the work item, spec-lite
for small items) always runs, so every item reaches the `queued` gate carrying an approach the
human can approve or veto. Only design's UX/artifact track is optional (VP3).

```
intake → design → queued → build → verify → audit → ready → shipping → complete
         spec      HUMAN                            HUMAN
         always    GATE 1                           GATE 2
```

Core semantics every fork keeps:

- **Two human gates.** `queued` (a human reviews design's implementation plan and commits
  engineering capacity) and `ready` (a human
  approves release). Both are workerless; automation never advances through them.
- **Evidence-based transitions.** Each stage records its artifact on the work item before the item
  moves (intake's requirements + AC, design's implementation plan, build's branch, verify's
  report, audit's findings). A downstream
  stage reconstructs its full context from the work item alone.
- **The five invariants** of [Development_AgenticSDLC.md](Development_AgenticSDLC.md) — one item/one outcome per pass,
  idempotency, isolation (no delegation, no shared tree), stale-lock reaping never live-lock
  stomping, bounce-to-owner / park-to-human — apply verbatim regardless of tracker or runtime.
- **Worker contract.** CLAIM → WORK → EMIT (`ADVANCE`/`BOUNCE`/`PARK`/`CONTINUE`) → STOP, exactly
  one item per pass.

Stage *names* are canonical; stage *content* is parameterized (see quality-bar variation point).
A fork may collapse `ready → shipping → complete` into a thin tail (a single-repo project's
"shipping" is often just "human merges the PR") but the gates and orderings must survive.

## Variation points

These are the sanctioned axes of difference. Each has a small contract; a fork customizes by
binding the contract, not by rewriting the spine.

### VP1 — Tracker backend

The spec's state machine needs, from any tracker, exactly these capabilities (the executable
form — `claim`, `emit`, `dep-edge`, … — is the operation table in
[`sdlc/bindings/README.md`](../sdlc/bindings/README.md), which each shipped binding fills):

| Abstract operation | `gh-issue` binding (template) | `ado-feature` binding (work) |
|---|---|---|
| stage marker (exactly one) | `stage:*` label | `stage:*` tag on the Feature (native States stay coarse/derived) |
| routing marker | repo labels / single repo | exactly one `repo:*` tag per child |
| claim lock + timestamp | `sdlc:wip` + claim comment | stage-Task claim line written by rev-CAS + transient `sdlc:wip` visibility tag |
| park to human | `sdlc:needs-human` | `sdlc:needs-human` **on the Feature**, HUMAN ACTION REQUIRED discussion comment |
| human keep-off | `sdlc:hold` | `sdlc:hold` |
| evidence record | issue **body sections** for durable artifacts (`## Requirements` / `## Acceptance criteria` / `## Design` / `## Implementation plan`, one owner per section) + comments for protocol traffic | split by level: Feature comments (team-facing traffic), PBI description (tech spec/plan), stage Tasks (agent working memory) |
| hierarchy & ordering | n/a (flat issues) | ADO Parent link (membership), predecessor/successor links (provider→consumer order) |
| tag mutation safety | `gh` label ops | **all tag ops via `sdlc.ps1`** — raw ADO CLI replaces rather than appends |
| status dashboard *(optional cache)* | — (labels + thread suffice) | description status block, dispatcher-rewritten from evidence |

Rule: a fork documents its binding table once, then every prompt in that fork speaks the local
dialect. The abstract operation names are the shared vocabulary for cross-fork comparison.

**Lock-substrate contract.** Whatever carries the claim lock must provide all three of:

1. **Deterministic contention resolution** — either an *append-only, server-timestamped* record
   (GitHub claim comments; requires the claim-verify + boundary ritual of
   the `gh-issue` binding), or a *compare-and-swap* write (e.g. an ADO PATCH tested against
   `System.Rev` — on a field or on a child Task's description), which serializes claims at the
   tracker and collapses the claim-verify ritual entirely.
2. **Provable age and owner from server-side data** — a comment timestamp, a field revision, or a
   timeline event. A worker-authored string alone proves nothing, and a whole-item modified stamp
   (`updatedAt` / `ChangedDate`) is refreshed by any edit. A lock whose age cannot be proven is
   never reaped — leave it and record it.
3. **A cheap "all locked items" query** for the dispatcher's Step 0 snapshot (a label filter, a
   WIQL field/tag clause — not a full-text scan of a rich-text field).

A fork may additionally cache a **derived status block** on the work item (see the ADO profile
for the worked format). A cache is never authoritative: it is regenerated from the evidence
record on any parse failure, never trusted or repaired by guesswork, and locks never live in it.

### VP2 — Topology: single-repo item vs multi-repo Feature

The multi-repo model is the general case; single-repo is its degenerate form.

- **Feature** — the authoritative record: owns lifecycle, requirements, acceptance criteria,
  dependencies, human gates, integration, release. Human attention consolidates here
  (`sdlc:needs-human` lives at the Feature; children report `result:*` evidence but never own the
  human-attention state).
- **Child PBI** — a repository-scoped execution record, *not* an independent workflow: one routing
  tag, technical plan, reserved branch/PR, phase evidence, short-lived worker locks.
- **Cross-repo ordering** — providers and independent children build first; a consumer may start
  once its predecessor publishes `contract-ready` evidence (it need not wait for the provider's PR
  to ship). At `verify`, every child runs its own repo's full verification; `audit` reconciles
  across children; `complete` requires all children shipped **and** Feature-level ACs pass.
- **Single-repo forks** collapse this: the issue is simultaneously the Feature and its only child.
  No hierarchy links, no routing tags, no contract-ready handshake. Nothing else changes.

### VP3 — Lifecycle modules (optional, on top of the spine)

- **Design UX track** — the design *stage* itself is spine, not a module: its spec track always
  runs, and every item reaches `queued` carrying a reviewed implementation plan. What varies is
  the **UX/artifact track**: a fork whose work is user-facing binds `<DESIGN_ARTIFACTS>` in its
  profile (its conventions for storyboards/mockups — what "what it looks like / how it behaves"
  is recorded in). Material UI changes then require version-controlled design artifacts + a human
  A/B/C pick (a PARK inside the design phase, recorded as design evidence) before the spec is
  written; design approval is distinct from and does not replace the `queued` gate. Forks without
  UI work leave the binding empty — every item takes the spec track only (spec-lite for exempt
  items), and product/scope questions still run as intake decision debates.
- **PSI lane** (production-support investigation) — a customization, not core. Its own machine
  (`reported → triage → diagnosed → decided → pending-fix → resolved`); the automated PSI worker is
  **read-only** (documents severity/repro/evidence/root-cause, never writes code); at `decided` a
  human chooses no-change / documentation / duplicate / needs-code; a needs-code PSI creates or
  attaches to a normal Feature and rides the spine from there. No automation bypass for any
  priority. Forks without production support omit the lane entirely.
- Future modules follow the same shape: a named sub-machine that *enters* the spine at a defined
  point (PSI enters at intake; the design UX track binds inside the design stage) and never adds a
  third human gate to the spine itself. (The UX track's PARK is a worker parking for a missing
  input, not a new gate.)

### VP4 — Dispatcher runtime

The dispatcher contract is runtime-agnostic: a concurrency model of per-issue claims + idempotent
verify-before-write tracker writes + a per-machine maintenance lock (no dispatcher singleton),
stale-lock reaping (2h heuristic, verify-before-write), git/PR maintenance, per-lane fan-out of
isolated workers, end-of-cycle digest. The reference maintenance-lock binding is a lock
**directory** at `.git/sdlc-maint.lock`: atomic-`mkdir` acquire (exactly one contender succeeds),
an `owner.txt` stamp (`<run-id> <ISO timestamp>`), and a 30-minute stale reap by atomic
**rename** (exactly one contender wins the reap). One gotcha rides the representation: because
acquisition is a `mkdir` under `.git/`, the lock cannot be taken from inside a git worktree
(there `.git` is a file, not a directory) — maintenance runs only from the main checkout.
Bindings in the wild:

- **Claude Code** — scheduled task → `dispatch.md` → one worker subagent per non-empty lane
  (IsekaiOnline, vtk, pemr).
- **GitHub Copilot** — interactive session runs the dispatcher prompt; scheduler mechanism TBD
  (work). Same contract; the fork documents how machine-local maintenance is serialized.
- **cron / CI** — headless agent invocation with the thin-pointer prompt.

A fork must state its binding for: how the cycle is triggered, how machine-local maintenance is
serialized (the per-machine lock's representation), and how workers are isolated (worktrees,
separate clones, or serial-variant tree hygiene).

**Coexistence with session-orchestration layers.** A machine running the dispatcher may also carry
a user-level interactive-orchestration policy (e.g. [pilotfish](https://github.com/Nanako0129/pilotfish),
which installs role agents and a delegation policy under `~/.claude/`). The two are orthogonal and
compose by scope: the orchestration layer governs *interactive* sessions; the SDLC pipeline governs
its own scheduled cycle, and `dispatch.md` declares that its fixed topology overrides any
session-level delegation policy for the cycle (the specific-over-general precedence such layers
themselves document). Two conventions deliberately differ and should not be "harmonized": role-agent
layers pin the model in each agent's frontmatter and tell the orchestrator to *omit* the model
argument when invoking a named role, while this pipeline uses **one** worker agent for every lane
and therefore *always sets* the model per spawn (the lane tier table) — each is correct for its
shape. Workers themselves are unaffected by construction: `<WORKER_AGENT>` has no delegation tool,
so no session policy can make it spawn anything.

### VP5 — Quality bars

`<TEST_CMD>`, `<FULL_SUITE_CMD>`, `<SMOKE_CMD>`, `<LINT_CMD>`, `<INVARIANTS>`, `<DOCS_SINKS>` are
per-fork by design and per-*repo* within a multi-repo fork (each child PBI verifies to its own
repo's bar). The stage semantics ("verify proves it works", "audit reviews security + invariants +
contracts + docs + merge order") are fixed; what proof consists of is the fork's declaration.
A fork with an inherited lint backlog may bind `<LINT_CMD>` to the reference **ratchet**
(`sdlc/tools/check-lint-baseline.mjs`: no growth vs a committed per-rule baseline + touched
files clean) rather than "lint clean" — declare which in the profile so the gate is auditable.

## The conformance profile

Each fork keeps one file — `sdlc/PROFILE.md`, filled from the shipped template
([`sdlc/PROFILE.md`](../sdlc/PROFILE.md)) — stating its bindings. It is **externalized
configuration**, not documentation: every `<KEY>` a core prompt names resolves at runtime to a row
of the profile's § Keys, and `BINDING` names which `bindings/<name>/BINDING.md` resolves the
abstract operations. That is what lets the core and binding files be copied unedited — and what
makes read-only consumption and ad-hoc drift audits work: an agent with read access to both
repos can diff a profile against this spec mechanically.

```markdown
# SDLC profile: <project>
Spec version: <framework tag>
## Keys
| BINDING | gh-issue | ado-feature | ado-pbi |
| SDLC_CLI | … or none |
| PROJECT · REPO_PATH · WORKER_AGENT · DEFAULT_BRANCH · PROD_BRANCH · WORKTREE_ROOT | … |
| BUILD_CMD · TEST_CMD · FULL_SUITE_CMD · LINT_CMD · SMOKE_CMD | … |
| LANG_CONVENTIONS · INVARIANTS · DECISION_RECORD · DOCS_SINKS | … |
| optional: DESIGN_ARTIFACTS · KNOWN_ENV_LIMITS · DEP_AUDIT_CMD · MIGRATIONS_DIR (+ down/up/dump) · DOCS_ROOT · DOC_DOMAINS · TOKEN_TOOL | unbound = the lane step is skipped |
## Variation points
- Spine: intake → design → queued → build → verify → audit → ready → shipping → complete
  (or the collapsed tail)
- VP1 tracker: per the binding — anything bound differently
- VP2 topology: <single-repo | multi-repo Feature/child> — routing markers if multi
- VP3 modules: design UX track <off | bound: conventions + trigger>, PSI lane <on/off>, others
- VP4 dispatcher: <trigger, maintenance-lock representation, worker isolation>
- VP5 quality bars: per repo; which lint-gate form
- Deterministic core: <none | sdlc.mjs | sdlc.ps1 | sdlc CLI> and which operations it owns
## Known deviations from spec: <list, with why>
```

Worked examples: [Development_SdlcProfileExample.md](Development_SdlcProfileExample.md) (the minimal
single-repo GitHub case with the reference CLI); the Azure DevOps examples (`ado-feature`,
`ado-pbi`) live upstream in agentic-sdlc under `docs/profiles/`.

The "known deviations" line is load-bearing: fork-per-project means divergence is legitimate, but
*undeclared* divergence is drift. An audit pass = read profile, read spec, list deltas, file
issues locally.

## What is NOT a variation point

- The five invariants.
- The two human gates and their positions.
- The worker contract (CLAIM → WORK → EMIT → STOP, one item, one outcome, never silent).
- Human-set markers (`needs-human`, `hold`) being untouchable by automation.
- Evidence-on-the-work-item as the only inter-stage state.
- Read-only-ness of the audit stage and of any PSI investigation worker.

A fork that needs to break one of these has found either a spec bug (port the fix here) or a
different methodology (stop calling it this one).
