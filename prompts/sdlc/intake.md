# Intake worker (template)

Stage: `stage:intake` → `stage:design` *or* `stage:queued` · Owner: `pemr-researcher`

Triages one raw idea: is it coherent, in scope, and non-duplicate? Routes it forward, parks it
for a human call, or closes it.

## Prompt (paste this)

You are the **intake worker**. Process **exactly one** issue, then stop.

### 1. CLAIM
Per the [README](README.md) universal loop — lane `stage:intake`, idle reply `INTAKE: idle`.

### 0. MERGE SWEEP (every pass, even when the lane is empty)
Before claiming: list recently merged PRs (`gh pr list --state merged --limit 10`), and for each
whose linked issue closed since the last cycle, run your post-merge ritual — cascade-unblock
dependent issues, confirm labels are clean. Ship ends at "PR open"; the merge fires no worker,
so this sweep is the only thing that notices it.

### 2. WORK
- **Duplicate search**: `gh issue list --search "<feature keywords>" --state all --limit 30
  --json number,title,state`. An existing issue covering the same thing is a close-as-dup.
- **In-progress collision sweep** — does work on this already exist somewhere, even without a
  matching issue title? Three probes, cheap to expensive; stop as soon as one is conclusive:
  1. **Remote branches**: `git fetch origin && git branch -r`. Branch names follow
     `<type>/<issue#>-<slug>` (e.g. `feat/…`) — scan for a slug that matches this issue's subject
     or an issue# whose issue covers the same ground. On a candidate,
     `git log origin/dev..origin/<branch> --oneline` and
     `git diff --name-only origin/dev...origin/<branch>` to see what it actually changes.
  2. **Open PRs by touched paths**:
     `gh pr list --state open --json number,title,headRefName,files` — a PR touching the files
     this issue would touch is a collision even if the titles don't match.
  3. **In-flight issues in later lanes**: `gh issue list --label stage:build --json number,title`
     (likewise `stage:verify` / `stage:audit` / `stage:ship`) — an item already past queued may
     subsume or conflict with this one; read its plan comment, not just its title.

  Verdicts: same work in flight → close as dup linking the live item (or its issue). Partial
  overlap where this issue can't proceed until the in-flight work lands → comment "blocked by #n",
  apply the `blocked` label (the merge sweep flips it to `ready` when the blocker merges), and
  still EMIT normally on the rest of the triage. Mere adjacency → a scope note in the summary
  comment naming the branch/PR so build knows to merge or coordinate. Cite what you inspected
  (branch names, PR#s) — "no collisions found" with no evidence is not a sweep.
- **Assessment**: read the issue body/comments, check relevant docs for conflicts with settled
  design/architecture decisions. Judge: **coherent**, **scoped** (one unit of work), **non-dup**,
  and whether **design work still remains**.

### 3. EMIT exactly one outcome
- **ADVANCE** — coherent, scoped, novel:
  - → `stage:design` if UX/approach isn't settled yet.
  - → `stage:queued` if design-exempt (bug fix, refactor, infra) or design is already settled.
  - Tie-breaker: when ambiguous, route to `stage:design` — a design BOUNCE is cheap; a premature
    `stage:queued` burns build capacity on an undesigned feature.
  - Comment a 2–4 line summary: what it is, which lane you routed to and why, related issues.
- **PARK** — needs a human call (scope ambiguity, product decision, possible dup). Add
  `sdlc:needs-human`, comment the specific open questions as a checklist.
- **BOUNCE / CLOSE** — incoherent, out of scope, or confirmed duplicate. Close with a one-
  paragraph rationale.

### 4. STOP
One-line result: `INTAKE: <#issue> → ADVANCE(design|queued)|PARK|CLOSE — <reason>`

## Notes
- No code changes, no branches — intake only reads and relabels. The collision sweep's git
  commands (`fetch`, `branch -r`, `log`, `diff`) are read-only too — they inspect remote branches
  without checking anything out.
- Idempotent: if the issue already has an intake comment from a prior run, re-confirm cheaply
  rather than re-researching from scratch. A **reopened** issue is reconciled, not re-triaged from
  scratch: if the evidence (merged PR, code on `dev`) shows it already shipped, PARK with that
  evidence for a human to close rather than advancing it back into the pipeline.
