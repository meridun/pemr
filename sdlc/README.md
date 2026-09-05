# SDLC worker prompts — the core

Executable artifacts (not reference docs). This directory is the **one tree an adopter copies**
(`sdlc/` here → `sdlc/` in your repo). It is layered so that the shared text has exactly one
home and everything tracker-, topology-, or project-specific lives in a file you can swap or fill:

| Layer | File(s) | Who edits it |
|---|---|---|
| **Core** — the universal worker loop, the lane prompts, the dispatcher protocol | this `README.md`, `lanes/*.md`, `dispatch.md` | nobody, on adoption — port fixes upstream instead |
| **Binding** — how the abstract tracker/code-host operations are performed on one substrate | `bindings/<name>/BINDING.md` (+ that binding's deterministic core, if any) | nobody, on adoption — pick one; declare deviations in the profile |
| **Profile** — this project's bindings: which binding, every `<KEY>`, the variation points, known deviations | `PROFILE.md` | **you** — the only file adoption fills |

Core text never names a tracker command. Where the core needs the tracker or code host it names
an **abstract operation** in backticks — `claim`, `emit`, `dep-edge`, `dup-search`, … — and the
binding says how that operation is performed here (a CLI one-shot, a `gh`/`az` command, a manual
ritual). The contract every binding must fill is [`bindings/README.md`](bindings/README.md).

> **Resolution rules (binding on every reader).**
>
> - `<KEY>` — anything in angle brackets is a **profile key**; its value is the matching row of
>   `PROFILE.md` § Keys. Prompts are not edited to fill them (a fork *may* inline-substitute; the
>   profile stays authoritative either way). An unbound optional key means *the step it gates is
>   skipped, not improvised.*
> - `` `op` `` — an abstract operation; its concrete form is the matching row of the bound
>   binding's `BINDING.md` (`<BINDING>` names it). CLI-first: when the binding ships a
>   deterministic core, use its one-shot for the op and never re-derive the state math by hand.
>   `sdlc …` anywhere in this tree means `<SDLC_CLI> …`.
> - **Read order for a worker:** this README → `PROFILE.md` → `bindings/<BINDING>/BINDING.md` →
>   `lanes/<lane>.md`. The dispatcher reads the same three, then `dispatch.md`.
> - **"Issue"** in this tree means *the work item the binding designates as the pipeline's unit*
>   — a GitHub issue, an ADO Feature, an ADO PBI. `#<n>` / `<issue#>` is its identifier in
>   whatever form the binding gives it.

## The pipeline

`stage:intake` → `stage:design` → `stage:queued` → `stage:build` → `stage:verify` →
`stage:audit` → `stage:ship` → *(PR merged, closed)* — the canonical spine's collapsed-tail form
(see `docs/Development_SdlcComposability.md`: ship opens the PR, the human merge **is** the `ready` gate, and
`shipping → complete` collapse into merge-and-close). A binding with an explicit tail
(`ready → shipping → complete`) declares it in its `BINDING.md`; the lane prompts are unchanged.

`stage:<name>` is the **stage marker** — canonical vocabulary in every binding (a label, a tag, a
field: the binding decides the substrate, never the name). Likewise the three **control markers**:
`sdlc:wip` (the machine-owned per-issue lock, volatile), `sdlc:needs-human` (parked — a worker
needs a human decision; automation never advances it), `sdlc:hold` (human keep-off; no worker
touches it). The binding also defines a **priority order** (`priority:critical` › `priority:medium`
› `priority:future` on GitHub; a field elsewhere).

`stage:design` is a **standard phase**: every triaged item passes through it for a reviewed
implementation plan (the spec track; a spec-lite for small items). Its UX/storyboard track is an
optional adopter module — see [`lanes/design.md`](lanes/design.md). The only design bypass is
intake's already-built → `stage:verify` floor.

`stage:queued` is intentionally workerless — the human throttle between design and build. What the
human reviews there is design's `## Implementation plan` artifact: **approve** by admitting to
`stage:build` (capacity-paced — items batching in queued is normal, not a stall), or **reject** by
bouncing `stage:queued` → `stage:design` with a comment saying why. Contrast with PARK, which is
for *missing inputs* mid-phase: parked items beg for an answer; queued items sit comfortably.

## How to run

- **Scheduled (primary):** the `sdlc-dispatch` scheduled task runs the dispatcher prompt
  ([`dispatch.md`](dispatch.md)) on a cadence (hourly is typical): a per-issue wip gate (stale-lock
  reaping, verify-before-write), machine-locked git + worktree maintenance, then one
  `<WORKER_AGENT>` subagent per non-empty lane. Each worker reads the four files in the read order
  above and executes one pass; workers share no context — the issue's evidence record is the only
  carried state. There is no dispatcher singleton: overlapping dispatch runs — same machine or
  different machines — deconflict via per-issue claims, a per-machine maintenance lock, and
  idempotent tracker writes. The dispatcher's own prompt is [`dispatch.md`](dispatch.md) — the
  canonical copy; the scheduled task is a thin pointer to it.
- **Manual:** paste this README, the profile, the binding, and a lane file's body into an agent
  session. Identical behavior — the prompt doesn't know what fired it. Mint your own run-id for the
  claim. Manual and scheduled runs coexist safely: claims deconflict per-issue.

## Universal worker loop (binding)

1. **CLAIM** — eligible = open issues at `stage:<lane>` that carry **none** of `sdlc:wip`,
   `sdlc:needs-human`, or `sdlc:hold`, and have **no open blocker** (`dep-read`). If the invoking
   message supplies a **candidate snapshot** for the lane (issue, markers, created-at — the
   dispatcher inlines one from its Step 0 `snapshot`), select from that list instead of
   re-querying; without one (e.g. a manual run), self-query with `snapshot`. The snapshot only
   seeds candidate selection, never ownership — the claim race always runs against live tracker
   data, so a stale entry (closed, re-staged, or claimed since the snapshot) just loses the claim;
   move to the next candidate. Pick the next: the binding's priority order first, then oldest by
   creation date (FIFO). If none (or the snapshot is exhausted) → reply `<LANE>: idle` and stop.

   **Take the lock with `claim <issue> <run-id> <lane>`.** The op must (a) make the issue visibly
   locked (`sdlc:wip`), (b) record the owner and a server-timestamped claim, and (c) tell you
   whether you **own** the issue — a lost race is a normal outcome, not an error: leave the
   winner's lock and claim untouched, delete nothing, mark only your own losing claim as
   superseded if the binding records one, and go pick the next eligible item. Do **not** re-derive
   the pick rule by eyeball where the binding's core offers `next <lane>` (the eyeballed mis-claim
   is a known failure class — lesson of #640). How ownership is proven is the binding's
   **lock-substrate contract** (`docs/Development_SdlcComposability.md` VP1): an append-only server-timestamped
   record needs the binding's claim-verify ritual; a compare-and-swap write collapses it to one
   conditional write. Either way, the boundary between "settled" and "live" claims is a
   machine-visible event the binding names (every outcome EMIT releases the lock, and a reaper
   strip correctly invalidates earlier claims).

   The lock is machine-owned and volatile; the dispatcher's reaper may strip it, and it re-checks
   the claim's owner + age immediately before doing so.
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
     status › recorded reports for the current HEAD › the issue's comments › markers — cite what you
     relied on when you no-op. Presume an existing valid artifact good unless the human's rewind
     comment gives a reason to distrust it or your own check finds something significant; then redo
     exactly the invalidated part. Keep the check cheap — dig deeper only when evidence conflicts,
     and PARK if it stays ambiguous rather than burn the pass. For partial work, post a short
     reconciliation note (what's already done + evidence, what remains) before continuing, then do
     only the gap. If the item is conclusively shipped already, PARK with the evidence (PR, commit,
     observed behavior) for a human to close — don't march it through the remaining lanes.

     Prior work need not come from the pipeline: a local or `origin` branch whose name reasonably
     matches the issue or a child of it, work already fully or partially merged to
     `<DEFAULT_BRANCH>`, and artifacts on a predecessor issue linked in the body (a clone's
     original — clones copy the body, not the thread) are all reconcilable evidence, slotted into
     the same hierarchy as their pipeline-native equivalents. Branches that are on neither local
     nor `origin`, or whose names bear no reasonable relation to the issue, are **not
     discoverable** — don't hunt for them.
   - **Worktree isolation.** Never work in the main checkout — it may hold human WIP or another
     worker. For any lane that touches a branch, use the issue-scoped worktree
     `<WORKTREE_ROOT>/<issue#>`: create it if missing (`git worktree add <WORKTREE_ROOT>/<issue#>
     <branch>`, cutting the branch first if the lane owns branch creation), reuse it if present.
     Git's one-checkout-per-branch rule across worktrees is a second lock layer: if `worktree add`
     fails because the branch is checked out elsewhere, treat it as a lost claim race — release the
     lock (an EMIT is not owed; just drop `sdlc:wip` per the binding) and move on. Do all
     git/build/test work inside the worktree; never stash, discard, or overwrite files in the main
     tree.
   - **Shared dependency install — never `npm install` (or the ecosystem equivalent) inside a
     worktree.** The reference CLI's `worktree` command junctions the new tree's root
     `node_modules` to the main checkout's install, so `<TEST_CMD>` and `<LINT_CMD>` work
     immediately with no per-tree install. That install is *shared*: an install run inside a
     worktree mutates the main checkout and every other junctioned tree. An issue that changes
     dependencies is the one exception — unlink the junction first (non-recursive `rmdir` /
     `Remove-Item` on the link itself — never recursive-delete *through* it, which empties the
     shared target), install for real in the tree, and say so in your emit body. CLI-less forks:
     create the junction by hand (`mklink /J` / `ln -s`) or install per tree — but the
     unlink-before-install rule is the same.
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
   CLOSE) — never silent. **Every outcome releases the lock** (`sdlc:wip` off) on the way out.

   **Bounce cap (bounded loops).** Before EMITting a BOUNCE, read the issue's outcome history
   (`history`) for this lane's prior BOUNCEs to the same target lane for the same class of
   failure. Two already there → PARK instead (`sdlc:needs-human`), summarizing the loop history
   (each bounce's reason and what the fixing lane did) so the human sees why it isn't converging.
   Two full round-trips that didn't converge won't converge on the third automated attempt. A
   bounce for a *different* failure class (e.g. earlier bounces were red tests, this one is a
   merge conflict) starts its own count.

   **The outcome is one operation — `emit <issue> <run-id> <OUTCOME> [--to <stage>]` with your
   judgment body.** The op must (a) record a **machine-parseable outcome marker** (`sdlc:emit
   <run-id> <OUTCOME>` as the first line of the outcome record — the signal claim-verify and the
   reaper use to tell a settled claim from a live one), (b) do the marker math atomically — stage
   swap for ADVANCE/BOUNCE, **validated against the stage graph** so illegal jumps and hand-typed
   marker typos are rejected by construction; `sdlc:needs-human` for PARK; close for CLOSE — and
   (c) release the lock. It refuses if your run-id doesn't own the live claim. Never hand-edit
   stage markers or hand-post an outcome when the binding's core is present: a prose-only outcome
   leaves your claim looking live forever, and every later worker on that issue falsely loses the
   race to your ghost (the phantom-lock bug). CLI-less forks perform the same three steps by hand,
   marker line first.

   Leave the worktree in place (dispatcher maintenance prunes worktrees for merged/dead branches,
   and build's CONTINUE resumption depends on reaped issues keeping theirs).
4. **STOP** — reply the lane's one-line result, then end the reply with a fenced **JSON result
   block** — the machine-parseable contract the dispatcher consumes (prose stays for humans):

   ```json
   {"issue": 60, "outcome": "ADVANCE", "next_stage": "verify", "notes": "one-line summary"}
   ```

   - `outcome`: `ADVANCE` | `BOUNCE` | `PARK` | `CONTINUE` | `CLOSE` | `IDLE`.
   - `next_stage`: the stage the item sits in after the outcome (e.g. `"verify"` after a build
     ADVANCE, `"build"` after a build CONTINUE or a bounce to build, unchanged lane for PARK);
     `null` for IDLE and CLOSE.
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

These come from the profile — they are acceptance criteria on **every** change, checked at build,
proven at verify, and audited at audit:

- **Git flow is `{feature} → <DEFAULT_BRANCH> → <PROD_BRANCH>`.** Feature branches
  `<type>/<issue#>-<slug>` cut from `<DEFAULT_BRANCH>` (the default/integration branch); PRs target
  `<DEFAULT_BRANCH>`; the human merges. `<PROD_BRANCH>` is the stable/release branch — it moves only
  by human-initiated PR. **No worker ever branches from, checks out, commits to, or targets
  `<PROD_BRANCH>`.**
- **`<INVARIANTS>`** — the project-specific rules that must hold on every change (e.g. exit-code
  parity, backward-compatible schema, no secrets in fixtures, multiplayer-safe state). The profile
  lists them explicitly so build implements to them, verify exercises them, and audit reviews for
  them.
- **`<LANG_CONVENTIONS>`** — the lint/format/test bar (e.g. `<LINT_CMD>` clean — or, on a repo with a
  lint backlog, the ratchet `node sdlc/tools/check-lint-baseline.mjs` passing plus touched files
  clean, `<FULL_SUITE_CMD>` green, formatter applied) that gates every commit.
- **Decisions live in `<DECISION_RECORD>`, not in doc prose or build-branch cargo** — a decision is
  a one-liner linking to the issue that holds the debate. Workers never write ADR-style history into
  docs.

## Evidence anatomy — artifact sections vs protocol traffic

The issue's **evidence record** holds the durable, addressable artifacts as named sections, one
per owner; **comments** (the binding's discussion channel) are protocol traffic (claims, emits,
PARK questions, verify/audit reports). Workers edit only their own sections and always preserve
the original author text at the top. The section headings are canonical; *where* they live (issue
body, Feature description, PBI description) is the binding's `read` / `write-section` row.

| Artifact section | Written by | Consumed by |
|---|---|---|
| *(original author text)* | human | intake |
| `## Requirements` | intake | design, build |
| `## Acceptance criteria` | intake | build, verify |
| `## Design` *(when the UX track ran)* | design | build |
| `## Implementation plan` (baseline SHA, approach, touched paths, risky seams, test strategy, out of scope) | design | **human at queued**, build |

## Files

| File | Stage | Notes |
|---|---|---|
| [`PROFILE.md`](PROFILE.md) | — | **the one file adoption fills**: binding choice, every `<KEY>`, variation points, deviations |
| [`bindings/`](bindings/README.md) | — | the operation contract + one directory per substrate (`gh-issue` here; `ado-feature` / `ado-pbi` upstream only) |
| [`dispatch.md`](dispatch.md) | *(dispatcher — runs every lane)* | scheduled task; git/worktree maintenance + per-lane fan-out |
| [`lanes/intake.md`](lanes/intake.md) | `stage:intake` → `stage:design` *(or `stage:verify`, already-built floor)* | triage, dedup, dependency edges, requirements + AC authoring, decision debates + close sweep |
| [`lanes/design.md`](lanes/design.md) | `stage:design` → `stage:queued` | standard phase: implementation plan (spec track) for every item; optional UX track |
| [`lanes/build.md`](lanes/build.md) | `stage:build` → `stage:verify` | execute the reviewed plan → implement + targeted tests |
| [`lanes/verify.md`](lanes/verify.md) | `stage:verify` → `stage:audit` | full suite + real-run smoke |
| [`lanes/audit.md`](lanes/audit.md) | `stage:audit` → `stage:ship` | read-only security/invariant review of the diff |
| [`lanes/ship.md`](lanes/ship.md) | `stage:ship` → *(closed on merge)* | docs fan-out + open PR |
| [`tools/check-lint-baseline.mjs`](tools/check-lint-baseline.mjs) | — | tracker-neutral lint ratchet (optional `<LINT_CMD>` binding) |

`intake` additionally runs a **close sweep** every pass (its step 0): post-close bookkeeping for
the open issues a just-closed issue was blocking, since ship ends at "PR open" and the merge
itself fires no worker.

## Dependencies — edges, never prose or markers

An issue that can't proceed until another lands is recorded as a **dependency edge** in the
tracker's own relationship model (this issue *blocked by* that one — `dep-edge <this> <blocker>`),
by whichever lane discovers it — intake's collision sweep, design's plan ordering / epic split,
build's readiness-regression bounce. The dispatcher's eligibility gate reads those edges
(`dep-read`): an issue with any open blocker is claimable in **no** lane, and becomes claimable on
the cycle after its last blocker closes. A `Depends on #n` line is a human mirror the machine
ignores; any `blocked` / `ready` readiness markers the binding keeps are **derived** from the
edges each cycle and never read by the machine — so never set them by hand as a substitute for the
edge. An edge the tracker cannot represent (a blocker in another repo or another tracker) is
`sdlc:hold` + the prose line, stated in the comment.

**Prose declarations are converted, not trusted.** `dep-migrate` turns a record's `Depends on #n`
/ `**Dependencies:** …` lines into edges. The dispatcher runs it every cycle and intake runs it
again in its WORK step, so a declaration is converted by whichever comes first; between filing
and that first conversion the gate cannot see it, and a reference the tracker can't resolve is
never converted (`sdlc:hold` + prose).

**Closing a blocker satisfies its edges — so re-point before a dup close.** An edge whose blocker
is closed never blocks, however it closed. When a lane closes an issue as a duplicate of a
*still-open* canonical issue, every issue blocked by the dup would become eligible on the next
cycle with its real prerequisite unmet. Before the close, read the dup's *blocking* edges
(`dep-read`) and `dep-edge` each open dependent onto the canonical issue; then close. Closing on
a merged PR needs no re-pointing — the work landed.
