# Adoption guide

How to wire this template into a real repo. ~30 minutes for a first pass.

## 1. Copy the trees

- `prompts/sdlc/` → `prompts/sdlc/` in your repo (or anywhere your agent can read; the dispatcher just
  needs the path).
- `.github/agents/sdlc-worker.agent.md` → your harness's agent dir, with the matching frontmatter from that file:
  - Claude Code: `.claude/agents/sdlc-worker.md`
  - GitHub Copilot: `.github/agents/sdlc-worker.agent.md`

Name the agent something project-scoped (e.g. `acme-sdlc-worker`) and use that everywhere `sdlc-worker`
appears. Avoid names already claimed by session-orchestration layers installed at the user level —
e.g. pilotfish registers `scout`, `Explore`, `plan-verifier`, `security-reviewer`, `mech-executor`,
`executor`, `verifier`, `security-executor` — a collision would silently swap in the wrong agent
body. A project-scoped name sidesteps the whole class.

Two Claude Code notes:

- **Version floor.** The worker's no-delegation guarantee rests on the harness *enforcing* the
  agent file's tool list, not on prompt text. Enforced tool exclusion is reliable on Claude Code
  ≥ 2.1.219 — treat that as the floor for scheduled operation.
- **`CLAUDE_CODE_SUBAGENT_MODEL`** silently overrides per-spawn model arguments; if it's set on the
  dispatch machine, the per-lane tier table in `dispatch.md` is a no-op. Unset it for the
  dispatcher's environment.

- *(optional)* `skills/proj-doc-tiers/` → your harness's skill dir (Claude Code:
  `.claude/skills/proj-doc-tiers/`). Copy it if the ship stage's docs fan-out should follow the
  hub-and-spoke tier discipline; skip it if your docs are a flat README.

## 2. Create the labels

Run the `gh` script in [Development_SdlcLabels.md](Development_SdlcLabels.md). It creates the `stage:*`, `sdlc:*`, and `priority:*`
labels the prompts depend on.

## 3. Fill the placeholders

Grep for `<` across `prompts/` and `agents/` and replace every one. The full list:

| Placeholder | Meaning | Example |
|---|---|---|
| `<PROJECT>` | Project name (appears in every worker's opening line) | `Acme API` |
| `<REPO_PATH>` | Local working directory the scheduled agent runs in | `C:\work\acme` |
| `sdlc-worker` | The isolated worker agent's `subagent_type` name | `acme-sdlc-worker` |
| `<DEFAULT_BRANCH>` | Integration branch — PRs target it, branches cut from it | `dev` or `main` |
| `<PROD_BRANCH>` | Release branch — **off-limits** to all workers | `main` or `release` |
| `<WORKTREE_ROOT>` | Where issue-scoped worktrees live | `../acme-wt` |
| `<BUILD_CMD>` | Build/compile/launch the software for a real run | `npm run build` |
| `<TEST_CMD>` | Targeted test run (build stage) | `npm test -- <path>` |
| `<FULL_SUITE_CMD>` | Full test suite (verify stage) | `npm test` |
| `<LINT_CMD>` | Lint/format/type gate that must be clean before commit | `npm run lint` |
| `<SMOKE_CMD>` | Repeatable real-run / e2e / smoke spec (verify stage) | `npm run e2e` |
| `<LANG_CONVENTIONS>` | The lint/format/test bar in one line | `eslint clean, prettier applied, jest green` |
| `<INVARIANTS>` | Project rules that are ACs on **every** change — list them | `no breaking API changes; all endpoints authz-checked; no PII in logs` |
| `<DECISION_RECORD>` | Where decisions are logged (a doc section, a registry file) | `docs/Decisions.md` |
| `<DESIGN_ARTIFACTS>` | *(optional, design UX track)* the fork's conventions for design artifacts — what storyboards/mockups are, where they live, how they're authored; leave unbound to run spec-track only | `docs/mockups/ per its README` |
| `<KNOWN_ENV_LIMITS>` | *(optional, verify)* declared environment limitations: the gate that can't run, its accepted substitute, and the report wording — so verify honors them instead of rediscovering (or PARKing over) them each pass | `integration suite needs local MySQL — covered via Docker e2e` |
| `<DOCS_SINKS>` | Documentation targets ship fans out to | `README.md, docs/API.md` |
| `<DOCS_ROOT>` | *(optional, docs-tiers skill)* root of the L3 documentation tree | `docs/` |
| `<DOC_DOMAINS>` | *(optional, docs-tiers skill)* thematic domain prefixes files route into | `Architecture_*, Testing_*, UserGuide_*` |
| `<TOKEN_TOOL>` | *(optional)* shell-output compactor — either an explicit prefix on every command, or a transparent shell wrapper (see `.github/agents/sdlc-worker.agent.md` for the two binding modes); delete the mentions if none | `tok` |

`<INVARIANTS>` is the one that most repays effort — it is the shared acceptance criterion build
implements to, verify exercises, and audit reviews for. Be specific and concrete.

## 4. Copy the reference CLI (recommended)

Copy `scripts/sdlc.mjs` into your repo (plain Node, no dependencies) and adapt the constants at the
top — `DEFAULT_BRANCH`, `PROD_BRANCH`, and the worktree naming function. Then wire the lane prompts
to it: wherever a prompt describes the claim lock, outcome emit, stage swap, or dispatcher gate
ritual, have
workers run the CLI one-shot instead
(`npm run sdlc -- claim|emit|advance|gate|cycle-prep|maint-lock|lanes|…`).

Why bother: `advance` validates every transition against the stage graph, which kills the
hand-typed label-typo class (`stage:verfy`) by construction, and it makes the gate, machine
maintenance lock, and claim-verify race check deterministic — the agent supplies judgment (what to
spawn, what to write in comments), the CLI supplies the state math. The pure helpers are exported and the
gh/git executors injectable, so you can unit-test your adaptations without touching GitHub.

## 5. Choose your variant

- Small backlog / single tree → **serial** (see [Development_AgenticSDLC.md](Development_AgenticSDLC.md#concurrency-variants);
  simplify `dispatch.md` per the note there, and you can ignore the worktree steps).
- Throughput matters → keep the shipped **per-issue** model. Nothing to create up front: there is
  no dispatcher singleton — overlapping dispatch runs deconflict via per-issue claims, idempotent
  GitHub writes, and a per-machine maintenance lock the dispatcher creates automatically at
  `.git/sdlc-maint.lock`. Just confirm your platform allows git worktrees.

Design is a **standard stage** — every item gets a reviewed implementation plan there (spec-lite
for small items), so the `stage:design` label and the shipped `prompts/sdlc/design.md` worker are
part of the default pipeline. What you decide is whether to bind its **UX track**: bind
`<DESIGN_ARTIFACTS>` in your profile if your work is user-facing and worth storyboarding; leave it
unbound to run spec-track only.

## 6. Write your conformance profile

Create `prompts/sdlc/PROFILE.md` from the skeleton in
[Development_SdlcComposability.md](Development_SdlcComposability.md#the-conformance-profile), declaring your bindings for the five
variation points and any known deviations. This is what keeps ad-hoc drift audits against the spec
cheap. Two worked examples: [profiles/github-single-repo.example.md](https://github.com/meridun/agentic-sdlc/blob/master/docs/profiles/github-single-repo.example.md)
(the minimal single-repo GitHub case most forks start from) and
[profiles/work-ado.example.md](https://github.com/meridun/agentic-sdlc/blob/master/docs/profiles/work-ado.example.md) (a multi-repo Azure DevOps
read-only consumer).

## 7. Dry-run manually before scheduling

Paste `prompts/sdlc/README.md` + one stage file (start with `intake.md`) into an agent session pointed
at your repo. Feed it a real issue labeled `stage:intake`. Watch it CLAIM, WORK, and EMIT. The prompt
behaves identically whether a human or a scheduler fired it — so a clean manual pass means the
scheduled one will work. Walk one issue all the way through intake → ship this way before automating.

## 8. Schedule the dispatcher

Register a recurring task whose body is a **thin pointer** to `dispatch.md`, e.g.:

> Read `<REPO_PATH>/prompts/sdlc/dispatch.md` and execute one dispatch cycle for the `<PROJECT>`
> project: spawn one `sdlc-worker` subagent per non-empty lane as the file directs. The file is
> the canonical prompt; follow it exactly.

Keep the delegation cue ("spawn one `sdlc-worker` subagent per …") in the pointer itself:
harness versions have been observed declining to spawn subagents from prompts that carry no
explicit delegation instruction, and the pointer is the first text the scheduled session sees.

Cadence: hourly is typical (the 2h reap threshold assumes ≤ hourly). Options:
- **Claude Code scheduled tasks** — the native path; the task calls the dispatcher subagent.
- **cron + headless agent** — `cron` → a headless agent invocation with the pointer as its prompt.
- **CI cron** (GitHub Actions `schedule:`) — a job that runs the agent CLI against the pointer.

**Enable it only once your queue depth justifies the spend** — each cycle costs tokens per non-empty
lane. Until then, run it manually on demand.

**Dispatcher sizing:** keep the dispatcher itself mid-tier (sonnet-class) even once its steps are
CLI-scripted — a dispatch failure is systemic (a whole cycle misroutes), not per-issue like a worker
failure. Consider dropping it to a small model (haiku-class) only after the worktree sweep and
conflict scan are scripted too and you've observed several clean cycles.

## 9. Watch the first few cycles

The dispatcher's digest (end of every run) is your dashboard: cycle duration, machine-lock result,
git maintenance,
one line per lane, queue depths, parked items, and **token cost per lane + cycle total**. That token line is the
trend to watch for cost regressions. Parked (`sdlc:needs-human`) items are your action queue.

**Track which pipeline paths the chain has actually exercised.** A full-chain ADVANCE run proves
the happy path, but most BOUNCE/PARK/CONTINUE tails start out unproven — keep a short provenance
list (which issues proved which paths) in your pipeline doc, and exercise an unproven tail
manually before trusting it scheduled. Fold what those runs teach back into the worker prompts;
the prompts, not the list, stay canonical for behavior.
