# Development_AgenticSDLC.md

If you adopt the `sdlc/` pipeline (see [sdlc/README.md](../sdlc/README.md)
for the stage graph and worker loop), document here:

- How the dispatcher is scheduled (cron, CI, Claude Code scheduled task) and its cadence.
- Any project-specific bounce/park rules beyond the generic lane templates.
- What's been proven to actually work end-to-end vs. what's still untested, so future changes
  know which tails are load-bearing.

> **Upstream pin:** `meridun/model-repo` **21c127c** (2026-10-10; model-repo is the canonical
> spec, agentic-sdlc retired). To re-sync, diff model-repo's `sdlc/`, `test/sdlc.test.mjs`,
> `.github/agents/sdlc-worker.agent.md`, `docs/Development_Sdlc*.md` against ours, then bump this
> pin and the one in [sdlc/PROFILE.md](../sdlc/PROFILE.md). Local adaptations to preserve: see
> **Known deviations from spec** in the profile.

## The concurrent variant

The pipeline is **per-issue concurrent**: locking is per-issue (the `sdlc:wip` label plus a
`sdlc:claim <run-id> <lane>` ownership comment, race-checked by `claim --verify`), and each
branch-touching worker operates in its own issue-scoped git worktree (`C:\Claude\pemr-wt-<issue#>`) —
git's one-checkout-per-branch rule is a second lock layer. So lane workers run in parallel; a
fresh wip lock makes only that one issue ineligible for a cycle, never aborting the run. There is
**no dispatcher singleton**: any number of dispatch runs may execute concurrently; they
deconflict via per-issue claims, a per-machine maintenance lock (`.git/sdlc-maint.lock`), and
idempotent GitHub writes. (The old pinned `sdlc:dispatch-lock` issue #2 is retired.)

## Three orthogonal vocabularies — do not confuse them

Three axes live on an issue at once. Each has its own label namespace so a reader never mistakes
one for another:

| Vocabulary | Question it answers | Values | Owner |
|---|---|---|---|
| Engineering readiness | _Can this issue be worked yet?_ | `ready` · `blocked` — derived from native dependency edges by `sdlc deps --apply` | GitHub's *blocked by* edges ([labels.md](../sdlc/bindings/gh-issue/labels.md)) |
| Design maturity | _How settled is the idea?_ | proposed · building · live | the design pipeline |
| **Queue position** | _Which lane's worker touches it next?_ | `stage:*` labels | this pipeline |

Queue position is a textual `stage:` namespace, no emoji, so it never visually collides with an
icon-rendered maturity or readiness axis. Edges, not labels or prose, are authoritative for
readiness: see [sdlc/README.md § Dependencies](../sdlc/README.md#dependencies--edges-never-prose-or-markers).

## Where artifacts live — branches are created lazily

A worker creates a branch only when it first has something to commit. Stages own different
branches with different lifecycles:

| Stage | Commits? | Branch | Merges to `dev` |
|---|---|---|---|
| intake | no — reads, relabels, edits issue-body sections | — | — |
| design | only when the UX track runs (storyboards, design-index edits; the plan lives in the issue body) | `docs/<issue#>-design` | fast, at the design→queued seam |
| build | yes — the implementation | `feat/<issue#>-<slug>` off `dev` | at ship, via the PR |

Two branches, not one, because the audiences differ. Design artifacts are shared reference and
cannot ride an unmerged feature branch for the life of a build, so they merge on their own
short-lived docs branch. The build branch is cut afterward and already contains the merged
design. Design-exempt items get their first branch at build.

Items built **outside** the pipeline have no branch. Intake routes them to the earliest lane whose
artifact is genuinely missing (floor `stage:verify`), and each downstream lane carries a
**no-branch fallback**: verify validates on `dev` and names the introducing commit, audit
reconstructs the change diff from that commit, and ship cuts a fresh branch for only the
still-missing docs/tests (see [verify.md](../sdlc/lanes/verify.md),
[audit.md](../sdlc/lanes/audit.md), [ship.md](../sdlc/lanes/ship.md)).

## Orchestration rules (ported from upstream 89aafaf)

- **Bounded bounce loops:** two same-class bounces between the same pair of lanes → the third
  pass PARKs (`sdlc:needs-human`) instead of bouncing again.
- **Spawn lane workers in the background:** a foreground timeout kills promoted child processes.
  Pull results; never re-spawn a worker just to collect its result.
- **Model routing:** use concrete model tiers, not provider aliases (alias resolution is
  account-dependent). Keep Fable-class models **off the audit lane** — safety classifiers can
  refuse benign defensive-security review.

## Manual ↔ scheduled (zero rewrite)

The three run modes (scheduled, focused, manual) are described in
[sdlc/README.md § How to run](../sdlc/README.md#how-to-run). All of them run the **identical**
lane prompt body, so a manual paste becomes a scheduled job with no rewrite, and a scheduled job
is debugged by pasting the same file into a session. What that buys here:

- **Structured results.** Every worker ends its reply with the fenced JSON result block
  (`{issue, outcome, next_stage, notes}`); self-heal and the digest read it instead of parsing
  prose. A missing or malformed block is a recorded contract violation with prose fallback.
- **Focused cycles pay every global ritual.** [sdlc/focus.md](../sdlc/focus.md) filters only the
  lanes section (`cycle-prep --issue <N>`); maintenance, locks, integrity check, self-heal and
  digest still run, so a focused run never leaves the rest of the pipeline's bookkeeping behind.
- **Manual is the proving mode.** Claims deconflict per issue, so manual and scheduled runs
  coexist. Exercise an unproven tail by hand before trusting it to the clock.

The stage-label integrity check is a hand-edit backstop, not a duplicate of the CLI's transition
validation: the CLI never creates a zero- or multi-stage issue, but hand edits and outside tooling
can. Repair rules: [sdlc/dispatch.md § Step 0b](../sdlc/dispatch.md#step-0b--stage-marker-integrity-you-do-this-yourself).

## The `sdlc` CLI — deterministic label/branch one-shots

[sdlc/bindings/gh-issue/sdlc.mjs](../sdlc/bindings/gh-issue/sdlc.mjs) (`npm run sdlc <cmd>`) holds the deterministic state
math; agents supply judgment and comment bodies. It is the upstream reference CLI with pemr
constants (`DEFAULT_BRANCH = 'dev'`, `PROD_BRANCH = 'main'`). Pure planners are covered by
`npm test` ([test/sdlc.test.mjs](../test/sdlc.test.mjs), zero-dep `node:test`, 211 tests).

**Why it exists.** The recurring part of the worker loop is not judgment; it is the same
label-swap and git-context dance every pass, and a hand-typed `stage:verfy` silently corrupts a
lane. Two design rules carry the safety load. Each command's core is a **pure function**, exported
and unit-tested with the `gh`/git executors injected, so dispatch logic is verified with no side
effects. And the core **throws rather than guesses**: zero, multiple, or unknown lane labels route
to a human instead of being interpreted.

Worker-side:

- `claim <issue> [<run-id> <lane>] [--verify]` — add `sdlc:wip` + the claim comment; `--verify`
  runs the race check and exits non-zero on a lost race.
- `claim --next <lane> <run-id>` — CLI picks the next eligible issue for the lane and claims it,
  with lost-race retry; exits `idle` on an empty lane. The pick rule lives in code so workers stop
  eyeballing priority/FIFO order.
- `emit <issue> <run-id> <outcome>` — post the machine-parseable `sdlc:emit <run-id> <OUTCOME>`
  comment **and** do the label math in one shot (marker-before-label-math; refuses if the claim
  isn't owned by `<run-id>`). It is a worker's only legal finish. The claim boundary is the newest
  `sdlc:wip` *unlabeled* timeline event (emit marker as fallback), never a regex over prose: a
  prose-only outcome leaves the claim looking live, and every later claimer loses a phantom race.
- `advance <issue> <to-stage>` — validate the transition against the stage graph, swap the
  stage label, drop `sdlc:wip`. An illegal jump or misspelled target exits non-zero with no
  mutation; removes precede adds so a lane is never momentarily label-less.
- `context <issue>` — branch + status + issue labels/state + open PRs.
- `worktree <issue> [<branch>]` — add (or reuse) the issue's sibling worktree; junctions its
  `node_modules` to the main checkout's install.
- `comment <issue> <file>` — post a body-file comment (plumbing only).
- `dup-check "<keywords>" [--exclude <issue#>]` — rank open-issue dup candidates; judgment stays
  with the caller. Deterministic scorer: title hit 3, body hit 1, label-token hit +1, ties by
  issue number. Exit code is the contract (**0** clean, **2** candidates, **1** usage error). The
  query feeds only the pure scorer and never reaches a shell; copy that for any future search
  subcommand.

Dispatcher-side:

- `cycle-prep [--apply] [--issue <N>]` — the whole pre-dispatch sequence (mint → maint-lock →
  lanes → gate --reap → deps-migrate → deps → sweep → git-maint → node-modules → worktree-sweep →
  conflict-scan → maint-release) in one delimited, machine-readable report. Prefer this over
  running the pieces by hand. It is a **composer, not a reimplementation**: each section invokes
  the standalone subcommand, so invariants and tests carry over. Maintenance sections run only
  while this run holds the maintenance lock, released in a `finally`. It cannot run inside a
  worktree (the lock lives under a real `.git`), so the dispatcher runs it in the main checkout.
  `--issue <N>` trims only the eligible/blocked lists to #N and prints one `focus #N:` verdict;
  depths, ineligibility breakdowns, and the integrity list stay global.
- `mint` — coin + print this cycle's run-id.
- `gate [--reap]` — per-issue wip-lock ages (from the labeled timeline event, not `updatedAt`)
  → LIVE / REAP / CLEAR; `--reap` re-verifies each stale lock against live data before writing.
- `maint-lock <run-id>` / `maint-release <run-id>` — per-machine maintenance lock
  (`.git/sdlc-maint.lock`); exit 1 = held: skip git maintenance, never abort the cycle. GitHub
  writes are idempotent and deconflict themselves; the filesystem does not, so the lock covers
  only the local half.
- `lanes [--issue <N>]` — per-lane depth + CLAIM-ordered eligibility (with ineligibility breakdown) + the
  stage-label integrity check. Integrity rule: **multiple** stage labels are always corrupt;
  **zero** stage labels is a legitimate state (post-ship awaiting merge, not yet in the
  pipeline) and only corrupt when the issue still carries `sdlc:wip` or `sdlc:needs-human`.
- `deps [--apply]` / `deps --migrate [--apply]` — derive `blocked`/`ready` from native dependency
  edges and lint labels with no edge behind them; `--migrate` turns prose `Depends on #n` lines
  into edges once. Labels are derived, prose is a mirror, edges are the truth.
- `heal [<lane>] [<issue>]` — post-worker self-heal: did the worker clear its lock? Lane-only
  form auto-discovers the issue.
- `git-maint` — fetch + prune, ff the integration branch, prune ancestry/squash-merged branches
  (three-way safety check), read-only open-PR state.
- `node-modules [--apply]` — keep the shared install honest after a dependency-changing merge.
  Drift probe: sha256 of the main checkout's `package-lock.json` against
  `node_modules/.sdlc-install-stamp`. Drift runs a plain `npm install` in the main checkout; never
  `npm ci` (its recursive delete next to junctioned worktrees can empty the shared install);
  skipped while `package.json`/`package-lock.json` are dirty; a failed install is one
  `node-modules: install FAILED (…)` line, never a failed cycle.
- `worktree-sweep [--apply]` — remove clean issue-scoped worktrees whose branch is gone/merged
  or issue closed. Positive "landed" signals only, so a fresh unpushed branch is never swept; an
  unreadable tree counts as dirty. The `pemr-wt-<issue#>` digit-only name pattern is the
  destructive path's safety boundary. The `node_modules` junction is unlinked **before**
  `git worktree remove`. A tree whose only entries are 0-byte untracked strays counts as clean;
  any tracked change or untracked file with content stays dirty.
- `conflict-scan [--apply]` — comment + bounce-to-build issues whose open PR conflicts with the
  integration branch, watermarked so a re-nudge only fires after the base moves again. It never
  merges, updates, or closes a PR.
- `sweep [--state <file>] [--ack]` — read-only merge-sweep work-list for intake step 0;
  `--ack` (run **after** processing) marks merges swept. A worker that dies in between re-lists
  the same closes next pass: at-least-once delivery paired with idempotent edits.
- `digest [--state <file>]` — queue depths, parked/hold lists, arrivals-diff vs last cycle.

## Lessons from production runs

Observed upstream (model-repo and its sources); each line is a failure and the rule it produced.
The lanes, not this list, are canonical for behavior: every lesson is already folded into them.

- A worker spawned a subagent to do its stage, which ran detached and stranded the item → workers
  get no agent-spawning tool; "owner skill X" means apply X's checklist inline.
- Lock age read from `updatedAt` made a stale lock look fresh after an unrelated comment → age
  comes from the `sdlc:wip` `labeled` timeline event only.
- A prose outcome comment left a finished worker's claim looking live → the claim boundary is the
  `sdlc:wip` unlabeled event, and `emit` is the only legal finish.
- A worker hand-picked the wrong "next" issue → the pick rule moved into `claim --next`.
- A worktree sweep recursed through a `node_modules` junction and emptied the shared install →
  unlink the junction before `git worktree remove`; never install inside a worktree.
- A 0-byte stray file kept a finished worktree "dirty" forever → strays are classified, not
  guessed at.
- Blocking lived in prose and labels, and a regex read "does not depend on ..." as an edge →
  native dependency edges became the source of truth.
- A conflicted PR was re-nudged every cycle → watermark the nudge against the integration
  branch's tip.
- A dispatcher singleton let an overrunning cycle abort the next one wholesale → per-issue claims
  plus a per-machine maintenance lock replaced it.
- The dispatcher missed issue numbers in freeform worker replies → workers end with a fenced JSON
  result block, and lane-level self-heal auto-discovers stalled items.
- An item ping-ponged between two lanes → the third same-class bounce parks for a human.
- Hand-edited labels produced zero- and dual-stage issues the CLI never creates → the integrity
  check stays as a runtime backstop.

Upstream's chain runs proved the full spine end to end on one item (every ADVANCE, plus PARK from
design and verify) and the outside-pipeline entry with no-branch fallbacks on another. Most BOUNCE
tails and build's CONTINUE stay thinly exercised; run those by hand here before trusting them to
the clock.

This file is intentionally close to a stub in the template — fill it in once the pipeline is
running against real issues.
