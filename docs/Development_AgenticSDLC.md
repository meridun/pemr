# Development_AgenticSDLC.md

If you adopt the `prompts/sdlc/` pipeline (see [prompts/sdlc/README.md](../prompts/sdlc/README.md)
for the stage graph and worker loop), document here:

- How the dispatcher is scheduled (cron, CI, Claude Code scheduled task) and its cadence.
- Any project-specific bounce/park rules beyond the generic lane templates.
- What's been proven to actually work end-to-end vs. what's still untested, so future changes
  know which tails are load-bearing.

Upstream source: `C:\Claude\agentic-sdlc` — last resynced **2026-08-06** at upstream `3e0db2d`
(prompts, CLI, tests; placeholder bindings live in
[prompts/sdlc/PROFILE.md](../prompts/sdlc/PROFILE.md)).

## The concurrent variant

The pipeline is **per-issue concurrent**: locking is per-issue (the `sdlc:wip` label plus a
`sdlc:claim <run-id> <lane>` ownership comment, race-checked by `claim --verify`), and each
branch-touching worker operates in its own issue-scoped git worktree (`../pemr-wt-<issue#>`) —
git's one-checkout-per-branch rule is a second lock layer. So lane workers run in parallel; a
fresh wip lock makes only that one issue ineligible for a cycle, never aborting the run. There is
**no dispatcher singleton**: any number of dispatch runs may execute concurrently; they
deconflict via per-issue claims, a per-machine maintenance lock (`.git/sdlc-maint.lock`), and
idempotent GitHub writes. (The old pinned `sdlc:dispatch-lock` issue #2 is retired.)

## Orchestration rules (ported from upstream 89aafaf)

- **Bounded bounce loops:** two same-class bounces between the same pair of lanes → the third
  pass PARKs (`sdlc:needs-human`) instead of bouncing again.
- **Spawn lane workers in the background:** a foreground timeout kills promoted child processes.
  Pull results; never re-spawn a worker just to collect its result.
- **Model routing:** use concrete model tiers, not provider aliases (alias resolution is
  account-dependent). Keep Fable-class models **off the audit lane** — safety classifiers can
  refuse benign defensive-security review.

## The `sdlc` CLI — deterministic label/branch one-shots

[scripts/sdlc.mjs](../scripts/sdlc.mjs) (`npm run sdlc <cmd>`) holds the deterministic state
math; agents supply judgment and comment bodies. It is the upstream reference CLI with pemr
constants (`DEFAULT_BRANCH = 'dev'`, `PROD_BRANCH = 'main'`). Pure planners are covered by
`npm test` ([test/sdlc.test.mjs](../test/sdlc.test.mjs), zero-dep `node:test`, 130 tests).

Worker-side:

- `claim <issue> [<run-id> <lane>] [--verify]` — add `sdlc:wip` + the claim comment; `--verify`
  runs the race check and exits non-zero on a lost race.
- `claim --next <lane> <run-id>` — CLI picks the next eligible issue for the lane and claims it,
  with lost-race retry.
- `emit <issue> <run-id> <outcome>` — post the machine-parseable `sdlc:emit <run-id> <OUTCOME>`
  comment **and** do the label math in one shot (marker-before-label-math; refuses if the claim
  isn't owned by `<run-id>`).
- `advance <issue> <to-stage>` — validate the transition against the stage graph, swap the
  stage label, drop `sdlc:wip`.
- `context <issue>` — branch + status + issue labels/state + open PRs.
- `worktree <issue> [<branch>]` — add (or reuse) the issue's sibling worktree.
- `comment <issue> <file>` — post a body-file comment (plumbing only).
- `dup-check "<keywords>" [--exclude <issue#>]` — rank open-issue dup candidates (exit 2 if
  any); judgment stays with the caller.

Dispatcher-side:

- `cycle-prep [--apply]` — the whole pre-dispatch sequence (mint → maint-lock → lanes →
  gate --reap → sweep → git-maint → worktree-sweep → conflict-scan → maint-release) in one
  delimited, machine-readable report. Prefer this over running the pieces by hand.
- `mint` — coin + print this cycle's run-id.
- `gate [--reap]` — per-issue wip-lock ages (from the labeled timeline event, not `updatedAt`)
  → LIVE / REAP / CLEAR; `--reap` re-verifies each stale lock against live data before writing.
- `maint-lock <run-id>` / `maint-release <run-id>` — per-machine maintenance lock
  (`.git/sdlc-maint.lock`); exit 1 = held: skip git maintenance, never abort the cycle.
- `lanes` — per-lane depth + CLAIM-ordered eligibility (with ineligibility breakdown) + the
  stage-label integrity check. Integrity rule: **multiple** stage labels are always corrupt;
  **zero** stage labels is a legitimate state (post-ship awaiting merge, not yet in the
  pipeline) and only corrupt when the issue still carries `sdlc:wip` or `sdlc:needs-human`.
- `heal [<lane>] [<issue>]` — post-worker self-heal: did the worker clear its lock? Lane-only
  form auto-discovers the issue.
- `git-maint` — fetch + prune, ff the integration branch, prune ancestry/squash-merged branches
  (three-way safety check), read-only open-PR state.
- `worktree-sweep [--apply]` — remove clean issue-scoped worktrees whose branch is gone/merged
  or issue closed.
- `conflict-scan [--apply]` — comment + bounce-to-build issues whose open PR conflicts with the
  integration branch, watermarked so a re-nudge only fires after the base moves again.
- `sweep [--state <file>] [--ack]` — read-only merge-sweep work-list for intake step 0;
  `--ack` (run **after** processing) marks merges swept.
- `digest [--state <file>]` — queue depths, parked/hold lists, arrivals-diff vs last cycle.

This file is intentionally close to a stub in the template — fill it in once the pipeline is
running against real issues.
