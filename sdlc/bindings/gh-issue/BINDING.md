# Binding: `gh-issue` — GitHub issues + PRs, single repo

The template default. Distilled from three production single-repo pipelines. Fills the contract
in [`../README.md`](../README.md).

## 1. Unit of work

The **GitHub issue**. Identified by number (`#<n>`); `<issue#>` in branch and worktree names is
that number. Single-repo topology: the issue is simultaneously the Feature and its only child —
no hierarchy links, no routing markers, no contract-ready handshake (`docs/Development_SdlcComposability.md` VP2).

## 2. Spine form

**Collapsed tail.** Ship opens the PR (`Closes #<n>`); the human merge *is* the `ready` gate and
`shipping → complete` fold into merge-and-close. Ship's terminal ADVANCE removes `stage:ship`.

## 3. Marker substrate — labels

Every marker is a **label**; the taxonomy and a `gh` script to create them are in
[`labels.md`](labels.md). Every open issue carries **exactly one** `stage:*` label — the dispatcher
enforces it (Step 0b; `stage-repair` / `park`). Priority order: `priority:critical` ›
`priority:medium` › `priority:future` › unlabeled, then FIFO by `createdAt`. `blocked` / `ready`
are optional **derived** labels (`readiness-derive`) the machine never reads.

Label ops are set operations — applying the same change twice converges — which is what makes
every dispatcher write here idempotent.

## 4. Lock substrate — append-only, server-timestamped

The lock is `sdlc:wip` (visibility) **plus a claim comment** `sdlc:claim <run-id> <lane>` (the
ownership record and race tiebreaker; GitHub stamps it server-side). Because the record is
append-only, a **claim-verify** ritual resolves contention:

> After posting your claim, re-fetch the issue's comments. If another `sdlc:claim` comment on this
> issue is newer than the **settled/live boundary** and predates yours (or ties with a
> lexicographically lower run-id), you lost the race — leave the label and the winner's claim
> untouched, delete nothing, and go pick the next eligible item. Only the losing worker's own
> claim comment may be edited, to note `(superseded)`.

**The boundary** is the issue's most recent `sdlc:wip` *unlabeled* timeline event (every outcome
EMIT removes `sdlc:wip`, and a reaper strip correctly invalidates earlier claims), with the
`sdlc:emit` marker comment as the fallback boundary signal when the timeline is unavailable.

**Age and owner for the reaper** come from the newest `sdlc:claim` comment's server timestamp and
run-id — never `updatedAt` (any comment resets it). A bare `sdlc:wip` label with no claim comment
is aged by its `labeled` event in the issue timeline
(`gh api repos/<owner>/<repo>/issues/<n>/timeline`); if that event can't be found, the age is
**unprovable** — leave it and record it.

## 5. Evidence locations

- **Artifact sections** (`## Requirements`, `## Acceptance criteria`, `## Design`,
  `## Implementation plan`) live in the **issue body**, appended below the original author text,
  one owner per section (`write-section` = `gh issue edit <n> --body-file`, splicing only your
  section).
- **Comment channel** = issue comments: claims, emits, PARK checklists, routing summaries, verify
  report, audit report, ship summary + PR link.

## 6. Dependency model — native issue dependencies

An edge is GitHub's native *blocked by* relation (REST
`issues/{n}/dependencies/blocked_by`, GraphQL `blockedBy` / `blocking`). Writing one takes the
blocker's numeric **id**, not its number:

```bash
BLOCKER_ID=$(gh api repos/{owner}/{repo}/issues/<blocker#> --jq .id)
gh api -X POST repos/{owner}/{repo}/issues/<dependent#>/dependencies/blocked_by -F issue_id=$BLOCKER_ID
```

Reading edges: one GraphQL pass over `repository.issues(states:OPEN)` with `blockedBy { number
state }` — the `blocked-by:` search qualifier does **not** return results, so never use
`gh issue list --search` for this. Needs `gh` ≥ 2.86 and a `repo`-scoped token; on a failed edge
query the cycle degrades to the label-only gate (the CLI report says `deps: edge query FAILED`)
rather than aborting. Native edges are **per-repo**: a cross-repo blocker is `sdlc:hold` + a prose
line. Existing prose dependencies migrate once with `sdlc deps --migrate` (`docs/Development_SdlcAdoption.md`).

## 7. Code host — GitHub PRs

`pr-list` = `gh pr list --state open --json number,title,headRefName,mergeable,reviewDecision,isDraft,files`;
`pr-state <branch>` = `gh pr view <branch> --json state,headRefOid`; `pr-open` = `gh pr create
--base <DEFAULT_BRANCH> --head <branch>` with `Closes #<n>` in the body. A merged PR auto-closes
the issue; the dispatcher's gate unblocks dependents from edge state on the next cycle.

## 8. Deterministic core — `sdlc.mjs`

[`sdlc.mjs`](sdlc.mjs) (plain Node, zero dependencies) is the reference CLI; `<SDLC_CLI>` =
`node sdlc/bindings/gh-issue/sdlc.mjs`, written `sdlc …` below. It owns every row marked **CLI**
in the table: transition validation against the stage graph, the claim-verify race check, the
emit marker + label math, the wip gate and maintenance lock, the edge-based eligibility gate and
derived readiness labels, the close-sweep work-list/ack, and the whole `cycle-prep` report. Adapt
the constants at its top (`DEFAULT_BRANCH`, `PROD_BRANCH`, `worktreeName`). Pure helpers are
exported and the `gh`/`git` executors injectable, so adaptations are unit-testable without GitHub
(`test/sdlc.test.mjs`). It resolves the repo root as three directories above itself — keep it at
`sdlc/bindings/gh-issue/` in the adopting repo, or adjust `runSdlc`'s `root` default.

## 9. Operation table

**CLI** = the deterministic core's one-shot (use it whenever present). **Manual** = the CLI-less
fallback, normative for what the op must do.

| Op | CLI | Manual |
|---|---|---|
| `snapshot` | `sdlc lanes` (per-lane depth, CLAIM-ordered eligibility with an ineligibility breakdown, blocked list, ≠1-stage list) — or the `=== lanes ===` section of `sdlc cycle-prep` | `gh issue list --state open --json number,labels,createdAt --limit 200` + the GraphQL edge pass (§6) |
| `read <issue>` | `sdlc context <issue>` (branch, status, labels, PRs) + `gh issue view <n> --json body,comments` | same |
| `history <issue>` | — | `gh issue view <n> --json comments`; filter first lines `sdlc:claim …` / `sdlc:emit …`; timeline for the boundary (§4) |
| `dep-read` | inside `sdlc lanes` / `cycle-prep` | GraphQL pass (§6) |
| `dup-search <kw>` | `sdlc dup-check "<kw>" [--exclude <n>]` — ranks open issues by keyword overlap; exit 2 = candidates, 0 = clean | `gh issue list --search "<kw>" --state all --limit 30 --json number,title,state` (also the wider net across closed issues) |
| `in-flight <stage>` | from `sdlc lanes` | `gh issue list --label stage:<stage> --json number,title` |
| `closed-since` | `sdlc sweep` (read-only work-list from edges; `sweep: clear` = nothing) | GraphQL over `issues(states:CLOSED)` with `blocking { number state }` and each dependent's `blockedBy` |
| `pr-list` / `pr-state` | inside `sdlc git-maint` / `conflict-scan` | §7 |
| `lock-age <issue>` | `sdlc gate` (LIVE / REAP / CLEAR per wip issue, timeline-aged) | newest `sdlc:claim` comment timestamp; bare label → `labeled` timeline event; else unprovable |
| `claim` | `sdlc claim <issue> <run-id> <lane> --verify` (exit 1 = lost race); `sdlc claim --next <lane> <run-id>` picks + claims (`claimed #n`, or exit 1 `idle`); on a lost race it edits **only your own** claim to `(superseded)` | add `sdlc:wip`; post `sdlc:claim <run-id> <lane>`; claim-verify (§4) |
| `emit` | `sdlc emit <issue> <run-id> <OUTCOME> [--to <stage>] --body <text>\|--body-file <file>` — posts `sdlc:emit <run-id> <OUTCOME>` + body, validates and swaps the stage label, `sdlc:needs-human` on PARK, closes on CLOSE, removes `sdlc:wip`; refuses if the run-id doesn't own the live claim | post the comment **with the marker line first**, then the label math by hand |
| `comment` | `sdlc comment <issue> <file>` | `gh issue comment <n> --body-file <file>` |
| `write-section` | — | `gh issue view --json body` → splice your section → `gh issue edit <n> --body-file` |
| `dep-edge` | — | §6 |
| `file` | — | `gh issue create --title … --body-file … --label stage:intake` |
| `close` | `sdlc emit … CLOSE` | `gh issue close <n> --comment …` |
| `pr-open` | — | §7 |
| `reap` | `sdlc gate --reap` (re-verifies each stale lock against live data before writing) | re-fetch the newest claim; still ≥ 2 h → remove `sdlc:wip` only and comment `sdlc-dispatch: reaped stale sdlc:wip lock owned by <run-id or "unknown"> (no activity ≥2h — worker presumed dead). Item re-enters its lane.` |
| `advance` | `sdlc advance <issue> <stage>` | re-read labels, then swap |
| `stage-repair` | inside `cycle-prep` lanes report (lists ≠1) | `gh issue edit <n> --add-label stage:intake` after re-reading labels |
| `park` | — | `gh issue edit <n> --add-label sdlc:needs-human` + `sdlc-dispatch: …` comment |
| `dep-migrate` | `sdlc deps --migrate --apply` (every `cycle-prep --apply`, the `=== deps-migrate ===` section, before `deps`; dry run without `--apply`) | §6 per prose line, by hand |
| `readiness-derive` | `sdlc deps --apply` (every `cycle-prep --apply`); `LINT` lines: `label-only-blocked`, `cycle` | skip — the labels are optional |
| `sweep-ack` | `sdlc sweep --ack` **after** processing the work-list (at-least-once delivery) | none — the sweep is bounded by its ~24 h window and idempotent |

**Dispatcher shortcuts.** `sdlc cycle-prep --apply` runs the whole zero-judgment pre-dispatch
sequence (mint → maint-lock → lanes → gate --reap → deps → sweep → git-maint → worktree-sweep →
conflict-scan → maint-release) and prints one delimited `=== section ===` report plus `run-id:` and
`started:` lines — read Steps 0/0a from it, don't re-run the commands. `sdlc heal <lane>` with no
issue auto-discovers every `stage:<lane>` + `sdlc:wip` straggler after the workers return.
`sdlc digest` prints depths, parked/hold lists, and the arrivals/departures delta vs the last
cycle. `sdlc worktree <issue>` adds the issue worktree and junctions its `node_modules` to the main
checkout's install. `sdlc mint`, `maint-lock`, `maint-release` bind the runtime-side machine lock
(`dispatch.md` Step -1).

## 10. Gotchas

- **Never hand-type stage labels** when the CLI is present — `stage:verfy`-class typos are what
  the graph validation exists to kill.
- **`updatedAt` is not lock age.** Any comment resets it; use the claim comment's timestamp or
  the timeline event.
- **`--search "blocked-by:"` returns nothing.** Read edges via GraphQL.
- **Edges take the blocker's id, not its number.**
- **`gh` ≥ 2.86** for native dependency fields; `repo` scope on the token.
- The result line and JSON block contain `→`; never echo them through a shell (`../README.md`
  STOP).
