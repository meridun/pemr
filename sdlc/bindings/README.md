# Bindings — the operation contract

A **binding** is how the core's abstract tracker and code-host operations are performed on one
substrate. The core (`../README.md`, `../lanes/*.md`, `../dispatch.md`) names operations in
backticks; the bound binding's `BINDING.md` says what each one is *here*. A project binds exactly
one (`<BINDING>` in `PROFILE.md`) and declares any deviation from it in the profile.

| Binding | Tracker | Unit of work ("issue") | Topology | Lineage |
|---|---|---|---|---|
| [`gh-issue`](gh-issue/BINDING.md) | GitHub issues + PRs | the issue | single repo, flat | the template default; three production adoptions; ships the reference CLI |
| [`ado-feature`](https://github.com/meridun/agentic-sdlc/tree/34b769e/sdlc/bindings/ado-feature/BINDING.md) *(upstream only — not carried here)* | Azure DevOps work items + ADO PRs | the **Feature** (child PBIs per repo, stage Tasks per stage) | multi-repo | one production adoption (read-only consumer) |
| [`ado-pbi`](https://github.com/meridun/agentic-sdlc/tree/34b769e/sdlc/bindings/ado-pbi/BINDING.md) *(upstream only — not carried here)* | Azure DevOps work items + ADO PRs | the **PBI** (stage Tasks per stage) | single repo / mono-repo | **proposed** — the single-repo degenerate form of `ado-feature`; no production lineage yet |

A new binding is a new directory with a `BINDING.md` that fills every row below. Nothing in the
core changes.

## What every `BINDING.md` must state

1. **Unit of work** — which tracker item is the pipeline's "issue", and how it is identified.
2. **Spine form** — collapsed tail (ship opens the PR, merge closes) or explicit
   `ready → shipping → complete`.
3. **Marker substrate** — what carries `stage:<name>`, `sdlc:wip`, `sdlc:needs-human`,
   `sdlc:hold`, and the priority order. Names are canonical; the substrate is the binding's.
4. **Lock-substrate class** (`docs/Development_SdlcComposability.md` VP1) — append-only server-timestamped record
   (needs a claim-verify ritual and a named settled/live boundary event) or compare-and-swap (one
   conditional write). Plus how a lock's **age and owner are proven server-side** for the reaper.
5. **Evidence locations** — where each canonical artifact section (`## Requirements`,
   `## Acceptance criteria`, `## Design`, `## Implementation plan`) lives, and what the comment
   channel is.
6. **Dependency model** — how a blocked-by edge is recorded and read; what cannot be represented
   (→ `sdlc:hold` + prose).
7. **Code host** — PR list/state/open, and how a PR links to and closes the issue.
8. **Deterministic core** — the CLI (or none) and which operations it owns.
9. **The operation table** below, one row each, with the concrete command or manual ritual.
10. **Gotchas** — substrate-specific hazards the core cannot know about.

## The operations

Read-side (never mutate):

| Op | Contract |
|---|---|
| `snapshot` | every open issue: id, stage marker(s), control markers, priority, created-at, open-blocker state. One call serves a whole dispatch cycle; workers self-query only on manual runs. |
| `read <issue>` | the evidence record: artifact sections + comments, in order. |
| `history <issue>` | the claim and outcome records (`sdlc:claim …` / `sdlc:emit …`) with server timestamps — bounce cap, idempotency, claim-verify. |
| `dep-read` | blocked-by edges for open issues with each blocker's open/closed state. |
| `dup-search <keywords>` | candidate duplicates among open issues (closed too, on request); judgment stays with the caller. |
| `in-flight <stage>` | open issues at a given stage (collision sweep). |
| `closed-since <window>` | issues closed in the window and the open issues each was blocking (close sweep work-list). |
| `pr-list` / `pr-state <branch>` | open PRs with head branch + mergeability; merge state of a branch's PR. |
| `lock-age <issue>` | server-provable owner + age of the live lock, or **unprovable** (then never reap). |

Write-side, worker:

| Op | Contract |
|---|---|
| `claim <issue> <run-id> <lane>` | take the lock: `sdlc:wip` on, owner + server timestamp recorded, ownership verified; non-zero / "lost" on a lost race. `next <lane> <run-id>` picks the next eligible issue by the binding's order and claims it. |
| `emit <issue> <run-id> <OUTCOME> [--to <stage>]` | outcome marker line first, then stage-graph-validated marker math, then lock release — atomically, refusing when `run-id` doesn't own the live claim. |
| `comment <issue> <body>` | protocol traffic on the comment channel. |
| `write-section <issue> <heading> <body>` | create/replace one owned artifact section, preserving everything else. |
| `dep-edge <dependent> <blocker>` | record a blocked-by edge. |
| `file <title> <body> [<stage>]` | create an issue (intake's dependency-audit batch issue). |
| `close <issue>` | close (intake CLOSE) — the emit op covers it where a core exists. |
| `pr-open <branch> <base> <body>` | open the PR that closes the issue on merge. |

Write-side, dispatcher:

| Op | Contract |
|---|---|
| `reap <issue>` | verify-before-write: re-prove the lock's age, then strip `sdlc:wip` only, leave every other marker, record the reap on the issue. |
| `advance <issue> <stage>` | dispatcher-side stage swap (the conflict bounce), validated against the stage graph, verify-before-write. |
| `stage-repair <issue>` | zero stage markers → `stage:intake`, verify-before-write. |
| `park <issue> <reason>` | `sdlc:needs-human` + comment (multi-stage marker, stalled worker). |
| `dep-migrate` | *(optional)* convert line-leading prose declarations (`Depends on #n`, `Blocked by #n`, `**Dependencies:** …`) in open-issue records into edges; idempotent; unresolvable references logged as skipped. Run every cycle and by intake. |
| `readiness-derive` | *(optional)* rewrite derived readiness markers from edges + lint (edge-less `blocked`, cycles). |
| `sweep-ack` | *(optional)* mark the close-sweep window processed; absent → the sweep is window-bounded and idempotent. |
