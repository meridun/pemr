# Focused dispatch

**Not a lane worker, and not the scheduled dispatcher.** A focused dispatch is **one normal
[`dispatch.md`](dispatch.md) cycle whose lane eligibility is filtered to a single issue**. It is
run manually, on request, to push one issue through consecutive lanes in one sitting. Everything
the dispatcher does — maintenance, machine lock, wip gate and reaping, dependency passes,
stage-marker integrity, per-lane model routing, background workers, self-heal, digest — happens
exactly as in a normal cycle. Only the candidate lists differ.

**Why it chains.** `dispatch.md` already re-runs a lane serially when an earlier worker in the
same cycle ADVANCEd an item into it, and never runs the same lane twice in one cycle. With the
eligible lists filtered to one issue, that rule walks the issue forward lane by lane and stops by
itself:

| Worker outcome / issue state | What happens |
|---|---|
| ADVANCE into a worker lane that hasn't run this cycle | that lane runs next (serially) |
| ADVANCE into `stage:queued` (design done) | stop — the human plan-approval gate |
| ship's terminal ADVANCE (PR open, zero-stage) | stop — the human merge gate |
| PARK | stop — `sdlc:needs-human` |
| BOUNCE | stop — the serial re-run rule follows ADVANCE only; rerun focus mode for another pass |
| CONTINUE | stop — the issue stays in a lane that already ran this cycle; rerun focus mode to resume |
| CLOSE, IDLE (lost the claim race) | stop |

Locking is the normal per-issue worker claim (`sdlc:wip` + the binding's claim record). Between
a worker's EMIT and the next worker's claim the issue is briefly unlocked; if a concurrent
scheduled dispatch claims it in that window, the focus run's next worker loses the claim race,
reports IDLE, and the run stops. That is a lost race, never an error — the issue continues in the
normal pipeline.

The filter needs the binding's deterministic core (gh-issue: `sdlc cycle-prep --issue <N>` and
`sdlc lanes --issue <N>`). CLI-less forks apply the same filter by hand: take the normal
`snapshot`, keep only #<N> in each lane's candidate list, and state where #<N> stands (eligible in
a lane, ineligible and why, queued, no or multiple stage markers, not open).

---

## Prompt (paste this, substituting the issue number)

You are running a **focused SDLC dispatch** for the `<PROJECT>` project, for issue **#<N>**.

Repository (local working directory): `<REPO_PATH>`

Read `sdlc/README.md`, `sdlc/PROFILE.md`, and `sdlc/bindings/<BINDING>/BINDING.md`, then
[`sdlc/dispatch.md`](dispatch.md), and execute ONE dispatch cycle exactly as it specifies — it is
binding in full — with only these deltas:

1. **Focus the prep.** Run `sdlc cycle-prep --apply --issue <N>` instead of the bare
   `cycle-prep --apply`. Maintenance, gate, deps, sweep, and integrity sections stay global; only
   the `=== lanes ===` eligible lists are filtered to #<N>, and the section carries a
   `focus #<N>: …` verdict line. If the verdict is anything other than `eligible in <lane>`
   (ineligible, queued, no/multiple stage labels, not open), run no workers: still do Step 0b and
   write the digest, reporting the verdict as the stop reason.
2. **Focus the re-query.** When a worker ADVANCEs #<N> into another lane, re-query with
   `sdlc lanes --issue <N>` (not an unfiltered lane query) and read its `focus` line to decide
   whether that lane runs next. Run lanes strictly **serially**, one worker at a time, each
   spawned in the background and awaited before the next — there is only one issue, so there is
   nothing to parallelize.
3. **No intake-first sweep.** Skip dispatch.md's "run intake before the batch when the close
   sweep has pending closes" exception unless #<N> itself is in `stage:intake`. The close sweep
   is bookkeeping for other issues; the next scheduled cycle processes it.
4. **Worker candidate list** is always the single entry for #<N>, from the same report (or the
   re-query) that made the lane eligible.
5. **Digest additions.** Add a `focus: #<N>` line at the top, one line per lane run in order
   (lane, outcome, next_stage, tokens), and a final `stopped: <reason>` line naming the stop row
   from the table in `sdlc/focus.md` (e.g. `stopped: queued (human gate)`,
   `stopped: BOUNCE → build`, `stopped: PARK`).

Never work the issue yourself — only `<WORKER_AGENT>` subagents touch it.
