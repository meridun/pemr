# SDLC worker prompts

Executable artifacts (not reference docs) for an issue-driven **Agentic SDLC pipeline**: a chain
of subagent "workers", one per pipeline stage, each doing one pass over one GitHub issue and then
stopping. State lives entirely on the issue (labels + comments) — workers share no context with
each other or with whatever dispatched them.

This is a **template** — fill in the project name, adjust the stage list, and replace the
`proj-*` skill/agent names with your own before relying on it.

## Stage graph

```
intake → design → queued → build → verify → audit → ship
```

- `stage:<lane>` labels track position in the pipeline.
- `sdlc:wip` is the per-item lock a worker sets on CLAIM and clears on EMIT.
- `sdlc:needs-human` parks an item for a human decision; `sdlc:hold` is a human keep-off flag.
  Workers never touch either.
- `stage:queued` is intentionally workerless — the human throttle between design and build.

The deterministic label/branch mechanics have one-shots in
[`scripts/sdlc.mjs`](../../scripts/sdlc.mjs) (`npm run sdlc claim|advance|context|worktree|…`) —
`advance` validates the transition against the stage graph so illegal jumps and label typos are
rejected by construction. Comment/report **bodies** stay agent judgment; `sdlc comment` is a thin
plumbing wrapper that posts a body the agent already authored.

## How to run

- **Scheduled**: a dispatcher (see [`dispatch.md`](dispatch.md)) runs periodically: a dispatcher
  singleton gate, a per-issue wip gate (stale-lock reaping only — a fresh lock just makes that
  one issue ineligible), git + worktree maintenance, then one worker subagent per non-empty
  lane. Locking is per-issue (claim comments, below), so lane workers may run concurrently.
- **Manual**: paste this README plus a lane file's body into an agent session. Identical
  behavior — the prompt doesn't know what fired it. Mint your own run-id for the claim comment;
  manual and scheduled runs coexist safely because claims deconflict per-issue.

## Universal worker loop (binding)

1. **CLAIM** — list open issues labeled `stage:<lane>` that are **not** labeled `sdlc:wip`,
   `sdlc:needs-human`, or `sdlc:hold`. Pick the next by priority then FIFO. If none → reply
   `<LANE>: idle` and stop. Then take the lock, **before** doing anything else:
   1. `npm run sdlc -- claim <issue> <run-id> <lane> --verify` — adds `sdlc:wip`, posts the claim
      comment `sdlc:claim <run-id> <lane>` (run-id = the dispatcher-supplied id, or any unique id
      you mint for a manual run), then runs the claim-verify race check. The label is the
      visibility signal; the claim comment is the ownership record and tiebreaker (earliest
      claim newer than the last outcome EMIT wins; ties break to the lexicographically lower
      run-id).
   2. **Lost the race** (the command exits non-zero and says so) → leave the label and the
      winner's claim untouched, delete nothing, and go pick the next eligible item.
2. **WORK** — per the lane file, with these constraints:
   - **Never delegate** — do all work inline, yourself. Don't spawn subagents or background
     tasks; a scheduled worker that yields mid-task strands the item under `sdlc:wip` forever.
     Where a lane names a role (`verifier`, `security-executor`, …) it names the **stance and
     checklist you apply inline**, not a subagent to dispatch. Cost-tiering happens one level
     up: the dispatcher sets each worker's `model` per lane (see `dispatch.md`).
   - **Idempotent** — if the stage's artifact already exists, treat as done; don't redo it.
   - **Worktree isolation** — never work in the main checkout; it may hold human WIP or another
     worker. For any lane that touches a branch, use the issue-scoped worktree
     `../<repo>-wt-<issue#>`: `npm run sdlc worktree <issue> [<branch>]` creates it (or reuses
     the branch if it exists); reuse the worktree if already present. Git's
     one-checkout-per-branch rule across worktrees is a second lock layer: if `worktree add`
     fails because the branch is checked out elsewhere, treat it as a lost claim race — release
     per CLAIM step 2 and move on. Read-only lanes (intake, audit) may skip the worktree.
   - **Refresh from your integration branch (staleness rule).** On entering the worktree:
     `git fetch origin`. If the upstream side of `git diff --name-only HEAD...origin/<integration>`
     intersects the paths this branch touches, merge it in (merge, never rebase — branches are
     pushed and handed between workers). No overlap → record "advanced, no path overlap" and do
     not merge, so existing verify/audit reports stay valid. **Conflict ownership:** build
     resolves merge conflicts; verify and audit never do — a conflicted merge there is a BOUNCE
     → `stage:build` naming the conflicting paths. Ship always merges (the PR must be
     mergeable) and may resolve docs-only conflicts itself; code conflicts BOUNCE to build.
   - **Tree hygiene** — record the entry branch before any switch and restore it before EMIT.
     Never touch the production branch. Never stash/discard uncommitted human work; if it
     genuinely blocks the work, PARK.
   - If a codebase knowledge graph exists (e.g. `graphify-out/graph.json`), query it before
     reading raw source.
3. **EMIT exactly one outcome** — ADVANCE, BOUNCE, or PARK — never silent. Every outcome removes
   `sdlc:wip` on the way out. **Leave the worktree in place** — dispatcher maintenance prunes
   worktrees for merged/dead branches, and a reaped issue's next worker reuses it.
4. **STOP** — reply the lane's one-line result. One item per pass; never pick up a second.

## Files

| File | Stage | Notes |
|---|---|---|
| [`dispatch.md`](dispatch.md) | *(dispatcher)* | Runs every lane once per cycle |
| [`intake.md`](intake.md) | `stage:intake` → `stage:design` or `stage:queued` | Triage: coherent, in scope, non-dup |
| [`design.md`](design.md) | `stage:design` → `stage:queued` | Settle UX/approach before build |
| [`build.md`](build.md) | `stage:build` → `stage:verify` | Implement |
| [`verify.md`](verify.md) | `stage:verify` → `stage:audit` | Tests + a real run |
| [`audit.md`](audit.md) | `stage:audit` → `stage:ship` | Review for correctness/security |
| [`ship.md`](ship.md) | `stage:ship` → *(closed on merge)* | Open the PR, update docs |

`intake` additionally runs a **merge sweep** every pass (its step 0): post-merge cleanup and
cascade-unblock for issues closed by merged PRs, since ship ends at "PR open" and the merge
itself fires no worker.

Fill in each lane file's specifics for your project — the ones in this directory are minimal
templates showing the shape, not production-ready prompts.
