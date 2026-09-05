# Label taxonomy

The `gh-issue` binding's marker substrate: every `stage:*` / `sdlc:*` / `priority:*` marker is a GitHub label. Create these once per repo.

## The labels

| Label | Colour | Meaning |
|---|---|---|
| `stage:intake` | `#0e8a16` | Awaiting triage/routing. |
| `stage:design` | `#c5def5` | Awaiting design — the implementation plan (spec track, every item); plus UX artifacts when the profile binds them. |
| `stage:queued` | `#fbca04` | Planned, awaiting admission — **the human throttle** (reviews the body's `## Implementation plan`). No worker. |
| `stage:build` | `#1d76db` | Being implemented (or waiting to be). |
| `stage:verify` | `#5319e7` | Awaiting full-suite + real-run validation. |
| `stage:audit` | `#b60205` | Awaiting read-only security/invariant review. |
| `stage:ship` | `#0052cc` | Awaiting docs fan-out + PR. |
| `sdlc:wip` | `#d93f0b` | Per-issue lock. Machine-owned, volatile. Paired with an `sdlc:claim` comment. |
| `sdlc:needs-human` | `#e99695` | Parked — a worker needs a human decision. Automation never advances it. |
| `sdlc:hold` | `#000000` | Human keep-off. No worker touches it. |
| `priority:critical` | `#b60205` | Claimed first within a lane. |
| `priority:medium` | `#fbca04` | Default priority. |
| `priority:future` | `#c2e0c6` | Claimed last. |
| `blocked` | `#d876e3` | *(derived, optional)* Has an **open native blocker**. Written by `sdlc deps --apply` from the dependency edges; never read by the machine. |
| `ready` | `#0e8a16` | *(derived, optional)* Has dependency edges and every blocker is closed. Complements `blocked`; same derivation. |

## Blocking: native issue dependencies are the source of truth

An issue's readiness is a fact about **GitHub's native issue dependencies** (the *blocked by* /
*blocking* relations on the issue — REST `issues/{n}/dependencies/{blocked_by,blocking}`, GraphQL
`blockedBy` / `blocking`), not about its labels and not about prose in its body. The dispatcher's
lane snapshot reads every open issue's `blockedBy` edges in one GraphQL pass and an issue with
**any OPEN blocker is never eligible** for a worker to claim, in any lane — a fourth ineligibility
bucket (`blocked`) beside hold, needs-human and wip. The gate is re-evaluated from live edge state
every cycle, so the moment a blocker closes (its PR merges, or a human closes it) its dependents
become claimable on the next cycle with no sweep, window or ack involved.

`blocked` / `ready` are therefore **derived labels**: `sdlc deps --apply` (on every `cycle-prep
--apply`) writes them from edge state so humans keep a readable readiness column, and the machine
stops trusting them. Hand-flipping `blocked` → `ready` unblocks nothing; adding `blocked` by hand
blocks nothing. The one ambiguity the lint surfaces rather than repairs is a `blocked` label with
**no** edge at all: that is either a cross-repo block (native edges are per-repo — represent it
as `sdlc:hold` plus prose) or a stale label; a human decides which.

**Writing an edge** takes the blocker's numeric *id*, not its number:

```bash
BLOCKER_ID=$(gh api repos/{owner}/{repo}/issues/<blocker#> --jq .id)
gh api -X POST repos/{owner}/{repo}/issues/<dependent#>/dependencies/blocked_by -F issue_id=$BLOCKER_ID
```

Prose (`Depends on #n`) may stay as a human mirror; it is no longer authoritative. Existing prose
migrates once with `sdlc deps --migrate` (dry run; `--apply` creates the edges —
see `docs/Development_SdlcAdoption.md` § 4).

## Exactly one stage label per open issue

Every open issue carries **exactly one** `stage:*` label — the pipeline invariant the dispatcher
enforces (`sdlc/dispatch.md` Step 0b). **Zero** stage labels makes an issue invisible to every lane
forever (a triage escapee that will never be built or closed): the dispatcher auto-repairs it to
`stage:intake` (verify-before-write) — intake is the safe re-entry, re-routing or reconciling from
there. That covers the post-ship window too: ship's terminal ADVANCE removes `stage:ship` and the
merge normally closes the issue promptly; an issue that outlives a dispatch cycle awaiting merge
is routed back through intake, whose evidence-based reconciliation (open PR in flight / already
merged) is a safe no-op or a PARK-with-evidence, never duplicate work. **Two or more** `stage:*`
labels make an issue eligible in two lanes at once — two workers could claim it in one cycle — so
the dispatcher parks it (`sdlc:needs-human`) for a human to pick: a snapshot can't adjudicate the
right stage. `sdlc:wip` or `sdlc:needs-human` on a zero-stage issue means a worker died
mid-transition; the same repair applies.

The runtime check is a **hand-edit backstop**: the reference CLI's transition validation never
creates a zero/dual-stage state, but labels edited by hand or by tooling outside the CLI still
can. A fork whose tracker substrate cannot hold the invariant (e.g. a state field that must pass
through a stageless value) declares the deviation in its `PROFILE.md` rather than silently
diverging.

## Create them (`gh`)

Run from the repo, authenticated with `repo` scope. `--force` makes it idempotent (updates colour if
the label already exists).

```bash
# stages
gh label create "stage:intake"  --color 0e8a16 --description "Awaiting triage/routing" --force
gh label create "stage:design"  --color c5def5 --description "Awaiting implementation plan (+ UX artifacts when bound)" --force
gh label create "stage:queued"  --color fbca04 --description "Plan reviewed here — human throttle (no worker)" --force
gh label create "stage:build"   --color 1d76db --description "Being implemented" --force
gh label create "stage:verify"  --color 5319e7 --description "Awaiting validation" --force
gh label create "stage:audit"   --color b60205 --description "Awaiting security/invariant review" --force
gh label create "stage:ship"    --color 0052cc --description "Awaiting docs fan-out + PR" --force

# sdlc control
gh label create "sdlc:wip"          --color d93f0b --description "Per-issue lock (machine-owned)" --force
gh label create "sdlc:needs-human"  --color e99695 --description "Parked — needs a human decision" --force
gh label create "sdlc:hold"         --color 000000 --description "Human keep-off — no worker touches it" --force

# priority
gh label create "priority:critical" --color b60205 --description "Claimed first within a lane" --force
gh label create "priority:medium"   --color fbca04 --description "Default priority" --force
gh label create "priority:future"   --color c2e0c6 --description "Claimed last" --force

# optional readiness axis — DERIVED from native issue dependencies by `sdlc deps --apply`
gh label create "blocked" --color d876e3 --description "Has an open native blocker (derived — edit the dependency, not the label)" --force
gh label create "ready"   --color 0e8a16 --description "Every native blocker closed (derived)" --force
```

No dispatcher-lock issue exists in this model: there is no dispatcher singleton. Overlapping
dispatch runs deconflict via per-issue claims, idempotent GitHub writes, and a per-machine
filesystem lock (`.git/sdlc-maint.lock`) the dispatcher manages itself — nothing to create on the
tracker. See `sdlc/dispatch.md` Step -1.
