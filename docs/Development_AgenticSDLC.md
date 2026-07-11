# Development_AgenticSDLC.md

If you adopt the `prompts/sdlc/` pipeline (see [prompts/sdlc/README.md](../prompts/sdlc/README.md)
for the stage graph and worker loop), document here:

- How the dispatcher is scheduled (cron, CI, Claude Code scheduled task) and its cadence.
- Any project-specific bounce/park rules beyond the generic lane templates.
- What's been proven to actually work end-to-end vs. what's still untested, so future changes
  know which tails are load-bearing.

## The concurrent variant

The pipeline is **per-issue concurrent**: locking is per-issue (the `sdlc:wip` label plus a
`sdlc:claim <run-id> <lane>` ownership comment, race-checked by `claim --verify`), and each
branch-touching worker operates in its own issue-scoped git worktree (`../<repo>-wt-<issue#>`) —
git's one-checkout-per-branch rule is a second lock layer. So lane workers run in parallel; a
fresh wip lock makes only that one issue ineligible for a cycle, never aborting the run. The
dispatcher itself is a singleton, serialized through a **pinned issue titled
`sdlc:dispatch-lock`** (label it `sdlc:hold` so no worker claims it) — create and pin that issue
as a one-time setup step before scheduling the dispatcher.

## The `sdlc` CLI — deterministic label/branch one-shots

[scripts/sdlc.mjs](../scripts/sdlc.mjs) (`npm run sdlc <cmd>`) holds the deterministic state
math; agents supply judgment and comment bodies.

Worker-side:

- `claim <issue> [<run-id> <lane>] [--verify]` — add `sdlc:wip` + the claim comment; `--verify`
  runs the race check and exits non-zero on a lost race.
- `advance <issue> <to-stage>` — validate the transition against the stage graph, swap the
  stage label, drop `sdlc:wip`.
- `context <issue>` — branch + status + issue labels/state + open PRs.
- `worktree <issue> [<branch>]` — add (or reuse) the issue's sibling worktree.
- `comment <issue> <file>` — post a body-file comment (plumbing only).

Dispatcher-side:

- `gate [--reap]` — per-issue wip-lock ages (from the labeled timeline event, not `updatedAt`)
  → LIVE / REAP / CLEAR.
- `lock <run-id>` / `unlock <run-id>` — the dispatcher singleton mutex on the pinned
  `sdlc:dispatch-lock` issue; `lock` exits non-zero if held fresh, supersedes a stale (≥2h) one.
- `lanes` — per-lane depth + CLAIM-ordered eligibility + the stage-label integrity check.
  Integrity rule: **multiple** stage labels are always corrupt; **zero** stage labels is a
  legitimate state (post-ship awaiting merge, the dispatch-lock issue, not yet in the pipeline)
  and only corrupt when the issue still carries `sdlc:wip` or `sdlc:needs-human`.
- `heal [<lane>] <issue>` — post-worker self-heal: did the worker clear its lock?
- `git-maint` — fetch + prune, ff the integration branch, prune ancestry/squash-merged branches
  (three-way safety check), read-only open-PR state.
- `digest [--state <file>]` — queue depths, parked/hold lists, arrivals-diff vs last cycle.

This file is intentionally close to a stub in the template — fill it in once the pipeline is
running against real issues.
