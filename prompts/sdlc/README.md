# SDLC worker prompts

Executable artifacts (not reference docs). Each file is **one stage worker** for the agentic SDLC
pipeline. All share the universal worker loop below; lane files define only their WORK and EMIT
specifics.

Pipeline: `stage:intake` → `stage:design` → `stage:queued` → `stage:build` → `stage:verify` →
`stage:audit` → `stage:ship` → *(PR merged, closed)* — the canonical spine's collapsed-tail form
(see `docs/Composability.md` (agentic-sdlc repo): ship opens the PR, the human merge **is** the `ready` gate, and
`shipping → complete` collapse into merge-and-close).

`stage:design` is a **standard phase**: every triaged item passes through it for a reviewed
implementation plan (the spec track; a spec-lite for small items). Its UX/storyboard track is an
optional adopter module — see [`design.md`](design.md). The only design bypass is intake's
already-built → `stage:verify` floor.

`stage:queued` is intentionally workerless — the human throttle between design and build. What the
human reviews there is design's `## Implementation plan` in the issue body: **approve** by
admitting to `stage:build` (capacity-paced — items batching in queued is normal, not a stall), or
**reject** by bouncing `stage:queued` → `stage:design` with a comment saying why. Contrast with
PARK, which is for *missing inputs* mid-phase: parked items beg for an answer; queued items sit
comfortably.

> **Placeholders.** Anything in `<ANGLE_BRACKETS>` is project-specific and must be filled in before
> use. See `docs/Adoption.md` (agentic-sdlc repo) for the full list. The core ones: `<PROJECT>` (name), `<REPO_PATH>`
> (local working dir), `<DEFAULT_BRANCH>` (integration branch, e.g. `dev`), `<PROD_BRANCH>`
> (release branch, off-limits to workers), `<WORKTREE_ROOT>` (e.g. `../<project>-wt`),
> `<TEST_CMD>` / `<FULL_SUITE_CMD>` / `<LINT_CMD>` / `<BUILD_CMD>` / `<SMOKE_CMD>`, `<INVARIANTS>`
> (project rules that are acceptance criteria on every change), and `<DECISION_RECORD>` (where
> decisions are logged). Optionally `<TOKEN_TOOL>` (a shell-output compactor to prefix commands
> with; omit if none).

## How to run

- **Scheduled (primary):** the `sdlc-dispatch` scheduled task runs the dispatcher prompt
  ([`dispatch.md`](dispatch.md)) on a cadence (hourly is typical): a per-issue wip gate (stale-lock
  reaping, verify-before-write), machine-locked git + worktree maintenance, then one
  `<WORKER_AGENT>` subagent per non-empty lane. Each worker reads this README plus its lane file
  and executes one pass; workers share no context — the issue thread is the only carried state.
  There is no dispatcher singleton: overlapping dispatch runs — same machine or different machines
  — deconflict via per-issue claims, a per-machine maintenance lock, and idempotent GitHub writes.
  The dispatcher's own prompt is [`dispatch.md`](dispatch.md) — the canonical copy; the scheduled
  task is a thin pointer to it.
- **Manual:** paste this README plus a worker file's body into an agent session. Identical behavior
  — the prompt doesn't know what fired it. Mint your own run-id for the claim comment. Manual and
  scheduled runs coexist safely: claims deconflict per-issue.

## Universal worker loop (binding)

1. **CLAIM** — eligible = open issues labeled `stage:<lane>` that are **NOT** labeled `sdlc:wip`,
   `sdlc:needs-human` (parked), or `sdlc:hold` (human keep-off). If the invoking message supplies a
   **candidate snapshot** for the lane (issue#, labels, createdAt — the dispatcher inlines one from
   its Step 0 snapshot), select from that list instead of re-querying; without one (e.g. a manual
   run), self-query as above. The snapshot only seeds candidate selection, never ownership — the
   claim race always runs against live GitHub data, so a stale entry (closed, relabeled, or claimed
   since the snapshot) just loses the claim; move to the next candidate. Pick the next: higher
   priority first (`priority:critical` › `priority:medium` › `priority:future`), then oldest by
   creation date (FIFO). If none (or the snapshot is exhausted) → reply `<LANE>: idle` and stop.

   **CLI-first** (the primary path when the reference CLI is present — `scripts/sdlc.mjs`, see
   `docs/Adoption.md` (agentic-sdlc repo)): do **not** re-derive the pick rule by eyeball (the eyeballed mis-claim is
   a known failure class — lesson of #640). With a candidate snapshot (or an already-known issue,
   e.g. build's CONTINUE resumption), claim it directly:
   `node scripts/sdlc.mjs claim <issue> <run-id> <lane> --verify` — a non-zero exit means you lost
   the race; move to the next candidate. Without a snapshot,
   `node scripts/sdlc.mjs claim --next <lane> <run-id>` computes the next eligible issue (same
   ordering as above), adds `sdlc:wip`, posts the claim comment `sdlc:claim <run-id> <lane>`
   (run-id = the dispatcher-supplied id, or any unique id you mint for a manual run), and runs the
   claim-verify race check — retrying the next eligible item on a lost race. It prints
   `claimed #<issue>` on success, or exits non-zero with `idle` on an empty lane. On a lost race
   the CLI edits **only your own losing claim comment** to note `(superseded)` — never the
   winner's.

   **Manual ritual (fallback, for CLI-less forks)** — take the lock in this order:
   1. Add `sdlc:wip` to the chosen issue.
   2. Post a claim comment: `sdlc:claim <run-id> <lane>`. The label is the visibility signal; the
      claim comment is the ownership record and tiebreaker.
   3. **Claim-verify:** re-fetch the issue's comments. If another `sdlc:claim` comment on this issue
      is newer than the last outcome EMIT and predates yours (or ties with a lexicographically lower
      run-id), you lost the race — leave the label and the winner's claim untouched, delete nothing,
      and go pick the next eligible item. Only the losing worker's own claim comment may be edited to
      note `superseded`.

   Either way, "newer than the last outcome EMIT" has a machine-visible boundary: the issue's most
   recent `sdlc:wip` *unlabeled* timeline event — every outcome EMIT removes `sdlc:wip`, and a
   reaper strip also (correctly) invalidates earlier claims — with the `sdlc:emit` marker comment
   (EMIT, below) as the fallback boundary signal when the timeline is unavailable.

   The lock is machine-owned and volatile; the dispatcher's reaper may strip it, and it re-checks
   the claim comment's run-id + timestamp immediately before doing so.
2. **WORK** — per the lane file, with these constraints:
   - **Never delegate — do all work inline, yourself.** No subagents (in an async harness they run
     detached; the worker yields and nothing resumes it — the item strands under `sdlc:wip`), no
     background tasks, no wait loops. Where a lane prompt names an owner skill or checklist, that
     names the **approach you apply inline**, not a subagent to dispatch. (Scheduled runs enforce
     this structurally: workers are spawned as `<WORKER_AGENT>`, which has no delegation tool.)
   - **Idempotent — reconcile, don't re-execute.** If the stage's artifact already exists, treat as
     done; do not redo. Schedulers fire on a clock, not on need — a re-run must be a safe no-op. The
     same applies to items a human rewound to an earlier stage or reopened after close: investigate
     what already exists before doing any work. Evidence hierarchy: merged code / branch state / PR
     status › recorded reports for the current HEAD › issue comments › labels — cite what you relied
     on when you no-op. Presume an existing valid artifact good unless the human's rewind comment
     gives a reason to distrust it or your own check finds something significant; then redo exactly
     the invalidated part. Keep the check cheap — dig deeper only when evidence conflicts, and PARK
     if it stays ambiguous rather than burn the pass. For partial work, post a short reconciliation
     note (what's already done + evidence, what remains) before continuing, then do only the gap.
     If the item is conclusively shipped already, PARK with the evidence (PR#, commit, observed
     behavior) for a human to close — don't march it through the remaining lanes.
   - **Worktree isolation.** Never work in the main checkout — it may hold human WIP or another
     worker. For any lane that touches a branch, use the issue-scoped worktree
     `<WORKTREE_ROOT>/<issue#>`: create it if missing (`git worktree add <WORKTREE_ROOT>/<issue#>
     <branch>`, cutting the branch first if the lane owns branch creation), reuse it if present.
     Git's one-checkout-per-branch rule across worktrees is a second lock layer: if `worktree add`
     fails because the branch is checked out elsewhere, treat it as a lost claim race — release per
     CLAIM step 3 and move on. Do all git/build/test work inside the worktree; never stash, discard,
     or overwrite files in the main tree.
   - **Refresh from `<DEFAULT_BRANCH>` (staleness rule).** On entering the worktree:
     `git fetch origin`. If `git diff --name-only HEAD...origin/<DEFAULT_BRANCH>` (upstream side)
     intersects the paths this branch touches, `git merge origin/<DEFAULT_BRANCH>` (merge, never
     rebase — branches are pushed and handed between workers). No overlap → record "dev advanced, no
     path overlap" and do not merge, so existing verify/audit reports stay valid. **Conflict
     ownership:** build resolves merge conflicts; verify and audit never do — a conflicted merge
     there is a BOUNCE → `stage:build` naming the conflicting paths. Ship always merges (the PR must
     be mergeable) and may resolve docs-only conflicts itself; code conflicts BOUNCE to build.
   - **Tree hygiene.** Before any branch switch, record the entry branch
     (`git rev-parse --abbrev-ref HEAD`) and restore exactly it before EMIT — never `<PROD_BRANCH>`
     (release, off-limits) and never a guessed default. Never stash, discard, or overwrite
     uncommitted files you didn't create (human WIP); if they genuinely block the work, PARK.
3. **EMIT exactly one outcome** — ADVANCE, BOUNCE, or PARK (build also defines CONTINUE, intake
   CLOSE) — never silent. **Every outcome removes `sdlc:wip`** on the way out.

   **Bounce cap (bounded loops).** Before EMITting a BOUNCE, check the issue's comments for this
   lane's prior `sdlc:emit … BOUNCE` markers to the same target lane for the same class of failure.
   Two already there → PARK instead (`sdlc:needs-human`), summarizing the loop history (each
   bounce's reason and what the fixing lane did) so the human sees why it isn't converging. Two
   full round-trips that didn't converge won't converge on the third automated attempt. A bounce
   for a *different* failure class (e.g. earlier bounces were red tests, this one is a merge
   conflict) starts its own count.

   **CLI-first:** when the reference CLI is present, the outcome is one command —

   ```
   node scripts/sdlc.mjs emit <issue> <run-id> <OUTCOME> [--to <stage>] --body <text>|--body-file <file>
   ```

   It posts the machine-parseable completion marker (`sdlc:emit <run-id> <OUTCOME>` as the
   comment's first line, your judgment body below it) and then does the label math atomically —
   stage swap for ADVANCE/BOUNCE (validated against the stage graph, so illegal jumps and the
   hand-typed `stage:verfy` typo class are rejected by construction), `sdlc:needs-human` for PARK,
   issue close for CLOSE — and removes `sdlc:wip`. It refuses to emit if your run-id doesn't own
   the issue's live claim. Never hand-edit stage labels or hand-post an outcome comment when the
   CLI is present: the marker line is the signal claim-verify uses to tell a settled claim from a
   live one — a prose-only outcome leaves your claim looking live forever, and every later worker
   on that issue falsely loses the race to your ghost (the phantom-lock bug).

   **Manual fallback (CLI-less forks):** post the outcome comment, then do the label math yourself
   — and start the comment with the same `sdlc:emit <run-id> <OUTCOME>` marker line, so the claim
   boundary stays machine-visible even where the timeline API is unavailable.

   Leave the worktree in place (dispatcher maintenance prunes worktrees for merged/dead branches,
   and build's CONTINUE resumption depends on reaped issues keeping theirs).
4. **STOP** — reply the lane's one-line result, then end the reply with a fenced **JSON result
   block** — the machine-parseable contract the dispatcher consumes (prose stays for humans):

   ```json
   {"issue": 60, "outcome": "ADVANCE", "next_stage": "verify", "notes": "one-line summary"}
   ```

   - `outcome`: `ADVANCE` | `BOUNCE` | `PARK` | `CONTINUE` | `CLOSE` | `IDLE`.
   - `next_stage`: the stage label the item sits in after the outcome (e.g. `"verify"` after a
     build ADVANCE, `"build"` after a build CONTINUE or a bounce to build, unchanged lane for
     PARK); `null` for IDLE and CLOSE.
   - Idle pass: `{"issue": null, "outcome": "IDLE", "next_stage": null, "notes": ""}`.
   - The block is always the **last** element of the reply, exactly one per reply. A lane a project
     configures to process multiple items in one pass returns an **array** of result objects, one
     per item.

   One item per pass; never pick up a second.

   **The result line and JSON block are return values to the dispatcher, never shell commands.**
   Do not paste them — or any `→`-containing text — into a shell: bash reads the Unicode `→`
   (normalized to ASCII `>`) as an output redirect and drops a 0-byte stray file named after the
   following token (`CONTINUE`, `dev`, `queued)`), which then makes the dispatcher's worktree
   sweep see the tree as dirty (lesson of #688). All EMIT goes through the emit ritual in step 3
   — never a hand-rolled `echo`/`gh` that echoes the result line.

## Project invariants (bind in every lane)

Fill these in for your project — they are acceptance criteria on **every** change, checked at build,
proven at verify, and audited at audit:

- **Git flow is `{feature} → <DEFAULT_BRANCH> → <PROD_BRANCH>`.** Feature branches
  `<type>/<issue#>-<slug>` cut from `<DEFAULT_BRANCH>` (the default/integration branch); PRs target
  `<DEFAULT_BRANCH>`; the human merges. `<PROD_BRANCH>` is the stable/release branch — it moves only
  by human-initiated PR. **No worker ever branches from, checks out, commits to, or targets
  `<PROD_BRANCH>`.**
- **`<INVARIANTS>`** — the project-specific rules that must hold on every change (e.g. exit-code
  parity, backward-compatible schema, no secrets in fixtures, multiplayer-safe state). List them
  explicitly so build implements to them, verify exercises them, and audit reviews for them.
- **`<LANG_CONVENTIONS>`** — the lint/format/test bar (e.g. `<LINT_CMD>` clean, `<FULL_SUITE_CMD>`
  green, formatter applied) that gates every commit.
- **Decisions live in `<DECISION_RECORD>`, not in doc prose or build-branch cargo** — a decision is
  a one-liner linking to the issue that holds the debate. Workers never write ADR-style history into
  docs.

## Issue anatomy — body sections vs comments

The issue **body** holds the durable, addressable artifacts, one section per owner; **comments**
are protocol traffic (claims, emits, PARK questions, verify/audit reports). Workers edit only
their own body sections and always preserve the original author text at the top.

| Body section | Written by | Consumed by |
|---|---|---|
| *(original author text)* | human | intake |
| `## Requirements` | intake | design, build |
| `## Acceptance criteria` | intake | build, verify |
| `## Design` *(when the UX track ran)* | design | build |
| `## Implementation plan` (baseline SHA, approach, touched paths, risky seams, test strategy, out of scope) | design | **human at queued**, build |

## Files

| File | Stage | Notes |
|---|---|---|
| [`dispatch.md`](dispatch.md) | *(dispatcher — runs every lane)* | scheduled task; git/worktree maintenance + per-lane fan-out |
| [`intake.md`](intake.md) | `stage:intake` → `stage:design` *(or `stage:verify`, already-built floor)* | triage, dedup, requirements + AC authoring, decision debates + merge sweep |
| [`design.md`](design.md) | `stage:design` → `stage:queued` | standard phase: implementation plan (spec track) for every item; optional UX track |
| [`build.md`](build.md) | `stage:build` → `stage:verify` | execute the reviewed plan → implement + targeted tests |
| [`verify.md`](verify.md) | `stage:verify` → `stage:audit` | full suite + real-run smoke |
| [`audit.md`](audit.md) | `stage:audit` → `stage:ship` | read-only security/invariant review of the diff |
| [`ship.md`](ship.md) | `stage:ship` → *(closed on merge)* | docs fan-out + open PR |

`intake` additionally runs a **merge sweep** every pass (its step 0): post-merge cascade-unblock for
issues closed by merged PRs, since ship ends at "PR open" and the merge itself fires no worker.
