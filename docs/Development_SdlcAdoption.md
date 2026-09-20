# Adoption guide

How to wire this template into a real repo. ~30 minutes for a first pass. The tree is layered —
**core** (shared prompts, never edited on adoption), **binding** (one per tracker substrate,
picked not edited), **profile** (the one file you fill) — see [`sdlc/README.md`](../sdlc/README.md).

## 1. Copy the tree

- `sdlc/` → `sdlc/` in your repo. One directory, copied verbatim: the core (`README.md`,
  `lanes/`, `dispatch.md`), the bindings (`bindings/*`), the profile template (`PROFILE.md`), and
  the tracker-neutral tools (`tools/`). Keep the path — the gh-issue CLI resolves your repo root
  as three directories above itself, and every pointer in this guide assumes `sdlc/` at the root.
  You may delete the `bindings/<name>/` directories you don't bind.
- `.github/agents/sdlc-worker.agent.md` → your harness's agent dir, with the matching frontmatter from that file:
  - Claude Code: `.claude/agents/<WORKER_AGENT>.md`
  - GitHub Copilot: `.github/agents/<WORKER_AGENT>.agent.md`

Name the agent something project-scoped (e.g. `acme-sdlc-worker`) and use that as `WORKER_AGENT`
in the profile. Avoid names already claimed by session-orchestration layers installed at the user
level — e.g. pilotfish registers `scout`, `Explore`, `plan-verifier`, `security-reviewer`,
`mech-executor`, `executor`, `verifier`, `security-executor` — a collision would silently swap in
the wrong agent body. A project-scoped name sidesteps the whole class.

Two Claude Code notes:

- **Version floor.** The worker's no-delegation guarantee rests on the harness *enforcing* the
  agent file's tool list, not on prompt text. Enforced tool exclusion is reliable on Claude Code
  ≥ 2.1.219 — treat that as the floor for scheduled operation.
- **`CLAUDE_CODE_SUBAGENT_MODEL`** silently overrides per-spawn model arguments; if it's set on the
  dispatch machine, the per-lane tier table in `dispatch.md` is a no-op. Unset it for the
  dispatcher's environment.

- *(optional)* `.github/skills/pemr-doc-tiers/` → your harness's skill dir (Claude Code:
  `.claude/skills/pemr-doc-tiers/`). Copy it if the ship stage's docs fan-out should follow the
  hub-and-spoke tier discipline; skip it if your docs are a flat README.

## 2. Pick the binding

| You have | Bind | Then |
|---|---|---|
| GitHub issues, one repo | [`gh-issue`](../sdlc/bindings/gh-issue/BINDING.md) | create the labels: run the `gh` script in [`labels.md`](../sdlc/bindings/gh-issue/labels.md) |
| Azure DevOps | [`ado-feature`](https://github.com/meridun/agentic-sdlc/tree/34b769e/sdlc/bindings/ado-feature/BINDING.md) (multi-repo) or [`ado-pbi`](https://github.com/meridun/agentic-sdlc/tree/34b769e/sdlc/bindings/ado-pbi/BINDING.md) (single repo, proposed) — **upstream only**, not carried in this repo | copy the binding directory from upstream agentic-sdlc into `sdlc/bindings/` first |

The binding is read at runtime, not edited: every abstract operation the core names (`claim`,
`emit`, `dep-edge`, …) is a row in its table. If your substrate needs something different, that
is a **declared deviation** in the profile, not a silent edit of the binding.

## 3. Fill the profile

Fill `sdlc/PROFILE.md`. Prompts are **not** edited: every `<KEY>` a prompt names resolves at
runtime to the matching row of the profile's § Keys (a fork *may* additionally inline-substitute;
the profile stays authoritative). Required keys:

| Key | Meaning | Example |
|---|---|---|
| `BINDING` | the bound `bindings/<name>/` | `gh-issue` |
| `SDLC_CLI` | the binding's deterministic core invocation, or `none` | `node sdlc/bindings/gh-issue/sdlc.mjs` |
| `SPEC_VERSION` | the framework git tag this profile was written against | `v2.0.0` |
| `PROJECT` | Project name (appears in every worker's opening line) | `Acme API` |
| `REPO_PATH` | Local working directory the scheduled agent runs in | `C:\work\acme` |
| `WORKER_AGENT` | The isolated worker agent's `subagent_type` name | `acme-sdlc-worker` |
| `DEFAULT_BRANCH` | Integration branch — PRs target it, branches cut from it | `dev` or `main` |
| `PROD_BRANCH` | Release branch — **off-limits** to all workers | `main` or `release` |
| `WORKTREE_ROOT` | Where issue-scoped worktrees live | `../acme-wt` |
| `BUILD_CMD` | Build/compile/launch the software for a real run | `npm run build` |
| `TEST_CMD` | Targeted test run (build stage) | `npm test -- <path>` |
| `FULL_SUITE_CMD` | Full test suite (verify stage) | `npm test` |
| `LINT_CMD` | Lint/format/type gate that must be clean before commit. On a repo with an existing lint backlog bind it to the **ratchet** (§ 5) — then "clean" means *no growth vs the committed per-rule baseline, and touched files clean on their own* | `npm run lint` · `npm run lint:baseline` |
| `SMOKE_CMD` | Repeatable real-run / e2e / smoke spec (verify stage) | `npm run e2e` |
| `LANG_CONVENTIONS` | The lint/format/test bar in one line | `eslint clean, prettier applied, jest green` |
| `INVARIANTS` | Project rules that are ACs on **every** change — list them | `no breaking API changes; all endpoints authz-checked; no PII in logs` |
| `DECISION_RECORD` | Where decisions are logged (a doc section, a registry file) | `docs/Decisions.md` |
| `DOCS_SINKS` | Documentation targets ship fans out to | `README.md, docs/API.md` |

Optional keys — an unbound key means the lane step it gates is **skipped, not improvised**:

| Key | Meaning | Example |
|---|---|---|
| `DESIGN_ARTIFACTS` | *(design UX track)* the fork's conventions for design artifacts — what storyboards/mockups are, where they live, how they're authored; leave unbound to run spec-track only | `docs/mockups/ per its README` |
| `KNOWN_ENV_LIMITS` | *(verify)* declared environment limitations: the gate that can't run, its accepted substitute, and the report wording | `integration suite needs local MySQL — covered via Docker e2e` |
| `DEP_AUDIT_CMD` | dependency-vulnerability scanner; binds intake's per-pass sweep (one batch issue, never a per-issue gate) and audit's lockfile-diff check | `npm audit --json` · `pip-audit` · `cargo audit` |
| `MIGRATIONS_DIR` | schema-migrations directory; when a diff touches it, verify runs the migration checks and audit checks the diff shape | `db/migrations/` |
| `MIGRATE_DOWN_CMD` / `MIGRATE_UP_CMD` | *(with `MIGRATIONS_DIR`)* roll the newest migration(s) back / forward against a disposable DB | `dbmate down` / `dbmate up` · `alembic downgrade -1` / `alembic upgrade head` |
| `SCHEMA_DUMP` | *(with `MIGRATIONS_DIR`)* the committed schema dump the migration tool regenerates; must travel in the same diff as the migration | `db/schema.sql` |
| `DOCS_ROOT` | *(docs-tiers skill)* root of the L3 documentation tree | `docs/` |
| `DOC_DOMAINS` | *(docs-tiers skill)* thematic domain prefixes files route into | `Architecture_*, Testing_*, UserGuide_*` |
| `TOKEN_TOOL` | shell-output compactor — either an explicit prefix on every command, or a transparent shell wrapper (see `.github/agents/sdlc-worker.agent.md` for the two binding modes) | `tok` |

`INVARIANTS` is the one that most repays effort — it is the shared acceptance criterion build
implements to, verify exercises, and audit reviews for. Be specific and concrete.

Then fill the profile's variation-point lines and **known deviations** (skeleton in
[Development_SdlcComposability.md](Development_SdlcComposability.md#the-conformance-profile)). Worked examples:
[Development_SdlcProfileExample.md](Development_SdlcProfileExample.md) (the minimal single-repo GitHub case
with the reference CLI); the Azure DevOps examples (`ado-feature`, `ado-pbi`) live upstream in
agentic-sdlc under `docs/profiles/`.

## 4. The gh-issue deterministic core (recommended)

`sdlc/bindings/gh-issue/sdlc.mjs` (plain Node, no dependencies) ships in the copied tree. Adapt
the constants at its top — `DEFAULT_BRANCH`, `PROD_BRANCH`, and the worktree naming function —
and set `SDLC_CLI` in the profile. `sdlc worktree` junctions each new worktree's `node_modules`
to the main checkout's install (see the shared-install rule in `sdlc/README.md`); non-Node
projects simply get a `no-source` no-op. The prompts already route every claim, emit, gate, lock,
and dependency ritual through it wherever the binding says **CLI**.

Why bother: `advance` validates every transition against the stage graph, which kills the
hand-typed label-typo class (`stage:verfy`) by construction, and it makes the gate, machine
maintenance lock, and claim-verify race check deterministic — the agent supplies judgment (what to
spawn, what to write in comments), the CLI supplies the state math. The pure helpers are exported
and the gh/git executors injectable, so you can unit-test your adaptations without touching GitHub.

The dependency gate needs `gh` ≥ 2.86 (native issue-dependency fields in GraphQL) and a token
with `repo` scope; on a failure the `lanes` / `cycle-prep` report says `deps: edge query FAILED`
and that cycle degrades to the label-only gate rather than aborting.

### 4a. Migrate prose dependencies to native edges (on adoption, then every cycle)

Blocking is read from GitHub's **native issue dependencies** (see the binding's `labels.md`), so a
backlog whose dependencies live in prose (`Depends on #n`, `Blocked by #n`) and a hand-kept
`blocked` label is invisible to the gate until migrated. Run it by hand once on adoption to
review the proposals; afterwards `cycle-prep` runs the same pass every cycle (its
`=== deps-migrate ===` section, applied under `--apply` — the binding's `dep-migrate` op) so
prose on newly filed issues is converted without waiting for intake. The migration is a dry run
by default:

```bash
node sdlc/bindings/gh-issue/sdlc.mjs deps --migrate
```

It parses only **line-leading** declarations (`Depends on #12`, `- Blocked by: #13, #14`,
`**Requires** #15`, `After #16`) — narrative mentions such as "None depends on #1337" never
propose an edge — and prints one `#dependent blocked_by #blocker (OPEN|CLOSED)` line per proposed
edge (edges to closed blockers are harmless and make `ready` derivable), plus a `skipped` line for
a reference that isn't an issue in this repo (deleted, transferred, or cross-repo — native edges
are per-repo; keep those as `sdlc:hold` + prose). Review the list, then create the edges:

```bash
node sdlc/bindings/gh-issue/sdlc.mjs deps --migrate --apply
```

Then run the derived-label pass and read its lint:

```bash
node sdlc/bindings/gh-issue/sdlc.mjs deps --apply
```

`deps` rewrites `blocked` / `ready` from edge state (it does this on every `cycle-prep --apply`
from now on) and lists what it will not repair: `label-only-blocked` (a `blocked` label with no
edge — drop the label or add the edge by hand) and `cycle` (a dependency cycle — cut an edge).
Prose lines may stay as a human mirror; the edge is authoritative from here.

## 5. Lint ratchet for repos with a backlog (optional, tracker-neutral)

A lane gate of "`<LINT_CMD>` clean" is unenforceable when `<DEFAULT_BRANCH>` is already red: a
worker can't tell its own breakage from inherited errors, so the gate is either ignored or blocks
everything. `sdlc/tools/check-lint-baseline.mjs` is a per-rule-id **ratchet**: it grandfathers the
current ESLint error counts in a committed `sdlc/tools/lint-baseline.json` and fails only on
growth — counts may hold or shrink, never grow — and a rule id absent from the baseline (a new
error class) also fails. It resolves `eslint` from *your* repo (a devDependency there; the script
itself has no dependencies) and runs through the ESLint Node API with `cache: false`, so a stale
`.eslintcache` can't produce phantom counts on one host and not another.

1. Add `"lint:baseline": "node sdlc/tools/check-lint-baseline.mjs"` to `package.json` scripts.
2. From a clean, fully installed checkout of `<DEFAULT_BRANCH>`, seed the baseline:
   `npm run lint:baseline -- --update` and commit `sdlc/tools/lint-baseline.json`.
3. Add a CI step on every PR (deterministic across hosts — `npm ci` first):
   ```yaml
   - name: Lint ratchet — ESLint error counts must not grow
     run: npm run lint:baseline
   ```
4. Bind `LINT_CMD` to `npm run lint:baseline` in the profile and say so on its VP5 line.
   Build and verify then gate on **ratchet passes + touched files clean** (`npx eslint <files>`);
   `npm run lint` stays the interactive/human command and is expected to be red until the
   backlog burns down. Burn it down opportunistically in files you touch, then lock in the lower
   counts with `--update` (a re-baseline that *raises* a count is a review flag, not a fix).

Non-ESLint stacks: the pure helpers (`aggregateCounts` / `compareToBaseline` / `reportCheck`)
are generic — swap `lintRepo` for your linter's JSON output and keep the same baseline shape.

## 6. Choose your concurrency variant

- Small backlog / single tree → **serial** (see [Development_AgenticSDLC.md](Development_AgenticSDLC.md#concurrency-variants);
  simplify `dispatch.md` per the note there, and you can ignore the worktree steps).
- Throughput matters → keep the shipped **per-issue** model. Nothing to create up front: there is
  no dispatcher singleton — overlapping dispatch runs deconflict via per-issue claims, idempotent
  tracker writes, and a per-machine maintenance lock the dispatcher creates automatically at
  `.git/sdlc-maint.lock`. Just confirm your platform allows git worktrees.

Design is a **standard stage** — every item gets a reviewed implementation plan there (spec-lite
for small items), so the `stage:design` marker and the shipped `sdlc/lanes/design.md` worker are
part of the default pipeline. What you decide is whether to bind its **UX track**: bind
`DESIGN_ARTIFACTS` in your profile if your work is user-facing and worth storyboarding; leave it
unbound to run spec-track only.

## 7. Dry-run manually before scheduling

Paste `sdlc/README.md`, `sdlc/PROFILE.md`, your `BINDING.md`, and one lane file (start with
`lanes/intake.md`) into an agent session pointed at your repo. Feed it a real issue at
`stage:intake`. Watch it CLAIM, WORK, and EMIT. The prompt behaves identically whether a human or
a scheduler fired it — so a clean manual pass means the scheduled one will work. Walk one issue
all the way through intake → ship this way before automating.

## 8. Schedule the dispatcher

Register a recurring task whose body is a **thin pointer** to `dispatch.md`, e.g.:

> Read `<REPO_PATH>/sdlc/dispatch.md` (with `sdlc/PROFILE.md` and the bound
> `sdlc/bindings/<BINDING>/BINDING.md`) and execute one dispatch cycle for the `<PROJECT>`
> project: spawn one `<WORKER_AGENT>` subagent per non-empty lane as the file directs. The file is
> the canonical prompt; follow it exactly.

Keep the delegation cue ("spawn one `<WORKER_AGENT>` subagent per …") in the pointer itself:
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
git maintenance, one line per lane, queue depths, parked items, and **token cost per lane + cycle
total**. That token line is the trend to watch for cost regressions. Parked (`sdlc:needs-human`)
items are your action queue.

**Track which pipeline paths the chain has actually exercised.** A full-chain ADVANCE run proves
the happy path, but most BOUNCE/PARK/CONTINUE tails start out unproven — keep a short provenance
list (which issues proved which paths) in your pipeline doc, and exercise an unproven tail
manually before trusting it scheduled. Fold what those runs teach back into the worker prompts;
the prompts, not the list, stay canonical for behavior.

## 10. Staying current

Upstream sync is manual (see [Development_SdlcComposability.md](Development_SdlcComposability.md#distribution-model-hybrid-fork-per-project)).
Because core and binding files are copied unedited, a re-sync is a plain overwrite of
`sdlc/README.md`, `sdlc/lanes/`, `sdlc/dispatch.md`, and `sdlc/bindings/` from a newer tag, then
bumping `SPEC_VERSION` — your profile and any declared deviations are the only local state. Diff
the profile against the new `PROFILE.md` template for keys added since your version.
