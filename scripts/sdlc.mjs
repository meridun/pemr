#!/usr/bin/env node
/**
 * `sdlc` — reference implementation of the deterministic one-shots for the
 * agentic SDLC pipeline's issue state-machine ritual. Adopters COPY this file
 * into their repo and adapt the constants below; it is not a library you
 * depend on. The dbmate analog for the pipeline: the stage graph *is* a
 * schema, so transitions are validated against it and the hand-typed
 * `stage:verfy` label-typo class is killed by construction.
 *
 * Only deterministic label/branch math lives here. Report and comment *bodies*
 * stay judgment (the agent writes them); `sdlc comment` is a thin plumbing
 * wrapper that posts a body the agent already authored to a file.
 *
 * Commands (worker-side):
 *   sdlc claim   <issue> [<run-id> <lane>]  add sdlc:wip (+ claim comment), print branch + status
 *   sdlc claim   --next <lane> <run-id>     CLI picks the next eligible issue for the lane + claims it
 *   sdlc emit    <issue> <run-id> <outcome>  post the machine-parseable outcome comment
 *                                     (`sdlc:emit <run-id> <OUTCOME>`) + do the label math in one
 *                                     shot — the completion signal `claim --verify` keys off
 *   sdlc advance <issue> <to-stage>   validate transition, swap stage label, drop sdlc:wip
 *   sdlc context <issue>              branch + status + issue labels/state + open PRs for branch
 *   sdlc worktree <issue> [<branch>]  add a sibling git worktree for the issue's branch
 *   sdlc comment <issue> <file>       post a body-file comment (plumbing only)
 *   sdlc dup-check "<keywords>"       rank open-issue dup candidates (exit 2 if any); judgment stays with the caller
 *
 * Commands (dispatcher-side; each a pure function of gh/git/fs state):
 *   sdlc gate     [--reap]            per-issue wip-lock ages from timeline events → LIVE/REAP/CLEAR;
 *                                     --reap re-verifies each stale lock against live data before writing
 *   sdlc mint                         coin + print this cycle's run-id (`run-id: <id>` line)
 *   sdlc maint-lock    <run-id>       acquire the per-machine maintenance lock (.git/sdlc-maint.lock);
 *                                     exit 1 = held: skip Step 0a, never abort the cycle
 *   sdlc maint-release <run-id>       release the maintenance lock (idempotent)
 *   sdlc lanes                        per-lane depth + eligibility + the ≠1 stage-label check
 *   sdlc heal     [<lane>] [<issue>]  post-worker self-heal: did the worker clear its lock? (lane-only = auto-discover)
 *   sdlc git-maint                    fetch, ff default branch, prune ancestry/squash-merged branches, PR state
 *   sdlc worktree-sweep [--apply]     remove clean issue-scoped worktrees whose branch is gone/merged or issue closed
 *   sdlc conflict-scan [--apply]      comment + bounce-to-build issues whose open PR conflicts with the default branch
 *   sdlc sweep    [--state <file>]    read-only merge-sweep work-list for intake step 0:
 *                                     merged PRs → closed issues → blocked dependents; `sweep: clear` if none;
 *                                     writes nothing — `sweep --ack` (run after processing) marks merges swept
 *   sdlc cycle-prep [--apply]         the whole pre-dispatch sequence (mint→maint-lock→lanes→gate --reap→
 *                                     sweep→git-maint→worktree-sweep→conflict-scan→maint-release) in one
 *                                     delimited, machine-readable report
 *   sdlc digest   [--state <file>]    queue depths, parked/hold lists, arrivals-diff vs last cycle
 *
 * The stage graph (forward pipeline edges + the documented bounces):
 *   intake → design → queued → build → verify → audit → ship
 * with `sdlc:wip` as the machine lock and `sdlc:needs-human` / `sdlc:hold` as
 * parks (see prompts/sdlc/README.md).
 *
 * Usage:
 *   node scripts/sdlc.mjs <command> [args...]   (or: npm run sdlc <command>)
 */

import { execFileSync } from 'child_process';
import { randomBytes } from 'crypto';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

import { assertClean } from './pii-scan.mjs';

// --- Adapt to your repo -------------------------------------------------------
// These are the only project-specific knobs. `<DEFAULT_BRANCH>` is the
// integration branch PRs target; `<PROD_BRANCH>` is the release branch no
// worker ever touches. The worktree name function must match the
// `<WORKTREE_ROOT>/<issue#>` pattern your prompts use — the dispatcher's sweep
// only touches worktrees matching it.
export const DEFAULT_BRANCH = 'dev'; // adapt to your repo (e.g. 'main')
export const PROD_BRANCH = 'main'; // pemr: main is prod (flow: feature → dev → main)
/** Sibling-worktree name for an issue: <repoName>-wt-<issue#>. */
export const worktreeName = (repoName, issue) => `${repoName}-wt-${issue}`;

export const WIP_LABEL = 'sdlc:wip';

/**
 * Ordered pipeline stages. Design is a standard phase of the spec; folding it
 * into intake is a *declared adopter deviation* — if you declare it, drop
 * 'design' here and remove its edges from STAGE_GRAPH below.
 */
export const STAGES = ['intake', 'design', 'queued', 'build', 'verify', 'audit', 'ship'];

/**
 * Legal stage transitions: the forward pipeline edge(s) plus the documented
 * bounces from prompts/sdlc/. `ship` is terminal on ADVANCE (the ship worker
 * opens a PR; the merge closes the issue) but may still bounce a late code
 * problem or merge conflict back to build. Any edge not listed here is
 * rejected — that is what fixes the label-typo class of bug.
 */
export const STAGE_GRAPH = {
  // 'design' is the standard forward edge; 'queued' is the shortcut for
  // adopters who declare the fold-design-into-intake deviation; 'verify' is
  // the already-built floor route (an item arrives with its implementation
  // already landed and only needs verification).
  intake: ['design', 'queued', 'verify'],
  design: ['queued', 'intake'],
  // 'build' is the forward edge; 'design' is the human spec-rejection bounce
  // at the throttle gate.
  queued: ['build', 'design'],
  build: ['verify', 'queued', 'design', 'intake'],
  verify: ['audit', 'build'],
  audit: ['ship', 'build'],
  // ship has no *forward* edge (the ship worker opens a PR; the merge closes
  // the issue) — its only edge is the conflict bounce back to build when an
  // open PR conflicts with the default branch (dispatch step 0a.3).
  ship: ['build'],
};

/** A user-facing error whose message is printed without a stack trace. */
export class SdlcError extends Error {}

export function isValidStage(stage) {
  return Object.prototype.hasOwnProperty.call(STAGE_GRAPH, stage);
}

export function isValidTransition(from, to) {
  return isValidStage(from) && (STAGE_GRAPH[from] ?? []).includes(to);
}

/**
 * Extract the single current stage from an issue's label names. Defensive:
 * throws on zero or multiple `stage:*` labels, or an unrecognized stage — all
 * of which are corrupt pipeline state a human should look at, not silently pick.
 */
export function currentStage(labelNames) {
  const stageLabels = (labelNames ?? []).filter((n) => n.startsWith('stage:'));
  if (stageLabels.length === 0) {
    throw new SdlcError('issue has no stage:* label — not in the pipeline');
  }
  if (stageLabels.length > 1) {
    throw new SdlcError(`issue has multiple stage labels: ${stageLabels.join(', ')}`);
  }
  const stage = stageLabels[0].slice('stage:'.length);
  if (!isValidStage(stage)) {
    throw new SdlcError(`unknown stage label "${stageLabels[0]}"`);
  }
  return stage;
}

/**
 * Compute the label edit for an advance/bounce. Returns the labels to remove
 * (current stage + sdlc:wip if present) and add (target stage), or throws an
 * SdlcError describing the illegal transition. Pure — no I/O.
 */
export function planAdvance(labelNames, toStage) {
  const from = currentStage(labelNames);
  if (!isValidStage(toStage)) {
    throw new SdlcError(`unknown target stage "${toStage}" (valid: ${STAGES.join(', ')})`);
  }
  if (from === toStage) {
    throw new SdlcError(`already at stage:${toStage} — nothing to do`);
  }
  if (!isValidTransition(from, toStage)) {
    const legal = (STAGE_GRAPH[from] ?? []).map((s) => `stage:${s}`).join(', ') || 'none';
    throw new SdlcError(
      `illegal transition stage:${from} → stage:${toStage} (legal from ${from}: ${legal})`,
    );
  }
  const removeLabels = [`stage:${from}`];
  if ((labelNames ?? []).includes(WIP_LABEL)) {
    removeLabels.push(WIP_LABEL);
  }
  return { from, to: toStage, removeLabels, addLabels: [`stage:${toStage}`] };
}

// --- Pure dispatcher-side helpers ----------------------------------------------

/** Lanes that have a worker (queued is the workerless human throttle). */
export const WORKER_LANES = ['intake', 'design', 'build', 'verify', 'audit', 'ship'];

/** Labels that make an item ineligible for a worker to claim. */
export const PARK_LABELS = ['sdlc:needs-human', 'sdlc:hold'];

/** The wip-lock reap threshold: two full hourly cycles. */
export const WIP_STALE_MS = 2 * 60 * 60 * 1000;

/**
 * Priority ordering for CLAIM: lower rank sorts first. Unlabeled is the normal
 * default and sorts between critical and future — only the two exceptional
 * tiers carry a label.
 */
export const PRIORITY_RANK = {
  'priority:critical': 0,
  'priority:future': 2,
};
const NO_PRIORITY_RANK = 1;

/** Every `stage:*` suffix on a label set (defensive: may be 0 or >1). */
export function stagesOf(labelNames) {
  return (labelNames ?? [])
    .filter((n) => n.startsWith('stage:'))
    .map((n) => n.slice('stage:'.length));
}

/** The priority rank of a label set (unlabeled → last). */
export function priorityRank(labelNames) {
  for (const [label, rank] of Object.entries(PRIORITY_RANK)) {
    if ((labelNames ?? []).includes(label)) return rank;
  }
  return NO_PRIORITY_RANK;
}

/**
 * The per-issue wip-lock gate decision. `wipItems` is `[{ number, ageMs }]` —
 * age measured from the `sdlc:wip` labeled event, NOT the issue's updatedAt
 * (which any later comment/edit falsely refreshes). Pure.
 *
 * Locking is per-issue: a fresh lock means a LIVE worker — that one issue is
 * simply ineligible this cycle; it never aborts the run. A stale lock
 * (at/over the threshold) is a dead worker → REAP that item only.
 */
export function planGate(wipItems, thresholdMs = WIP_STALE_MS) {
  const items = wipItems ?? [];
  const live = items.filter((i) => i.ageMs < thresholdMs);
  const reap = items.filter((i) => i.ageMs >= thresholdMs);
  const decision = items.length === 0 ? 'clear' : reap.length > 0 ? 'reap' : 'live';
  return { decision, live, reap };
}

/**
 * The machine maintenance-lock reap threshold (dispatch.md Step -1): 30 min,
 * not the 2h worker threshold — maintenance takes minutes, so a longer freeze
 * only delays recovery.
 */
export const MAINT_STALE_MS = 30 * 60 * 1000;

/**
 * Machine maintenance-lock decision (dispatch.md Step -1). There is no
 * dispatcher singleton: this lock only serializes Step 0a (git/worktree/
 * artifact maintenance) on ONE machine, never aborts a cycle, and runs on
 * different machines never contend. `state` is `{ exists, ageMs }` — age
 * measured from the lock's owner.txt timestamp. Pure.
 */
export function planMaintLock(state, thresholdMs = MAINT_STALE_MS) {
  if (!state?.exists) return { action: 'acquire' };
  if (state.ageMs < thresholdMs) return { action: 'skip' }; // live run — skip Step 0a, never abort
  return { action: 'reap' }; // holder presumed dead — reap by atomic rename
}

/**
 * Claim-verify race decision from an issue's comments (`[{ body, createdAt }]`,
 * chronological) plus the claim boundary: the newest `sdlc:wip` *unlabeled*
 * timeline event (see `lastUnlabeledAt`), or null if the label was never
 * removed. Every outcome EMIT removes `sdlc:wip`, so claims at/before that
 * event are settled history — and a reaper strip also (correctly) invalidates
 * earlier claims. The `sdlc:emit`/legacy outcome-comment markers are kept as a
 * second settle signal so an unavailable timeline (boundary null) degrades to
 * the pre-boundary behavior instead of resurrecting settled claims (which
 * would make `emit` falsely refuse ownership). Winner = earliest live claim;
 * ties break to the lexicographically lower run-id. Pure.
 */
export function planClaimVerify(comments, myRunId, boundaryIso = null) {
  // `sdlc:emit …` is the protocol marker posted by `sdlc emit`; the bare
  // ADVANCE/BOUNCE/PARK/CONTINUE prefixes are legacy hand-posted outcomes kept
  // for issues whose history predates the emit command.
  const OUTCOME = /^(sdlc:emit\s|ADVANCE|BOUNCE|PARK|CONTINUE|sdlc-dispatch: reaped)/;
  let claims = [];
  for (const c of comments ?? []) {
    if (OUTCOME.test(c.body ?? '')) {
      claims = []; // claims before an outcome are settled history
      continue;
    }
    const m = c.body?.match(/^sdlc:claim\s+(\S+)/);
    if (!m) continue;
    if (boundaryIso && c.createdAt <= boundaryIso) continue; // settled by unlabel event
    claims.push({ runId: m[1], createdAt: c.createdAt });
  }
  if (!claims.some((c) => c.runId === myRunId)) {
    return { won: false, winner: null, reason: 'own claim not found' };
  }
  const winner = claims.slice().sort((a, b) => {
    if (a.createdAt !== b.createdAt) return a.createdAt < b.createdAt ? -1 : 1;
    return a.runId < b.runId ? -1 : 1;
  })[0];
  return { won: winner.runId === myRunId, winner: winner.runId, reason: null };
}

/** Worker outcomes `sdlc emit` accepts (prompts/sdlc/README.md EMIT step). */
export const EMIT_OUTCOMES = ['ADVANCE', 'BOUNCE', 'PARK', 'CONTINUE', 'CLOSE'];

/**
 * Compute the label edit (and whether to close) for a worker's EMIT. Pure.
 *
 *   ADVANCE/BOUNCE --to <stage>  planAdvance label math (stage swap + wip drop)
 *   ADVANCE from ship (no --to)  terminal: remove stage:ship + wip, add nothing
 *   PARK                          drop wip, add sdlc:needs-human
 *   CONTINUE                      drop wip only (build's not-a-lane-change outcome)
 *   CLOSE                         drop wip, close the issue (intake dup/incoherent)
 *
 * Throws SdlcError on an unknown outcome, a missing/illegal target stage, or a
 * BOUNCE without a target.
 */
export function planEmit(labelNames, outcome, toStage) {
  if (!EMIT_OUTCOMES.includes(outcome)) {
    throw new SdlcError(`unknown outcome "${outcome}" (valid: ${EMIT_OUTCOMES.join(', ')})`);
  }
  const labels = labelNames ?? [];
  const wipRemove = labels.includes(WIP_LABEL) ? [WIP_LABEL] : [];
  if (outcome === 'ADVANCE' || outcome === 'BOUNCE') {
    if (!toStage) {
      if (outcome === 'ADVANCE' && currentStage(labels) === 'ship') {
        // Terminal ADVANCE: PR is open; the merge closes the issue.
        return { removeLabels: ['stage:ship', ...wipRemove], addLabels: [], close: false };
      }
      throw new SdlcError(`${outcome} requires a target stage: sdlc emit <issue> <run-id> ${outcome} --to <stage>`);
    }
    const plan = planAdvance(labels, toStage);
    return { removeLabels: plan.removeLabels, addLabels: plan.addLabels, close: false };
  }
  if (outcome === 'PARK') {
    return { removeLabels: wipRemove, addLabels: ['sdlc:needs-human'], close: false };
  }
  if (outcome === 'CONTINUE') {
    return { removeLabels: wipRemove, addLabels: [], close: false };
  }
  // CLOSE
  return { removeLabels: wipRemove, addLabels: [], close: true };
}

/**
 * The most recent time `label` was added, from GitHub issue timeline events.
 * Returns an ISO string or null. Pure over the parsed `/timeline` payload.
 */
export function lastLabeledAt(timelineEvents, label) {
  let latest = null;
  for (const ev of timelineEvents ?? []) {
    if (ev.event === 'labeled' && ev.label?.name === label && ev.created_at) {
      if (latest === null || ev.created_at > latest) latest = ev.created_at;
    }
  }
  return latest;
}

/**
 * The most recent time `label` was removed, from GitHub issue timeline events.
 * Returns an ISO string or null. Pure over the parsed `/timeline` payload.
 * For `sdlc:wip` this is the claim-verify boundary (see `planClaimVerify`).
 */
export function lastUnlabeledAt(timelineEvents, label) {
  let latest = null;
  for (const ev of timelineEvents ?? []) {
    if (ev.event === 'unlabeled' && ev.label?.name === label && ev.created_at) {
      if (latest === null || ev.created_at > latest) latest = ev.created_at;
    }
  }
  return latest;
}

/**
 * Per-lane eligibility + depth from ONE open-issue snapshot, plus the
 * stage-label integrity check. `issues` is
 * `[{ number, labels:[{name}]|[name], createdAt }]`. Pure.
 *
 * Eligible = has that stage, not wip and not parked/hold; ordered exactly as a
 * worker's CLAIM would pick — priority (critical › unlabeled › future),
 * then FIFO by createdAt. `integrity` lists every issue whose stage-label
 * state is corrupt (see the zero-vs-flagged rule below). Each lane also
 * carries an `ineligible` breakdown ({hold, needs-human, wip} counts) so the
 * lanes report can explain a deep-but-idle lane at a glance.
 */
export function computeLanes(issues) {
  const norm = (issues ?? []).map((i) => ({
    number: i.number,
    createdAt: i.createdAt ?? '',
    labels: (i.labels ?? []).map((l) => (typeof l === 'string' ? l : l.name)),
  }));

  const lanes = {};
  for (const lane of WORKER_LANES.concat('queued')) {
    lanes[lane] = { depth: 0, eligible: [], ineligible: { hold: 0, 'needs-human': 0, wip: 0 } };
  }

  const integrity = [];
  for (const issue of norm) {
    const stages = stagesOf(issue.labels);
    // Multiple stage labels are always corrupt. Zero is only corrupt when the
    // issue still carries a machine flag (wip/needs-human) — a stage-less open
    // issue is otherwise a legitimate state: post-ship awaiting PR merge, or
    // simply not (yet) in the pipeline.
    const zeroButFlagged =
      stages.length === 0 &&
      (issue.labels.includes(WIP_LABEL) || issue.labels.includes('sdlc:needs-human'));
    if (stages.length > 1 || zeroButFlagged) {
      integrity.push({ number: issue.number, stages });
    }
    for (const stage of stages) {
      if (!lanes[stage]) continue; // unknown stage label — skip (currentStage guards elsewhere)
      lanes[stage].depth += 1;
      const parked = PARK_LABELS.some((p) => issue.labels.includes(p));
      const locked = issue.labels.includes(WIP_LABEL);
      if (!parked && !locked) {
        lanes[stage].eligible.push(issue);
      } else if (issue.labels.includes('sdlc:hold')) {
        // One bucket per ineligible issue so the breakdown sums to depth −
        // eligible count. Precedence: hold › needs-human › wip.
        lanes[stage].ineligible.hold += 1;
      } else if (issue.labels.includes('sdlc:needs-human')) {
        lanes[stage].ineligible['needs-human'] += 1;
      } else {
        lanes[stage].ineligible.wip += 1;
      }
    }
  }

  for (const lane of Object.keys(lanes)) {
    lanes[lane].eligible = lanes[lane].eligible
      .sort((a, b) => {
        const pr = priorityRank(a.labels) - priorityRank(b.labels);
        if (pr !== 0) return pr;
        return a.createdAt < b.createdAt ? -1 : a.createdAt > b.createdAt ? 1 : 0;
      })
      .map((i) => i.number);
  }

  return { lanes, integrity };
}

/**
 * Render a lane's ineligibility breakdown, e.g. `(hold 12, needs-human 4)`, from
 * the `ineligible` bucket counts a `computeLanes` lane carries. Pure.
 * Zero-count buckets are omitted; returns `''` when nothing is ineligible (a
 * fully-eligible lane, where depth == eligible count, prints unchanged). Bucket
 * order is fixed: hold, needs-human, wip.
 */
export function laneIneligibilityBreakdown(lane) {
  const buckets = lane?.ineligible ?? {};
  const parts = ['hold', 'needs-human', 'wip']
    .filter((k) => (buckets[k] ?? 0) > 0)
    .map((k) => `${k} ${buckets[k]}`);
  return parts.length ? `(${parts.join(', ')})` : '';
}

/** Post-worker self-heal check: did the worker clear its lock? Pure. */
export function planHeal(labelNames) {
  return { stillLocked: (labelNames ?? []).includes(WIP_LABEL) };
}

/**
 * Lane self-heal discovery: every open issue still carrying BOTH
 * `stage:<lane>` and `sdlc:wip` — a worker that claimed but never emitted an
 * outcome (a settled outcome drops wip and swaps the stage). Returns the stalled
 * issue numbers in ascending order. `issues` is `[{ number, labels }]`. Pure —
 * lets `sdlc heal <lane>` find its own targets with no issue argument.
 */
export function planHealLane(issues, lane) {
  return (issues ?? [])
    .map((i) => ({
      number: i.number,
      labels: (i.labels ?? []).map((l) => (typeof l === 'string' ? l : l.name)),
    }))
    .filter((i) => i.labels.includes(WIP_LABEL) && i.labels.includes(`stage:${lane}`))
    .map((i) => i.number)
    .sort((a, b) => a - b);
}

/**
 * Mint a dispatcher cycle run-id: `dispatch-<yyyymmdd>-<hhmm>-<hex4>`,
 * matching the format the dispatcher used to build by hand. `now` and `hex` are
 * injectable for deterministic tests; `hex` defaults to 2 random bytes. Pure.
 */
export function mintRunId(now = new Date(), hex = null) {
  const pad = (n) => String(n).padStart(2, '0');
  const yyyymmdd = `${now.getUTCFullYear()}${pad(now.getUTCMonth() + 1)}${pad(now.getUTCDate())}`;
  const hhmm = `${pad(now.getUTCHours())}${pad(now.getUTCMinutes())}`;
  const suffix = hex ?? randomBytes(2).toString('hex');
  return `dispatch-${yyyymmdd}-${hhmm}-${suffix}`;
}

/**
 * Which local branches are safe to delete as ancestry-merged into the default
 * branch. `mergedBranches` is the raw `git branch --merged <DEFAULT_BRANCH>`
 * line list. Never returns the default branch, the prod branch, or the current
 * branch. Pure — the caller still re-confirms each with
 * `git merge-base --is-ancestor` before deleting.
 */
export function planBranchPrune(mergedBranches, currentBranch) {
  const keep = new Set([DEFAULT_BRANCH, PROD_BRANCH, currentBranch]);
  return (mergedBranches ?? [])
    .map((line) => {
      // `git branch` marks the current branch with `* ` and a branch checked
      // out in a linked worktree with `+ `. Capture the marker + name.
      const m = line.match(/^([*+]?)\s*(\S+)/);
      return m ? { marker: m[1], name: m[2] } : null;
    })
    .filter((b) => b && b.name)
    // A `+ ` worktree-held branch is off-limits: `git branch -D` refuses it,
    // exactly like the current branch — never a prune candidate.
    .filter((b) => b.marker !== '+')
    .map((b) => b.name)
    .filter((b) => !keep.has(b));
}

/** Branch names whose upstream shows `[gone]` in `git branch -vv` output. Pure. */
export function parseGoneBranches(branchVvOutput, currentBranch) {
  const keep = new Set([DEFAULT_BRANCH, PROD_BRANCH, currentBranch]);
  const gone = [];
  for (const line of (branchVvOutput ?? '').split(/\r?\n/)) {
    if (!line.trim()) continue;
    // `  branch  <sha> [origin/branch: gone] msg` — leading `* ` marks the
    // current branch, `+ ` a branch checked out in a linked worktree.
    const m = line.match(/^([*+]?)\s*(\S+)\s+[0-9a-f]+\s+\[[^\]]*: gone\]/);
    // Skip `+ ` worktree-held branches: `git branch -D` refuses them.
    if (m && m[1] !== '+' && !keep.has(m[2])) gone.push(m[2]);
  }
  return gone;
}

/**
 * A squash-merged branch (invisible to `--merged`) may be deleted ONLY when all
 * three hold: upstream is gone, its PR is MERGED, and the local tip equals the
 * merged head SHA — so no local-only commits are lost. Pure.
 */
export function canDeleteSquashMerged({ upstreamGone, prState, localTip, headRefOid }) {
  return (
    upstreamGone === true &&
    prState === 'MERGED' &&
    Boolean(localTip) &&
    localTip === headRefOid
  );
}

/**
 * Parse `git worktree list --porcelain` into the issue-scoped sibling trees this
 * sweep owns: those whose path basename matches `worktreeName(repoName, <issue#>)`.
 * The main checkout and any human-named worktree never match, so they are never
 * candidates — the pattern is the safety boundary. Pure.
 *
 * @param {string} porcelain  raw `git worktree list --porcelain` output
 * @param {string} repoName   the main repo dir basename
 * @returns {Array<{ path: string, branch: string|null, issue: number }>}
 */
export function parseWorktrees(porcelain, repoName) {
  // Derive the regex from worktreeName() itself, so an adopter's rename of the
  // worktree convention automatically re-scopes the sweep: mark the issue slot
  // with a placeholder, escape the literal parts, swap in a digit capture.
  const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const marker = '\u0000';
  const pattern = new RegExp(
    `^${worktreeName(repoName, marker).split(marker).map(esc).join('(\\d+)')}$`,
  );
  const trees = [];
  for (const block of (porcelain ?? '').split(/\r?\n\r?\n/)) {
    const lines = block.split(/\r?\n/).filter(Boolean);
    if (!lines.length) continue;
    let wtPath = null;
    let branch = null;
    for (const line of lines) {
      if (line.startsWith('worktree ')) wtPath = line.slice('worktree '.length).trim();
      else if (line.startsWith('branch ')) {
        branch = line.slice('branch '.length).trim().replace(/^refs\/heads\//, '');
      }
    }
    if (!wtPath) continue;
    // basename regardless of separator (git prints POSIX slashes even on Windows).
    const base = wtPath.split(/[\\/]/).filter(Boolean).pop() ?? '';
    const m = base.match(pattern);
    if (m) trees.push({ path: wtPath, branch, issue: Number(m[1]) });
  }
  return trees;
}

/**
 * Classify a worktree's `git status --porcelain` output for sweep tolerance.
 * Returns `{ clean, strays }`. A tree is "clean modulo strays" — `clean:true`
 * with the stray paths listed — only when EVERY porcelain entry is an untracked
 * (`??`) path AND every such path is 0 bytes; those strays are provably not work
 * (mangled `> OUTCOME` redirects per #688) and may be cleared. Any tracked
 * modification, any untracked file with size > 0, or an unreadable/quoted path →
 * `clean:false` with no strays (real work — never touch). Pure: `sizeOf(relPath)`
 * is injected so the caller supplies filesystem access; it may throw, which is
 * treated conservatively as dirty.
 *
 * @param {string} porcelain raw `git status --porcelain` output
 * @param {(relPath:string)=>number} sizeOf byte size of a path within the tree
 * @returns {{ clean: boolean, strays: string[] }}
 */
export function classifyStatusForSweep(porcelain, sizeOf) {
  const lines = String(porcelain ?? '')
    .split('\n')
    .map((l) => l.replace(/\r$/, ''))
    .filter((l) => l.length > 0);
  if (lines.length === 0) return { clean: true, strays: [] };
  const strays = [];
  for (const line of lines) {
    // porcelain v1 untracked entries are exactly '?? <path>'. Anything else
    // (tracked staged/modified, quoted special-char path) is real → dirty.
    if (!line.startsWith('?? ')) return { clean: false, strays: [] };
    const rel = line.slice(3);
    if (rel.startsWith('"')) return { clean: false, strays: [] }; // quoted path — treat as real
    let size;
    try {
      size = sizeOf(rel);
    } catch {
      return { clean: false, strays: [] };
    }
    if (size !== 0) return { clean: false, strays: [] };
    strays.push(rel);
  }
  return { clean: true, strays };
}

/**
 * Unlink symlink/junction entries at the root of a worktree that is about to be
 * removed. On Windows, `git worktree remove` recurses THROUGH an NTFS junction
 * and deletes the junction TARGET's contents (#723: a sweep emptied a shared
 * main-checkout `node_modules/.bin` via a stale worktree's `node_modules`
 * junction before aborting on a locked file). Removing the link itself — never
 * its target — is safe on every platform: `lstat` reports junctions as
 * symlinks, and a non-recursive `rmSync` unlinks the reparse point without
 * touching what it points at. Only root entries are scanned; the lane
 * convention creates at most one root-level `node_modules` junction.
 *
 * @param {string} treePath worktree root about to be handed to `git worktree remove`
 * @param {(msg:string)=>void} log progress logger
 * @returns {string[]} names of the entries that were unlinked
 */
export function unlinkWorktreeRootLinks(treePath, log = () => {}) {
  const removed = [];
  let entries;
  try {
    entries = fs.readdirSync(treePath, { withFileTypes: true });
  } catch {
    return removed; // tree already gone — nothing to protect
  }
  for (const ent of entries) {
    const full = path.join(treePath, ent.name);
    try {
      if (!fs.lstatSync(full).isSymbolicLink()) continue;
      fs.rmSync(full, { recursive: false, force: true });
      removed.push(ent.name);
      log(`  unlinked ${ent.name} (junction/symlink — git worktree remove would delete through it)`);
    } catch (err) {
      log(`  could not unlink ${full}: ${String(err.message).split('\n')[0]}`);
    }
  }
  return removed;
}

/**
 * Decide which issue-scoped worktrees to remove. A tree is removable ONLY when
 * it is clean AND done — done meaning its branch is gone/merged OR its issue is
 * closed. Dirty-but-done trees and still-active trees are left with a reason, so
 * uncommitted work is never destroyed. Pure — the caller gathers each tree's
 * `clean` / `branchGone` / `issueClosed` status and executes the plan.
 *
 * @param {Array<{path,branch,issue,clean,branchGone,issueClosed}>} trees
 * @returns {{ remove: Array, leave: Array }} each entry carries a `reason`
 */
export function planWorktreeSweep(trees) {
  const remove = [];
  const leave = [];
  for (const t of trees ?? []) {
    const done = [];
    if (t.branchGone) done.push('branch gone/merged');
    if (t.issueClosed) done.push('issue closed');
    if (done.length === 0) {
      leave.push({ ...t, reason: 'active (branch present, issue open)' });
    } else if (!t.clean) {
      leave.push({ ...t, reason: `dirty — ${done.join(', ')} but uncommitted changes present` });
    } else {
      remove.push({ ...t, reason: done.join(', ') });
    }
  }
  return { remove, leave };
}

/**
 * Digest numbers from a snapshot: per-lane depth, parked/hold lists, and — when
 * a previous snapshot is supplied — the arrivals/departures diff vs last cycle
 * plus per-list parked/hold deltas (so the near-identical parked/hold lists can
 * be reported as "no change" instead of re-listing every cycle). `issues` is
 * `[{ number, labels }]`. `prev` is either the legacy array of last cycle's open
 * issue numbers, or `{ current, parked, hold }` from the persisted state. Pure.
 */
export function computeDigest(issues, prev = null) {
  const prevObj = Array.isArray(prev) ? { current: prev } : prev;
  const prevNumbers = prevObj ? (prevObj.current ?? null) : null;
  const norm = (issues ?? []).map((i) => ({
    number: i.number,
    labels: (i.labels ?? []).map((l) => (typeof l === 'string' ? l : l.name)),
  }));

  const depths = {};
  for (const lane of WORKER_LANES.concat('queued')) depths[lane] = 0;
  const parked = [];
  const hold = [];
  for (const issue of norm) {
    for (const stage of stagesOf(issue.labels)) {
      if (depths[stage] !== undefined) depths[stage] += 1;
    }
    if (issue.labels.includes('sdlc:needs-human')) parked.push(issue.number);
    if (issue.labels.includes('sdlc:hold')) hold.push(issue.number);
  }

  const current = norm.map((i) => i.number);
  let arrivals = null;
  let departures = null;
  if (prevNumbers) {
    const prevSet = new Set(prevNumbers);
    const now = new Set(current);
    arrivals = current.filter((n) => !prevSet.has(n));
    departures = prevNumbers.filter((n) => !now.has(n));
  }

  const diffSet = (prevArr, nowArr) => {
    const p = new Set(prevArr);
    const n = new Set(nowArr);
    const added = nowArr.filter((x) => !p.has(x));
    const removed = prevArr.filter((x) => !n.has(x));
    return { added, removed, changed: added.length > 0 || removed.length > 0 };
  };
  const parkedDelta = prevObj && Array.isArray(prevObj.parked) ? diffSet(prevObj.parked, parked) : null;
  const holdDelta = prevObj && Array.isArray(prevObj.hold) ? diffSet(prevObj.hold, hold) : null;

  return { depths, parked, hold, current, arrivals, departures, parkedDelta, holdDelta };
}

/**
 * The intake merge-sweep work-list (intake.md step 0). Pure.
 *
 * Maps recently-merged PRs → the issues each closed → the open issues that
 * *depend on* those closed issues (a `#<n>` reference in the body), flagging
 * which dependents carry the `blocked` label — the cascade-unblock candidates
 * intake flips Blocked → Ready. A PR already in `sweptPrs`, one that closed no
 * issue, or (when `sinceMs` is given) one merged at/before that cutoff is
 * skipped, so the sweep is idempotent and bounded. No I/O and no writes — the
 * intake worker consumes the list and performs the actions itself.
 *
 * @param {Array}  mergedPrs   `[{ number, mergedAt, title, closes: [issueNum] }]`
 * @param {Array}  openIssues  `[{ number, title, labels: [name]|[{name}], body }]`
 * @param {object} [opts]      `{ sinceMs: number|null, sweptPrs: Array<number> }`
 * @returns {{ items: Array, closedIssues: number[], empty: boolean }}
 */
export function computeSweep(mergedPrs, openIssues, { sinceMs = null, sweptPrs = [] } = {}) {
  const swept = new Set((sweptPrs ?? []).map(Number));
  const issues = (openIssues ?? []).map((i) => ({
    number: i.number,
    title: i.title ?? '',
    labels: (i.labels ?? []).map((l) => (typeof l === 'string' ? l : l.name)),
    body: i.body ?? '',
  }));

  // Open issues that reference `#<closedNum>` in their body (word-boundaried so
  // `#12` never matches `#123`), flagged if they carry the `blocked` label.
  const dependentsOf = (closedNum) => {
    const ref = new RegExp(`#${closedNum}(?!\\d)`);
    return issues
      .filter((i) => i.number !== closedNum && ref.test(i.body))
      .map((i) => ({ number: i.number, title: i.title, blocked: i.labels.includes('blocked') }));
  };

  const items = [];
  for (const pr of mergedPrs ?? []) {
    if (swept.has(Number(pr.number))) continue;
    if (sinceMs !== null && pr.mergedAt && new Date(pr.mergedAt).getTime() <= sinceMs) continue;
    const closes = (pr.closes ?? []).map((n) => ({ number: n, dependents: dependentsOf(n) }));
    if (!closes.length) continue; // closed no issue — nothing to cascade
    items.push({ number: pr.number, mergedAt: pr.mergedAt ?? null, title: pr.title ?? '', closes });
  }

  const closedIssues = [...new Set(items.flatMap((it) => it.closes.map((c) => c.number)))];
  return { items, closedIssues, empty: items.length === 0 };
}

/**
 * The next `sweptPrs` marker after an ack (intake.md step 0). Pure.
 *
 * Retains only prior swept PRs still present in the fetched merged list (bounds
 * the marker to ≤ the fetch limit) plus every PR merged inside the window.
 * `newlyAcked` is the in-window set not already marked — what this ack adds.
 *
 * @param {Array}  mergedPrs  `[{ number, mergedAt }]`
 * @param {object} [opts]     `{ sinceMs: number|null, sweptPrs: Array<number> }`
 * @returns {{ nextSwept: number[], newlyAcked: number[] }}
 */
export function computeSweepAck(mergedPrs, { sinceMs = null, sweptPrs = [] } = {}) {
  const prior = (sweptPrs ?? []).map(Number);
  const fetchedNums = new Set((mergedPrs ?? []).map((p) => Number(p.number)));
  const inWindow = (mergedPrs ?? [])
    .filter((p) => sinceMs === null || (p.mergedAt && new Date(p.mergedAt).getTime() > sinceMs))
    .map((p) => Number(p.number));
  const priorSet = new Set(prior);
  const nextSwept = [...new Set([...prior.filter((n) => fetchedNums.has(n)), ...inWindow])];
  const newlyAcked = inWindow.filter((n) => !priorSet.has(n));
  return { nextSwept, newlyAcked };
}

/**
 * Stages a conflicting-PR issue is bounced back to `build` from. A PR only
 * exists once ship opens it, so a still-labelled issue with an open conflicting
 * PR was bounced back into the pipeline; verify/audit/ship all return to build,
 * where conflict resolution lives (dispatch step 0a.3). A post-ship *stageless*
 * issue (terminal ADVANCE dropped stage:ship) gets the nudge comment only.
 */
export const CONFLICT_BOUNCE_STAGES = ['verify', 'audit', 'ship'];

/** The fixed conflict-nudge comment body (deterministic template — no judgment). */
export function conflictCommentBody(branch) {
  return `sdlc-dispatch: branch ${branch} conflicts with ${DEFAULT_BRANCH} — needs a merge`;
}

/**
 * Deterministic conflict-scan plan (dispatch step 0a.3). Pure.
 *
 *   `prs`           `[{ number, headRefName, baseRefName, mergeable, linkedIssues:[num] }]`
 *   `issueInfo`     `{ [number]: { state, labels:[name], comments:[{ body, createdAt }] } }`
 *   `baseUpdatedAt` the base branch's tip commit ISO time — a conflict comment at
 *                   or after it counts as already-posted-since-the-last-base-change
 *                   (idempotency); null disables the watermark (any prior comment
 *                   suppresses).
 *   `baseBranch`    only PRs targeting this branch are considered (default DEFAULT_BRANCH).
 *
 * For each open CONFLICTING PR into `baseBranch`, and each linked issue that is
 * open and not wip/needs-human/hold, plan the fixed conflict comment (unless one
 * already exists since the last base-branch update) and, when the issue sits in
 * verify/audit/ship, a bounce back to build. Never plans any PR mutation.
 */
export function planConflictScan(prs, issueInfo = {}, baseUpdatedAt = null, baseBranch = DEFAULT_BRANCH) {
  const watermark = baseUpdatedAt ? new Date(baseUpdatedAt).getTime() : null;
  const actions = [];
  const skipped = [];
  for (const pr of prs ?? []) {
    if (pr.baseRefName && pr.baseRefName !== baseBranch) continue;
    if (pr.mergeable !== 'CONFLICTING') continue;
    const branch = pr.headRefName;
    const linked = pr.linkedIssues ?? [];
    if (linked.length === 0) {
      skipped.push({ pr: pr.number, reason: 'no linked issue' });
      continue;
    }
    for (const num of linked) {
      const info = issueInfo?.[num];
      if (!info || info.state === 'CLOSED') {
        skipped.push({ pr: pr.number, issue: num, reason: 'linked issue closed or not found' });
        continue;
      }
      const labels = info.labels ?? [];
      if (labels.includes(WIP_LABEL) || PARK_LABELS.some((p) => labels.includes(p))) {
        skipped.push({ pr: pr.number, issue: num, reason: 'issue is wip/needs-human/hold' });
        continue;
      }
      const body = conflictCommentBody(branch);
      const alreadyPosted = (info.comments ?? []).some(
        (c) =>
          (c.body ?? '').startsWith(body) &&
          (watermark === null || new Date(c.createdAt).getTime() >= watermark),
      );
      if (alreadyPosted) {
        skipped.push({
          pr: pr.number,
          issue: num,
          reason: `conflict comment already posted since last ${baseBranch} update`,
        });
        continue;
      }
      let stage = null;
      try {
        stage = currentStage(labels);
      } catch {
        stage = null; // post-ship stageless (or corrupt) — comment only, never guess a stage
      }
      const advanceTo = CONFLICT_BOUNCE_STAGES.includes(stage) ? 'build' : null;
      actions.push({ pr: pr.number, issue: num, branch, comment: body, stage, advanceTo });
    }
  }
  return { actions, skipped };
}

// --- Duplicate-candidate scoring ------------------------------------------------

/**
 * Common words carrying no discriminating signal — dropped from a dup-check
 * query so a phrase like "add a bar to the menu" scores on {add, bar, menu},
 * not on {a, to, the}.
 */
export const DUP_STOPWORDS = new Set([
  'a', 'an', 'and', 'or', 'of', 'to', 'in', 'on', 'for', 'is', 'it', 'with',
  'by', 'as', 'at', 'be', 'this', 'that', 'from', 'into', 'over', 'per', 'the',
  'when', 'via', 'not', 'no', 'so', 'if', 'but', 'are', 'was', 'has', 'can',
]);

/** Lowercase alphanumeric tokens of a string (defensive over null/undefined). */
export function dupTokens(text) {
  return String(text ?? '').toLowerCase().match(/[a-z0-9]+/g) ?? [];
}

/** The de-duplicated, meaningful (non-stopword, length ≥ 2) terms of a query. */
export function dupQueryTerms(query) {
  return [...new Set(dupTokens(query).filter((t) => t.length >= 2 && !DUP_STOPWORDS.has(t)))];
}

/**
 * Rank open issues by keyword overlap with a query — the deterministic dup-check
 * baseline (a semantic backend is a deferred phase 2). Judgment (is it REALLY a
 * dup) stays with the caller; this only supplies the candidate set. Pure — the
 * caller injects the open-issue snapshot.
 *
 *   `query`   free-text title/keywords.
 *   `issues`  `[{ number, title, body, labels }]` (labels: name-strings or {name}).
 *   `opts`    { titleWeight=3, bodyWeight=1, labelWeight=1, limit=10, exclude=null }.
 *
 * Per query term: a title hit scores `titleWeight`, else a body hit scores
 * `bodyWeight`; a label hit adds `labelWeight` on top (a hint, not a substitute).
 * Ranked by descending score, then ascending issue number (stable + deterministic).
 * Zero-score issues drop out. `exclude` omits one issue (e.g. the query's own).
 */
export function scoreDupCandidates(query, issues, opts = {}) {
  const { titleWeight = 3, bodyWeight = 1, labelWeight = 1, limit = 10, exclude = null } = opts;
  const terms = dupQueryTerms(query);
  if (terms.length === 0) return [];
  const excludeNum = exclude == null || exclude === '' ? null : Number(String(exclude).replace(/^#/, ''));
  const scored = [];
  for (const issue of issues ?? []) {
    if (excludeNum != null && Number(issue.number) === excludeNum) continue;
    const titleSet = new Set(dupTokens(issue.title));
    const bodySet = new Set(dupTokens(issue.body));
    const labelSet = new Set(
      (issue.labels ?? []).flatMap((l) => dupTokens(typeof l === 'string' ? l : l?.name)),
    );
    let score = 0;
    const hits = [];
    for (const term of terms) {
      let termScore = 0;
      if (titleSet.has(term)) termScore += titleWeight;
      else if (bodySet.has(term)) termScore += bodyWeight;
      if (labelSet.has(term)) termScore += labelWeight;
      if (termScore > 0) {
        score += termScore;
        hits.push(term);
      }
    }
    if (score > 0) scored.push({ number: Number(issue.number), title: issue.title ?? '', score, hits });
  }
  scored.sort((a, b) => b.score - a.score || a.number - b.number);
  return limit && limit > 0 ? scored.slice(0, limit) : scored;
}

// --- I/O boundary: gh / git executors (injectable for tests) -------------------

/**
 * Every outbound `gh` write funnels through here, so the privacy guard lives
 * here too rather than at each `issue comment` call site — a new call site
 * cannot forget it.
 *
 * This matters because the dispatcher's whole job is quoting diffs, repro steps
 * and issue text back into comments: a real identifier that reaches the repo
 * once gets republished by the lanes many times over. See AGENTS.md
 * §"Privacy posture".
 */
export function guardOutboundBody(args, scan = assertClean) {
  const isComment = args.includes('comment') || args.includes('create') || args.includes('edit');
  if (!isComment) return;
  const bodyIdx = args.indexOf('--body');
  if (bodyIdx !== -1 && args[bodyIdx + 1] != null) {
    scan(String(args[bodyIdx + 1]), 'this issue comment');
  }
  const fileIdx = args.indexOf('--body-file');
  if (fileIdx !== -1 && args[fileIdx + 1] != null) {
    const p = String(args[fileIdx + 1]);
    if (p !== '-' && fs.existsSync(p)) scan(fs.readFileSync(p, 'utf8'), `this comment body (${p})`);
  }
}

const defaultGh = (args) => {
  guardOutboundBody(args);
  return execFileSync('gh', args, { encoding: 'utf8' });
};
const defaultGit = (args) => execFileSync('git', args, { encoding: 'utf8' });

/** Fetch an issue's label names via gh. */
function fetchLabelNames(gh, issue) {
  const out = gh(['issue', 'view', String(issue), '--json', 'labels', '--jq', '.labels[].name']);
  return out
    .split(/\r?\n/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function requireIssue(issue) {
  if (!issue || !/^#?\d+$/.test(String(issue))) {
    throw new SdlcError(`expected an issue number, got "${issue ?? ''}"`);
  }
  return String(issue).replace(/^#/, '');
}

/** One open-issue snapshot that serves a whole cycle (Step 0). */
function snapshotOpenIssues(gh, fields = 'number,labels,createdAt,title') {
  return JSON.parse(gh(['issue', 'list', '--state', 'open', '--json', fields, '--limit', '200']));
}

/** Human-readable lock age. */
function fmtAge(ms) {
  const h = Math.floor(ms / 3600000);
  const m = Math.floor((ms % 3600000) / 60000);
  return h > 0 ? `${h}h${m}m` : `${m}m`;
}

/** An issue's comments as `[{ body, createdAt }]` (chronological). */
function fetchLockComments(gh, issue) {
  return JSON.parse(
    gh(['issue', 'view', String(issue), '--json', 'comments', '--jq', '[.comments[] | {body: .body, createdAt: .createdAt}]']),
  );
}

/**
 * The claim-verify boundary for an issue: the newest `sdlc:wip` unlabeled
 * timeline event. Unavailable → null, which degrades safe (planClaimVerify
 * falls back to the outcome-marker settle signal alone).
 */
function fetchClaimBoundary(gh, issue) {
  try {
    const timeline = JSON.parse(
      gh(['api', `repos/{owner}/{repo}/issues/${issue}/timeline`, '--paginate']),
    );
    return lastUnlabeledAt(timeline, WIP_LABEL);
  } catch {
    return null;
  }
}

/**
 * README CLAIM step 3: on a lost race, only the loser's own claim comment may
 * be edited to note `superseded` — the winner's claim and the label stay
 * untouched. Cosmetic: losing cleanly matters more than the note.
 */
function markClaimSuperseded(gh, commentUrl, runId, lane) {
  const own = String(commentUrl ?? '').match(/issuecomment-(\d+)/);
  if (!own) return;
  try {
    gh(['api', '-X', 'PATCH', `repos/{owner}/{repo}/issues/comments/${own[1]}`, '-f', `body=sdlc:claim ${runId} ${lane} (superseded)`]);
  } catch {
    // cosmetic — losing cleanly matters more than the note
  }
}

// --- Commands ------------------------------------------------------------------

/** Shared success report for a won claim (single or --next). */
function reportClaim(git, log, issue, runId, lane) {
  const branch = git(['rev-parse', '--abbrev-ref', 'HEAD']).trim();
  const status = git(['status', '--short']).trimEnd();
  log(runId ? `claimed #${issue} (+${WIP_LABEL}, claim ${runId} ${lane})` : `claimed #${issue} (+${WIP_LABEL})`);
  log(`branch: ${branch}`);
  log(status ? `status:\n${status}` : 'status: clean');
}

/**
 * `sdlc claim --next <lane> <run-id>`: let the CLI pick the next eligible issue
 * for the lane (same ordering as `computeLanes()` / the worker CLAIM rule) and
 * claim it atomically with the same `--verify` race check as a single claim.
 * On a lost race it retries the next eligible item; an empty lane exits non-zero
 * with `idle` so a worker can pass straight through.
 */
function cmdClaimNext(args, nextIdx, { gh, git, log }) {
  const lane = args[nextIdx + 1];
  // Everything that is neither the --next flag, its lane value, nor another
  // flag is positional; the sole positional is the run-id.
  const positional = args.filter((a, i) => i !== nextIdx && i !== nextIdx + 1 && !a.startsWith('--'));
  const runId = positional[0];
  if (!lane || lane.startsWith('--')) {
    throw new SdlcError('claim --next requires a lane and run-id: sdlc claim --next <lane> <run-id>');
  }
  if (!runId) {
    throw new SdlcError('claim --next requires a run-id: sdlc claim --next <lane> <run-id>');
  }
  if (!WORKER_LANES.includes(lane)) {
    throw new SdlcError(`unknown lane "${lane}" (valid: ${WORKER_LANES.join(', ')})`);
  }

  const { lanes } = computeLanes(snapshotOpenIssues(gh));
  const eligible = lanes[lane]?.eligible ?? [];
  if (eligible.length === 0) {
    log(`claim --next ${lane}: idle — no eligible issues.`);
    process.exitCode = 1;
    return;
  }

  for (const number of eligible) {
    const issue = String(number);
    gh(['issue', 'edit', issue, '--add-label', WIP_LABEL]);
    const myCommentUrl = String(gh(['issue', 'comment', issue, '--body', `sdlc:claim ${runId} ${lane}`]) ?? '').trim();
    const result = planClaimVerify(fetchLockComments(gh, issue), runId, fetchClaimBoundary(gh, issue));
    if (result.won) {
      log(`claim: verified — ${runId} owns #${issue}.`);
      reportClaim(git, log, issue, runId, lane);
      return;
    }
    // Lost this one: leave the winner's label untouched, mark our own losing
    // claim superseded, and try the next eligible item (README CLAIM step 2).
    markClaimSuperseded(gh, myCommentUrl, runId, lane);
    log(`claim: LOST race on #${issue} to ${result.winner ?? '(unknown)'} — trying next eligible.`);
  }
  log(`claim --next ${lane}: idle — every eligible issue lost to a concurrent claim.`);
  process.exitCode = 1;
}

function cmdClaim(args, { gh, git, log }) {
  const nextIdx = args.indexOf('--next');
  if (nextIdx >= 0) {
    cmdClaimNext(args, nextIdx, { gh, git, log });
    return;
  }
  const verify = args.includes('--verify');
  const positional = args.filter((a) => a !== '--verify');
  const issue = requireIssue(positional[0]);
  const runId = positional[1];
  const lane = positional[2];
  if (runId && !lane) {
    throw new SdlcError('claim with a run-id also requires a lane: sdlc claim <issue> <run-id> <lane>');
  }
  if (verify && !runId) {
    throw new SdlcError('--verify requires a run-id + lane: sdlc claim <issue> <run-id> <lane> --verify');
  }
  gh(['issue', 'edit', issue, '--add-label', WIP_LABEL]);
  let myCommentUrl = null;
  if (runId) {
    // The label is the visibility signal; this comment is the ownership record
    // and race tiebreaker (see prompts/sdlc/README.md CLAIM).
    myCommentUrl = String(gh(['issue', 'comment', issue, '--body', `sdlc:claim ${runId} ${lane}`]) ?? '').trim();
  }
  if (verify) {
    const result = planClaimVerify(fetchLockComments(gh, issue), runId, fetchClaimBoundary(gh, issue));
    if (!result.won) {
      markClaimSuperseded(gh, myCommentUrl, runId, lane);
      log(`claim: LOST race on #${issue} to ${result.winner ?? '(unknown)'} — leave the label, pick the next item.`);
      process.exitCode = 1;
      return;
    }
    log(`claim: verified — ${runId} owns #${issue}.`);
  }
  reportClaim(git, log, issue, runId, lane);
}

function cmdEmit(args, { gh, log }) {
  const toIdx = args.indexOf('--to');
  const bodyIdx = args.indexOf('--body');
  const bodyFileIdx = args.indexOf('--body-file');
  const toStage = toIdx >= 0 ? args[toIdx + 1] : null;
  const bodyText = bodyIdx >= 0 ? args[bodyIdx + 1] : null;
  const bodyFile = bodyFileIdx >= 0 ? args[bodyFileIdx + 1] : null;
  const flagValueIdx = new Set(
    [toIdx, bodyIdx, bodyFileIdx].filter((i) => i >= 0).map((i) => i + 1),
  );
  const positional = args.filter((a, i) => !a.startsWith('--') && !flagValueIdx.has(i));
  const issue = requireIssue(positional[0]);
  const runId = positional[1];
  const outcome = positional[2];
  if (!runId || !outcome) {
    throw new SdlcError('emit requires a run-id and outcome: sdlc emit <issue> <run-id> <outcome> [--to <stage>] [--body <text>|--body-file <file>]');
  }
  if (bodyText !== null && bodyFile !== null) {
    throw new SdlcError('emit takes --body or --body-file, not both');
  }

  // Ownership: only the live claim's winner may settle it. A worker that lost
  // its race (or whose claim was already settled/reaped) must not emit.
  const verdict = planClaimVerify(fetchLockComments(gh, issue), runId, fetchClaimBoundary(gh, issue));
  if (!verdict.won) {
    throw new SdlcError(
      verdict.reason === 'own claim not found'
        ? `emit refused: no live sdlc:claim for ${runId} on #${issue} (already settled or never claimed)`
        : `emit refused: #${issue} is owned by ${verdict.winner}, not ${runId}`,
    );
  }

  const labels = fetchLabelNames(gh, issue);
  const plan = planEmit(labels, outcome, toStage);

  // Marker comment first: it is the completion record claim --verify keys off.
  // If the label edit below then fails, heal reports STALLED but no later
  // claimer can falsely lose to this (now settled) claim.
  const body = bodyFile ? fs.readFileSync(bodyFile, 'utf8') : (bodyText ?? '');
  const marker = `sdlc:emit ${runId} ${outcome}${toStage ? ` → stage:${toStage}` : ''}`;
  gh(['issue', 'comment', issue, '--body', body ? `${marker}\n\n${body}` : marker]);

  if (plan.removeLabels.length || plan.addLabels.length) {
    const editArgs = ['issue', 'edit', issue];
    for (const label of plan.removeLabels) editArgs.push('--remove-label', label);
    for (const label of plan.addLabels) editArgs.push('--add-label', label);
    gh(editArgs);
  }
  if (plan.close) {
    gh(['issue', 'close', issue]);
  }
  const labelNote = [
    plan.removeLabels.length ? `removed ${plan.removeLabels.join(', ')}` : null,
    plan.addLabels.length ? `added ${plan.addLabels.join(', ')}` : null,
    plan.close ? 'closed' : null,
  ].filter(Boolean).join('; ');
  log(`emit: #${issue} ${outcome}${toStage ? ` → stage:${toStage}` : ''} (${labelNote || 'no label change'})`);
}

function cmdAdvance(args, { gh, log }) {
  const issue = requireIssue(args[0]);
  const toStage = args[1];
  if (!toStage) {
    throw new SdlcError('advance requires a target stage: sdlc advance <issue> <to-stage>');
  }
  const labels = fetchLabelNames(gh, issue);
  const plan = planAdvance(labels, toStage);
  const editArgs = ['issue', 'edit', issue];
  for (const label of plan.removeLabels) {
    editArgs.push('--remove-label', label);
  }
  for (const label of plan.addLabels) {
    editArgs.push('--add-label', label);
  }
  gh(editArgs);
  log(`#${issue}: stage:${plan.from} → stage:${plan.to} (removed ${plan.removeLabels.join(', ')})`);
}

function cmdContext(args, { gh, git, log }) {
  const issue = requireIssue(args[0]);
  const branch = git(['rev-parse', '--abbrev-ref', 'HEAD']).trim();
  const status = git(['status', '--short']).trimEnd();
  const view = JSON.parse(
    gh(['issue', 'view', issue, '--json', 'number,title,state,labels']),
  );
  const labels = (view.labels ?? []).map((l) => l.name);
  const prs = JSON.parse(gh(['pr', 'list', '--head', branch, '--json', 'number,title,state']));

  log(`#${view.number} [${view.state}] ${view.title}`);
  log(`stage: ${labels.find((n) => n.startsWith('stage:')) ?? '(none)'}`);
  const flags = labels.filter((n) => n.startsWith('sdlc:') || n.startsWith('priority:'));
  log(`labels: ${flags.length ? flags.join(', ') : '(none relevant)'}`);
  log(`branch: ${branch}`);
  log(status ? `status:\n${status}` : 'status: clean');
  if (prs.length) {
    log(`prs (head ${branch}): ${prs.map((p) => `#${p.number} [${p.state}]`).join(', ')}`);
  } else {
    log(`prs (head ${branch}): none`);
  }
}

function cmdWorktree(args, { git, log }, root) {
  const issue = requireIssue(args[0]);
  const branch = args[1] ?? `feat/${issue}`;
  const repoName = path.basename(root);
  const target = path.resolve(root, '..', worktreeName(repoName, issue));
  git(['fetch', 'origin']);
  // Reuse an existing branch if present; otherwise create it off the default branch.
  const existing = git(['branch', '--list', branch]).trim();
  if (existing) {
    git(['worktree', 'add', target, branch]);
  } else {
    git(['worktree', 'add', '-b', branch, target, `origin/${DEFAULT_BRANCH}`]);
  }
  log(`worktree: ${target} (branch ${branch})`);
}

function cmdComment(args, { gh, log }) {
  const issue = requireIssue(args[0]);
  const file = args[1];
  if (!file) {
    throw new SdlcError('comment requires a body file: sdlc comment <issue> <file>');
  }
  gh(['issue', 'comment', issue, '--body-file', file]);
  log(`#${issue}: comment posted from ${file}`);
}

/**
 * `sdlc dup-check "<title or keywords>" [--limit N] [--exclude <issue>]`:
 * deterministic dup-candidate search over open issues. Prints ranked
 * `#<n> <title> (score N)` or `no candidates`, and sets exit code 2 when any
 * candidate is found — so a caller (intake's dup step, retro triage, the
 * issue-filing ritual) can branch on it (0 = clean, 2 = review these, 1 = usage
 * error). Judgment stays with the caller; the CLI only supplies the set.
 */
function cmdDupCheck(args, { gh, log }) {
  const limitIdx = args.indexOf('--limit');
  const excludeIdx = args.indexOf('--exclude');
  const limit = limitIdx >= 0 ? Number(args[limitIdx + 1]) : 10;
  const exclude = excludeIdx >= 0 ? args[excludeIdx + 1] : null;
  const flagValueIdx = new Set([limitIdx, excludeIdx].filter((i) => i >= 0).map((i) => i + 1));
  const positional = args.filter((a, i) => !a.startsWith('--') && !flagValueIdx.has(i));
  const query = positional.join(' ').trim();
  if (!query) {
    throw new SdlcError('dup-check requires a query: sdlc dup-check "<title or keywords>" [--limit N] [--exclude <issue>]');
  }
  const terms = dupQueryTerms(query);
  if (terms.length === 0) {
    throw new SdlcError(`dup-check: no usable search terms in "${query}" (all stopwords or too short)`);
  }
  const issues = snapshotOpenIssues(gh, 'number,title,body,labels');
  const candidates = scoreDupCandidates(query, issues, { limit, exclude });
  if (candidates.length === 0) {
    log(`dup-check: no candidates for "${query}" — ${issues.length} open issue(s) scanned (terms: ${terms.join(', ')}).`);
    return; // exit 0 — clean
  }
  log(`dup-check: ${candidates.length} candidate(s) for "${query}" (terms: ${terms.join(', ')}):`);
  for (const c of candidates) {
    log(`  #${c.number} ${c.title} (score ${c.score})`);
  }
  log('dup-check: judge each — the CLI ranks, you decide. Exit 2 = candidates found.');
  process.exitCode = 2; // candidates found — caller must judge
}

function cmdGate(args, { gh, log }) {
  const reap = args.includes('--reap');
  const snapshot = snapshotOpenIssues(gh, 'number,labels,updatedAt');
  const wip = snapshot.filter((i) => (i.labels ?? []).some((l) => l.name === WIP_LABEL));
  const now = Date.now();
  const wipItems = wip.map((i) => {
    // Accurate age from the sdlc:wip labeled event, not updatedAt (which any
    // later comment falsely refreshes).
    let addedAt = null;
    try {
      const timeline = JSON.parse(
        gh(['api', `repos/{owner}/{repo}/issues/${i.number}/timeline`, '--paginate']),
      );
      addedAt = lastLabeledAt(timeline, WIP_LABEL);
    } catch {
      addedAt = null; // fall back below
    }
    const stamp = addedAt ?? i.updatedAt;
    const ageMs = now - new Date(stamp).getTime();
    return { number: i.number, ageMs, viaTimeline: addedAt !== null };
  });

  const plan = planGate(wipItems);
  for (const it of plan.live) {
    log(`gate: LIVE #${it.number} (${fmtAge(it.ageMs)}) — worker running; ineligible this cycle.`);
  }
  for (const it of plan.reap) {
    log(`gate: REAP #${it.number} (stale ${fmtAge(it.ageMs)})`);
    if (reap) {
      // Verify-before-write: the snapshot may be stale under concurrent
      // dispatchers — another run may have reaped this issue and a new worker
      // claimed it since. Re-fetch the newest claim and recompute the age
      // immediately before writing.
      let freshAgeMs = null;
      try {
        const claims = fetchLockComments(gh, it.number).filter((c) => /^sdlc:claim\s/.test(c.body ?? ''));
        const newest = claims[claims.length - 1];
        if (newest) freshAgeMs = Date.now() - new Date(newest.createdAt).getTime();
      } catch {
        freshAgeMs = null;
      }
      if (freshAgeMs !== null && freshAgeMs < WIP_STALE_MS) {
        log(`  reap skipped — fresh claim (${fmtAge(freshAgeMs)}); another run got here first.`);
        continue;
      }
      gh(['issue', 'edit', String(it.number), '--remove-label', WIP_LABEL]);
      gh([
        'issue',
        'comment',
        String(it.number),
        '--body',
        'sdlc-dispatch: reaped stale sdlc:wip lock (no activity ≥2h — worker presumed dead). Item re-enters its lane. Its worktree is left in place for reuse.',
      ]);
      log(`  reaped: removed ${WIP_LABEL} from #${it.number}`);
    }
  }
  if (plan.reap.length && !reap) log('gate: run with --reap to remove the stale lock(s) and comment.');
  if (plan.decision === 'clear') log('gate: CLEAR — no wip locks.');
}

function cmdMint(args, { log }) {
  // Coin the cycle run-id (single source of truth), printed on its own
  // `run-id:` line so the dispatcher can capture it and hand it to workers.
  log(`run-id: ${mintRunId()}`);
}

/** The machine maintenance-lock directory (inside .git: never tracked or swept). */
function maintLockDir(root) {
  return path.join(root, '.git', 'sdlc-maint.lock');
}

/** Best-effort owner + age of a held maintenance lock. */
function readMaintOwner(dir) {
  const ageFromMtime = () => {
    try {
      return Date.now() - fs.statSync(dir).mtimeMs;
    } catch {
      return 0; // dir vanished mid-read — treat as freshly held; next cycle acquires
    }
  };
  try {
    const txt = fs.readFileSync(path.join(dir, 'owner.txt'), 'utf8').trim();
    const [runId, ...stamp] = txt.split(/\s+/);
    const ts = new Date(stamp.join(' ')).getTime();
    if (runId && !Number.isNaN(ts)) return { runId, ageMs: Date.now() - ts };
    return { runId: runId || 'unknown', ageMs: ageFromMtime() };
  } catch {
    return { runId: 'unknown', ageMs: ageFromMtime() };
  }
}

function cmdMaintLock(args, { log }, root) {
  const runId = args[0];
  if (!runId) throw new SdlcError('maint-lock requires a run-id: sdlc maint-lock <run-id>');
  const dir = maintLockDir(root);
  const acquire = () => {
    fs.mkdirSync(dir); // atomic, fail-if-exists: exactly one contender succeeds
    fs.writeFileSync(path.join(dir, 'owner.txt'), `${runId} ${new Date().toISOString()}\n`);
  };
  try {
    acquire();
    log(`maint-lock: ACQUIRED ${runId}`);
    return { status: 'acquired' };
  } catch (err) {
    if (err?.code !== 'EEXIST') throw err;
  }
  const owner = readMaintOwner(dir);
  const plan = planMaintLock({ exists: true, ageMs: owner.ageMs });
  if (plan.action === 'skip') {
    log(`maint-lock: HELD by ${owner.runId} (${fmtAge(owner.ageMs)}) — skip maintenance this cycle; never abort over this lock.`);
    process.exitCode = 1;
    return { status: 'held', owner: owner.runId, age: fmtAge(owner.ageMs) };
  }
  // Stale (holder presumed dead) → reap by rename: atomic, exactly one contender wins.
  const stale = `${dir}.stale-${runId}`;
  try {
    fs.renameSync(dir, stale);
  } catch {
    log('maint-lock: HELD — another run just reaped or holds it; skip maintenance this cycle.');
    process.exitCode = 1;
    return { status: 'held', owner: owner.runId, age: fmtAge(owner.ageMs) };
  }
  fs.rmSync(stale, { recursive: true, force: true });
  try {
    acquire();
  } catch {
    log('maint-lock: HELD — lost the post-reap acquire race; skip maintenance this cycle.');
    process.exitCode = 1;
    return { status: 'held', owner: owner.runId, age: fmtAge(owner.ageMs) };
  }
  log(`maint-lock: REAPED stale lock from ${owner.runId} (${fmtAge(owner.ageMs)}); ACQUIRED ${runId}`);
  return { status: 'reaped', owner: owner.runId, age: fmtAge(owner.ageMs) };
}

function cmdMaintRelease(args, { log }, root) {
  const runId = args[0];
  const dir = maintLockDir(root);
  if (!fs.existsSync(dir)) {
    log('maint-release: RELEASED (already clear)'); // idempotent — a missing lock is a successful no-op
    return;
  }
  const owner = readMaintOwner(dir);
  if (runId && owner.runId !== 'unknown' && owner.runId !== runId) {
    log(`maint-release: NOT OWNER — held by ${owner.runId}; left in place.`);
    process.exitCode = 1;
    return;
  }
  fs.rmSync(dir, { recursive: true, force: true });
  log(`maint-release: RELEASED${runId ? ` ${runId}` : ''}`);
}

function cmdLanes(args, { gh, log }) {
  const snapshot = snapshotOpenIssues(gh);
  const { lanes, integrity } = computeLanes(snapshot);
  for (const lane of WORKER_LANES.concat('queued')) {
    const l = lanes[lane];
    const elig = l.eligible.length ? l.eligible.map((n) => `#${n}`).join(', ') : '—';
    const breakdown = laneIneligibilityBreakdown(l);
    log(`${lane}: depth ${l.depth}, eligible ${elig}${breakdown ? ` ${breakdown}` : ''}`);
  }
  if (integrity.length) {
    log('integrity (corrupt stage-label state — needs human):');
    for (const v of integrity) {
      const desc = v.stages.length === 0 ? 'no stage label (but wip/needs-human)' : `stages: ${v.stages.join(', ')}`;
      log(`  #${v.number}: ${desc}`);
    }
  } else {
    log('integrity: clean');
  }
}

function cmdHeal(args, { gh, log }) {
  // Three forms:
  //   heal <lane> <issue>  explicit issue, lane advisory (self-heal after a known worker)
  //   heal <issue>         explicit issue, no lane
  //   heal <lane>          NO issue → auto-discover every stalled wip issue in the lane
  const isIssueArg = (a) => /^#?\d+$/.test(String(a ?? ''));
  let lane = null;
  let issueArg = null;
  if (args.length >= 2) {
    lane = args[0];
    issueArg = args[1];
  } else if (args.length === 1) {
    if (isIssueArg(args[0])) issueArg = args[0];
    else lane = args[0];
  }

  if (issueArg) {
    // Single-issue check (explicit issue given).
    const issue = requireIssue(issueArg);
    const labels = fetchLabelNames(gh, issue);
    const { stillLocked } = planHeal(labels);
    const laneNote = lane ? ` (${lane})` : '';
    if (stillLocked) {
      log(`heal: #${issue}${laneNote} STALLED — still carries ${WIP_LABEL}; worker did not emit an outcome.`);
    } else {
      log(`heal: #${issue}${laneNote} OK — lock cleared; ${stagesOf(labels)[0] ? `now stage:${stagesOf(labels)[0]}` : 'no stage label'}.`);
    }
    return;
  }

  // Lane auto-discovery: no issue argument — find every stalled wip issue myself.
  if (!lane) {
    throw new SdlcError('heal requires a lane and/or issue: sdlc heal [<lane>] [<issue>]');
  }
  if (!WORKER_LANES.includes(lane)) {
    throw new SdlcError(`unknown lane "${lane}" (valid: ${WORKER_LANES.join(', ')})`);
  }
  const stalled = planHealLane(snapshotOpenIssues(gh, 'number,labels'), lane);
  if (stalled.length === 0) {
    log(`heal: ${lane} OK — no stalled ${WIP_LABEL} issues in stage:${lane}.`);
    return;
  }
  for (const number of stalled) {
    log(`heal: #${number} (${lane}) STALLED — still carries ${WIP_LABEL}; worker did not emit an outcome.`);
  }
}

function cmdGitMaint(args, { git, gh, log }) {
  const current = git(['rev-parse', '--abbrev-ref', 'HEAD']).trim();

  git(['fetch', 'origin', '--prune']);

  // Update the default branch without touching the working tree.
  const devBefore = (() => {
    try {
      return git(['rev-parse', DEFAULT_BRANCH]).trim();
    } catch {
      return null;
    }
  })();
  try {
    if (current === DEFAULT_BRANCH) git(['pull', '--ff-only', 'origin', DEFAULT_BRANCH]);
    else git(['fetch', 'origin', `${DEFAULT_BRANCH}:${DEFAULT_BRANCH}`]);
    const devAfter = git(['rev-parse', DEFAULT_BRANCH]).trim();
    log(
      devBefore === devAfter
        ? `${DEFAULT_BRANCH} update: already current`
        : `${DEFAULT_BRANCH} update: ${devBefore?.slice(0, 8)}..${devAfter.slice(0, 8)}`,
    );
  } catch (err) {
    log(`${DEFAULT_BRANCH} update: skipped (${String(err.message).split('\n')[0]})`);
  }

  // Ancestry-merged prune.
  const merged = git(['branch', '--merged', DEFAULT_BRANCH]).split(/\r?\n/);
  const candidates = planBranchPrune(merged, current);
  const pruned = [];
  for (const b of candidates) {
    try {
      git(['merge-base', '--is-ancestor', b, DEFAULT_BRANCH]); // throws (nonzero) if not ancestor
      git(['branch', '-D', b]);
      pruned.push(b);
    } catch {
      // not a true ancestor — leave it
    }
  }
  log(pruned.length ? `pruned (ancestry-merged): ${pruned.join(', ')}` : 'pruned (ancestry-merged): none');

  // Squash-merged prune (3-condition safety).
  const gone = parseGoneBranches(git(['branch', '-vv']), current);
  const squashPruned = [];
  const leftGone = [];
  for (const b of gone) {
    try {
      const pr = JSON.parse(gh(['pr', 'view', b, '--json', 'state,headRefOid']));
      const localTip = git(['rev-parse', b]).trim();
      if (canDeleteSquashMerged({ upstreamGone: true, prState: pr.state, localTip, headRefOid: pr.headRefOid })) {
        git(['branch', '-D', b]);
        squashPruned.push(b);
      } else {
        leftGone.push(`${b} (pr ${pr.state}, tip ${localTip === pr.headRefOid ? '=' : '≠'} head)`);
      }
    } catch {
      leftGone.push(`${b} (no merged PR / ambiguous)`);
    }
  }
  if (squashPruned.length) log(`pruned (squash-merged): ${squashPruned.join(', ')}`);
  if (leftGone.length) log(`left (gone upstream, unconfirmed): ${leftGone.join(', ')}`);

  // Open-PR state (read-only, digest only).
  try {
    const prs = JSON.parse(
      gh(['pr', 'list', '--state', 'open', '--json', 'number,title,headRefName,mergeable,reviewDecision,isDraft']),
    );
    if (prs.length) {
      log('open PRs:');
      for (const p of prs) {
        log(`  #${p.number} ${p.headRefName} [${p.mergeable ?? '?'}/${p.reviewDecision || 'no-review'}${p.isDraft ? '/draft' : ''}] ${p.title}`);
      }
    } else {
      log('open PRs: none');
    }
  } catch (err) {
    log(`open PRs: unavailable (${String(err.message).split('\n')[0]})`);
  }
}

function cmdWorktreeSweep(args, { git, gh, log, statSize = (p) => fs.statSync(p).size }, root) {
  const apply = args.includes('--apply');
  const repoName = path.basename(root);

  let trees;
  try {
    trees = parseWorktrees(git(['worktree', 'list', '--porcelain']), repoName);
  } catch (err) {
    log(`worktree-sweep: could not list worktrees (${String(err.message).split('\n')[0]})`);
    return;
  }
  if (!trees.length) {
    log('worktree-sweep: no issue-scoped worktrees.');
    return;
  }

  // Fetch/prune first so origin refs and ancestry are current before we judge.
  try {
    git(['fetch', 'origin', '--prune']);
  } catch (err) {
    log(`worktree-sweep: fetch failed (${String(err.message).split('\n')[0]}) — judging on stale refs.`);
  }

  const enriched = trees.map((t) => {
    // Clean = no uncommitted changes in that tree. A tree whose only content is
    // 0-byte untracked strays (mangled `> OUTCOME` redirects, #688) counts as
    // "clean modulo strays": clean:true plus the stray paths to clear on --apply.
    // Defensive: an unreadable tree is treated as dirty so it is never removed.
    let clean = false;
    let strays = [];
    try {
      const porcelain = git(['-C', t.path, 'status', '--porcelain']);
      const classified = classifyStatusForSweep(porcelain, (rel) =>
        statSize(path.join(t.path, rel)),
      );
      clean = classified.clean;
      strays = classified.strays;
    } catch {
      clean = false;
      strays = [];
    }

    // branchGone/merged: no branch ref, OR the branch is ancestry-merged into
    // origin/<DEFAULT_BRANCH>, OR its PR is MERGED. All are positive "landed"
    // signals — none fires on a fresh never-pushed branch, so in-progress work
    // is safe.
    let branchGone = false;
    if (!t.branch) {
      branchGone = true; // detached worktree with no branch
    } else {
      try {
        git(['show-ref', '--verify', '--quiet', `refs/heads/${t.branch}`]);
        // branch exists locally — check merged / PR state
        let merged = false;
        try {
          git(['merge-base', '--is-ancestor', t.branch, `origin/${DEFAULT_BRANCH}`]);
          merged = true;
        } catch {
          merged = false;
        }
        if (!merged) {
          try {
            const prs = JSON.parse(gh(['pr', 'list', '--head', t.branch, '--state', 'merged', '--json', 'number']));
            merged = Array.isArray(prs) && prs.length > 0;
          } catch {
            merged = false;
          }
        }
        branchGone = merged;
      } catch {
        branchGone = true; // branch ref no longer exists
      }
    }

    // issueClosed: the owning issue is CLOSED. Ambiguous/unreadable → false.
    let issueClosed = false;
    try {
      const state = JSON.parse(gh(['issue', 'view', String(t.issue), '--json', 'state'])).state;
      issueClosed = state === 'CLOSED';
    } catch {
      issueClosed = false;
    }

    return { ...t, clean, strays, branchGone, issueClosed };
  });

  const plan = planWorktreeSweep(enriched);

  for (const t of plan.leave) {
    log(`worktree-sweep: LEAVE ${t.path} (#${t.issue}) — ${t.reason}`);
  }
  for (const t of plan.remove) {
    const strayCount = t.strays?.length ?? 0;
    const strayNote = strayCount
      ? ` (${apply ? 'cleared' : 'after clearing'} ${strayCount} 0-byte stray)`
      : '';
    log(`worktree-sweep: REMOVE ${t.path} (#${t.issue}) — ${t.reason}${strayNote}`);
    if (apply) {
      // A 0-byte stray blocks `git worktree remove` as an untracked change; clear
      // the provably-empty files first (classify already vetted size === 0).
      for (const rel of t.strays ?? []) {
        const full = path.join(t.path, rel);
        try {
          fs.rmSync(full);
          log(`  cleared 0-byte stray: ${full}`);
        } catch (err) {
          log(`  could not clear stray ${full}: ${String(err.message).split('\n')[0]}`);
        }
      }
      // #723: unlink any root-level junction (e.g. node_modules → main checkout)
      // BEFORE git touches the tree — git worktree remove follows junctions on
      // Windows and would delete the shared target's contents through it.
      unlinkWorktreeRootLinks(t.path, log);
      try {
        git(['worktree', 'remove', t.path]);
        log(`  removed: ${t.path}`);
      } catch (err) {
        // Report (don't fail) on permission errors etc. — stale admin dirs can
        // survive a kill and refuse deletion; the prune below still runs.
        log(`  could not remove ${t.path}: ${String(err.message).split('\n')[0]}`);
      }
    }
  }

  if (plan.remove.length && !apply) {
    log('worktree-sweep: run with --apply to remove the clean stale worktree(s).');
  }
  if (apply) {
    try {
      git(['worktree', 'prune']);
      log('worktree-sweep: pruned worktree admin metadata.');
    } catch (err) {
      log(`worktree-sweep: prune reported (${String(err.message).split('\n')[0]}) — continuing.`);
    }
  }
  if (!plan.remove.length) log('worktree-sweep: nothing to remove.');
}

function cmdConflictScan(args, { gh, git, log }) {
  const apply = args.includes('--apply');

  // The default branch's tip commit time is the idempotency watermark: a
  // conflict comment older than the last base-branch change is stale, so a
  // re-nudge is warranted; a newer one means we already flagged this since the
  // base branch last moved.
  let baseUpdatedAt = null;
  try {
    baseUpdatedAt = git(['log', '-1', '--format=%cI', `origin/${DEFAULT_BRANCH}`]).trim() || null;
  } catch {
    baseUpdatedAt = null;
  }

  const prs = JSON.parse(
    gh([
      'pr', 'list', '--state', 'open', '--base', DEFAULT_BRANCH,
      '--json', 'number,headRefName,baseRefName,mergeable,closingIssuesReferences',
      '--limit', '200',
    ]),
  ).map((p) => ({
    number: p.number,
    headRefName: p.headRefName,
    baseRefName: p.baseRefName,
    mergeable: p.mergeable,
    linkedIssues: (p.closingIssuesReferences ?? []).map((r) => r.number),
  }));

  // Fetch labels/comments/state for every issue linked by a conflicting PR.
  const issueInfo = {};
  const wanted = new Set();
  for (const pr of prs) {
    if (pr.mergeable === 'CONFLICTING') for (const n of pr.linkedIssues) wanted.add(n);
  }
  for (const n of wanted) {
    try {
      const v = JSON.parse(gh(['issue', 'view', String(n), '--json', 'state,labels,comments']));
      issueInfo[n] = {
        state: v.state,
        labels: (v.labels ?? []).map((l) => l.name),
        comments: (v.comments ?? []).map((c) => ({ body: c.body, createdAt: c.createdAt })),
      };
    } catch {
      // leave undefined → planned as "linked issue closed or not found"
    }
  }

  const { actions, skipped } = planConflictScan(prs, issueInfo, baseUpdatedAt);

  if (!actions.length && !skipped.length) {
    log(`conflict-scan: no conflicting PRs into ${DEFAULT_BRANCH}.`);
    return;
  }

  for (const a of actions) {
    const adv = a.advanceTo ? ` + advance #${a.issue} stage:${a.stage} → stage:${a.advanceTo}` : '';
    log(`conflict-scan: PR #${a.pr} (${a.branch}) → comment #${a.issue}${adv}`);
    if (apply) {
      gh(['issue', 'comment', String(a.issue), '--body', a.comment]);
      if (a.advanceTo) {
        const plan = planAdvance(issueInfo[a.issue].labels, a.advanceTo);
        const editArgs = ['issue', 'edit', String(a.issue)];
        for (const l of plan.removeLabels) editArgs.push('--remove-label', l);
        for (const l of plan.addLabels) editArgs.push('--add-label', l);
        gh(editArgs);
      }
    }
  }
  for (const s of skipped) {
    log(`conflict-scan: skipped ${s.issue ? `#${s.issue} ` : ''}(PR #${s.pr}) — ${s.reason}`);
  }
  if (!apply && actions.length) {
    log('conflict-scan: dry run — re-run with --apply to post comments + advance.');
  }
}

function cmdDigest(args, { gh, log }, root) {
  const idx = args.indexOf('--state');
  const statePath = idx >= 0 && args[idx + 1] ? args[idx + 1] : path.join(root, '.sdlc-cache', 'last-digest.json');

  let prev = null;
  try {
    if (fs.existsSync(statePath)) {
      const saved = JSON.parse(fs.readFileSync(statePath, 'utf8'));
      prev = { current: saved.current ?? null, parked: saved.parked ?? null, hold: saved.hold ?? null };
    }
  } catch {
    prev = null;
  }

  const snapshot = snapshotOpenIssues(gh, 'number,labels');
  const d = computeDigest(snapshot, prev);

  const fmtList = (nums) => (nums.length ? nums.map((n) => `#${n}`).join(', ') : 'none');
  // Diff-style: the parked/hold lists barely change cycle-to-cycle, so report
  // "no change" when they're identical and only annotate the +/− delta otherwise,
  // instead of re-listing every number every cycle.
  const fmtParkLine = (label, list, delta) => {
    if (delta && !delta.changed) return `${label}: no change (${list.length})`;
    let line = `${label}: ${fmtList(list)}`;
    if (delta && delta.changed) {
      const parts = [];
      if (delta.added.length) parts.push(`+${delta.added.map((n) => `#${n}`).join(', ')}`);
      if (delta.removed.length) parts.push(`-${delta.removed.map((n) => `#${n}`).join(', ')}`);
      if (parts.length) line += ` (${parts.join(' ')})`;
    }
    return line;
  };

  log(
    `depths: ${WORKER_LANES.concat('queued')
      .map((l) => `${l} ${d.depths[l]}`)
      .join(' / ')}`,
  );
  log(fmtParkLine('parked (needs-human)', d.parked, d.parkedDelta));
  log(fmtParkLine('hold', d.hold, d.holdDelta));
  if (d.arrivals !== null) {
    log(`arrivals vs last cycle: ${d.arrivals.length ? d.arrivals.map((n) => `#${n}`).join(', ') : 'none'}`);
    log(`departures vs last cycle: ${d.departures.length ? d.departures.map((n) => `#${n}`).join(', ') : 'none'}`);
  } else {
    log('arrivals vs last cycle: (no prior snapshot)');
  }

  try {
    fs.mkdirSync(path.dirname(statePath), { recursive: true });
    fs.writeFileSync(
      statePath,
      JSON.stringify({ at: new Date().toISOString(), current: d.current, parked: d.parked, hold: d.hold }, null, 2),
    );
  } catch (err) {
    log(`digest: could not persist snapshot (${String(err.message).split('\n')[0]})`);
  }
}

function cmdSweep(args, { gh, log }, root) {
  const stateIdx = args.indexOf('--state');
  const windowIdx = args.indexOf('--window');
  const ack = args.includes('--ack');
  const statePath =
    stateIdx >= 0 && args[stateIdx + 1]
      ? args[stateIdx + 1]
      : path.join(root, '.sdlc-cache', 'last-sweep.json');
  const windowHours = windowIdx >= 0 && args[windowIdx + 1] ? Number(args[windowIdx + 1]) : 24;

  let sweptPrs = [];
  try {
    if (fs.existsSync(statePath)) {
      sweptPrs = JSON.parse(fs.readFileSync(statePath, 'utf8')).sweptPrs ?? [];
    }
  } catch {
    sweptPrs = [];
  }

  const prs = JSON.parse(
    gh([
      'pr', 'list', '--state', 'merged', '--base', DEFAULT_BRANCH,
      '--json', 'number,mergedAt,title,closingIssuesReferences', '--limit', '50',
    ]),
  );
  const mergedPrs = prs.map((p) => ({
    number: p.number,
    mergedAt: p.mergedAt,
    title: p.title,
    closes: (p.closingIssuesReferences ?? []).map((r) => r.number),
  }));
  const sinceMs = Number.isFinite(windowHours) ? Date.now() - windowHours * 3600000 : null;

  // --ack: the only mode that writes the marker. Run it AFTER processing the
  // work-list so a worker that dies mid-processing re-lists the same merges on
  // the next pass (at-least-once; the cascade-unblock itself is idempotent).
  if (ack) {
    const { nextSwept, newlyAcked } = computeSweepAck(mergedPrs, { sinceMs, sweptPrs });
    try {
      fs.mkdirSync(path.dirname(statePath), { recursive: true });
      fs.writeFileSync(
        statePath,
        JSON.stringify({ at: new Date().toISOString(), sweptPrs: nextSwept }, null, 2),
      );
      log(
        newlyAcked.length
          ? `sweep: acked ${newlyAcked.length} merge${newlyAcked.length === 1 ? '' : 's'} (${newlyAcked.map((n) => `#${n}`).join(', ')})`
          : 'sweep: ack — nothing new',
      );
    } catch (err) {
      log(`sweep: could not persist marker (${String(err.message).split('\n')[0]})`);
    }
    return;
  }

  const openIssues = JSON.parse(
    gh(['issue', 'list', '--state', 'open', '--json', 'number,title,labels,body', '--limit', '200']),
  );
  const { items, empty } = computeSweep(mergedPrs, openIssues, { sinceMs, sweptPrs });

  if (empty) {
    log('sweep: clear');
    return;
  }
  for (const it of items) {
    log(`PR #${it.number} (merged ${it.mergedAt ?? '?'}) ${it.title}`);
    for (const c of it.closes) {
      log(`  closed #${c.number}`);
      if (c.dependents.length) {
        for (const d of c.dependents) {
          log(`    dependent #${d.number}${d.blocked ? ' [blocked → unblock]' : ''} ${d.title}`);
        }
      } else {
        log('    dependents: none');
      }
    }
  }
  log('sweep: run `sdlc sweep --ack` after processing to mark these swept');
}

/**
 * `sdlc cycle-prep [--apply]`: the whole fixed, zero-judgment pre-dispatch
 * sequence in one shot — mint → maint-lock → lanes → gate --reap → sweep →
 * (git-maint → worktree-sweep → conflict-scan) → maint-release — emitting one
 * delimited, machine-readable report so the dispatcher runs `cycle-prep`,
 * reads it, spawns workers, then `digest`.
 *
 * Semantics are identical to the individual subcommands (they are literally
 * invoked here): the run-id is minted internally and printed verbatim on its own
 * `run-id:` line; the machine maintenance lock guards Step 0a exactly as before —
 * the git/worktree/conflict maintenance trio runs ONLY while this run holds the
 * lock, and the lock is released at the end of that section regardless. A lock
 * held by another live run is a normal outcome (maintenance skipped, cycle
 * continues), NOT a cycle-prep failure, so its exit-1 signal is not propagated.
 * gate always reaps (verify-before-write, as dispatch Step 0 does). `--apply`
 * propagates to worktree-sweep/conflict-scan (default: preview).
 */
function cmdCyclePrep(args, deps, root) {
  const { log } = deps;
  const apply = args.includes('--apply');
  const runId = mintRunId();
  const startedAt = new Date().toISOString();

  // run-id first, on its own line, byte-identical to `sdlc mint` — the dispatcher
  // captures this as the cycle's single source of truth. `started:` stamps the
  // cycle start so digest can compute wall-clock duration.
  log(`run-id: ${runId}`);
  log(`started: ${startedAt}`);

  const section = (name) => log(`\n=== ${name} ===`);

  // Machine maintenance lock (dispatch Step -1). A held-by-other lock sets
  // exitCode 1 for the standalone command; here it is expected, so restore the
  // prior exit code — a skipped maintenance section is not a cycle-prep failure.
  section('maint-lock');
  const savedExit = process.exitCode;
  const lock = cmdMaintLock([runId], deps, root);
  const weHoldLock = lock.status === 'acquired' || lock.status === 'reaped';
  if (!weHoldLock) process.exitCode = savedExit;

  try {
    // Snapshot + per-issue wip gate + merge-sweep peek (dispatch Step 0) — run
    // every cycle whether or not we hold the maintenance lock.
    section('lanes');
    cmdLanes([], deps, root);

    section('gate');
    cmdGate(['--reap'], deps, root);

    section('sweep');
    cmdSweep([], deps, root);

    if (weHoldLock) {
      // Git + worktree maintenance (dispatch Step 0a) — only while holding the lock.
      section('git-maint');
      cmdGitMaint([], deps, root);

      section('worktree-sweep');
      cmdWorktreeSweep(apply ? ['--apply'] : [], deps, root);

      section('conflict-scan');
      cmdConflictScan(apply ? ['--apply'] : [], deps, root);
    } else {
      section('maintenance');
      log(`maintenance: skipped (lock held by ${lock.owner ?? 'another run'}${lock.age ? `, ${lock.age}` : ''}) — Step 0a not run this cycle.`);
    }
  } finally {
    // Release at the end of the maintenance section (success or not), never at
    // cycle end — but only if this run actually acquired/reaped the lock.
    if (weHoldLock) {
      section('maint-release');
      cmdMaintRelease([runId], deps, root);
    }
  }

  section('summary');
  const maintNote = weHoldLock ? (apply ? 'applied' : 'previewed') : 'skipped';
  log(`cycle-prep: run-id ${runId}, started ${startedAt}, maintenance ${maintNote}.`);
}

const USAGE = `sdlc — deterministic SDLC pipeline one-shots (reference implementation)

  Worker-side:
  sdlc claim    <issue> [<run-id> <lane>] [--verify]  add ${WIP_LABEL} + claim comment; --verify exits 1 on a lost race
  sdlc claim    --next <lane> <run-id>                CLI picks the next eligible issue for the lane + claims it; exits 1 with idle if none
  sdlc emit     <issue> <run-id> <ADVANCE|BOUNCE|PARK|CONTINUE|CLOSE> [--to <stage>] [--body <text>|--body-file <file>]
                                    post the sdlc:emit marker comment + do the outcome's label math
  sdlc advance  <issue> <to-stage>  validate + swap stage label, drop ${WIP_LABEL} (dispatcher-side swaps; workers use emit)
  sdlc context  <issue>             branch + status + issue labels/state + PRs
  sdlc worktree <issue> [<branch>]  add a sibling git worktree for the branch
  sdlc comment  <issue> <file>      post a body-file comment (plumbing)
  sdlc dup-check "<keywords>" [--limit N] [--exclude <issue>]  rank open-issue dup candidates; exit 2 if any found, 0 if clean

  Dispatcher-side:
  sdlc gate     [--reap]            per-issue wip-lock ages (timeline-based) → LIVE/REAP/CLEAR; --reap re-verifies before writing
  sdlc mint                         coin + print this cycle's run-id
  sdlc maint-lock    <run-id>       acquire the per-machine maintenance lock (exit 1 = held: skip Step 0a, never abort)
  sdlc maint-release <run-id>       release the maintenance lock (idempotent)
  sdlc lanes                        per-lane depth + eligibility + stage-label integrity
  sdlc heal     [<lane>] [<issue>]  post-worker self-heal check (still locked?); lane-only auto-discovers stalled wip issues
  sdlc git-maint                    fetch, ff ${DEFAULT_BRANCH}, prune merged branches, PR state
  sdlc worktree-sweep [--apply]     remove clean issue-scoped worktrees whose branch is gone/merged or issue closed
  sdlc conflict-scan [--apply]      nudge + bounce-to-build issues whose open PR conflicts with ${DEFAULT_BRANCH}
  sdlc sweep    [--state <file>] [--window <hours>]  merged PRs → closed issues → blocked dependents (read-only, writes nothing)
  sdlc sweep --ack [--state <file>] [--window <hours>]  mark the window's merges swept (run AFTER processing the work-list)
  sdlc cycle-prep [--apply]         the whole pre-dispatch sequence in one shot (mint→maint-lock→lanes→gate --reap→sweep→git-maint→worktree-sweep→conflict-scan→maint-release), one delimited report
  sdlc digest   [--state <file>]    depths, parked/hold, arrivals-diff vs last cycle

Stages: ${STAGES.join(' → ')}`;

const COMMANDS = {
  claim: cmdClaim,
  emit: cmdEmit,
  advance: cmdAdvance,
  context: cmdContext,
  worktree: cmdWorktree,
  comment: cmdComment,
  'dup-check': cmdDupCheck,
  gate: cmdGate,
  mint: cmdMint,
  'maint-lock': cmdMaintLock,
  'maint-release': cmdMaintRelease,
  lanes: cmdLanes,
  heal: cmdHeal,
  'git-maint': cmdGitMaint,
  'worktree-sweep': cmdWorktreeSweep,
  'conflict-scan': cmdConflictScan,
  sweep: cmdSweep,
  'cycle-prep': cmdCyclePrep,
  digest: cmdDigest,
};

/**
 * Run one sdlc command. `deps` is injectable so tests can supply fake gh/git
 * executors and capture the exact CLI invocations. Returns nothing; throws
 * SdlcError on user error.
 */
export function runSdlc(argv, deps = {}) {
  const {
    gh = defaultGh,
    git = defaultGit,
    log = (msg) => console.log(msg),
    root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..'),
    statSize,
  } = deps;
  const [command, ...rest] = argv;
  if (!command || command === '--help' || command === '-h') {
    log(USAGE);
    return;
  }
  const handler = COMMANDS[command];
  if (!handler) {
    throw new SdlcError(`unknown command "${command}"\n\n${USAGE}`);
  }
  handler(rest, { gh, git, log, statSize }, root);
}

// Only run when invoked directly, so tests can import the pure helpers.
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    runSdlc(process.argv.slice(2));
  } catch (err) {
    if (err instanceof SdlcError) {
      console.error(`sdlc: ${err.message}`);
      process.exit(1);
    }
    throw err;
  }
}
