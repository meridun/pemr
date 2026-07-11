#!/usr/bin/env node
/**
 * `sdlc` — deterministic one-shots for the Agentic SDLC pipeline's issue
 * state-machine ritual (issue #609). The dbmate analog for our pipeline: the
 * stage graph *is* a schema, so transitions are validated against it and the
 * hand-typed `stage:verify`/`stage:audit` typo class is killed by construction.
 *
 * Only deterministic label/branch math lives here. Report and comment *bodies*
 * stay judgment (the agent writes them); `sdlc comment` is a thin plumbing
 * wrapper that posts a body the agent already authored to a file.
 *
 * Commands (worker-side — issue #609):
 *   sdlc claim   <issue> [<run-id> <lane>]  add sdlc:wip (+ claim comment), print branch + status
 *   sdlc advance <issue> <to-stage>   validate transition, swap stage label, drop sdlc:wip
 *   sdlc context <issue>              branch + status + issue labels/state + open PRs for branch
 *   sdlc worktree <issue> [<branch>]  add a sibling git worktree for the issue's branch
 *   sdlc comment <issue> <file>       post a body-file comment (plumbing only)
 *
 * Commands (dispatcher-side — issue #615; each a pure function of gh/git state):
 *   sdlc gate     [--reap]            per-issue wip-lock ages from timeline events → LIVE/REAP/CLEAR (#613)
 *   sdlc lock     <run-id>            take the dispatcher singleton lock (pinned dispatch-lock issue)
 *   sdlc unlock   <run-id>            release the dispatcher singleton lock
 *   sdlc lanes                        per-lane depth + eligibility + the ≠1 stage-label check (#614)
 *   sdlc heal     [<lane>] <issue>    post-worker self-heal: did the worker clear its lock?
 *   sdlc git-maint                    fetch, ff dev, prune ancestry/squash-merged branches, PR state
 *   sdlc digest   [--state <file>]    queue depths, parked/hold lists, arrivals-diff vs last cycle
 *
 * The stage graph (forward pipeline edges + the documented bounces):
 *   intake → design → queued → build → verify → audit → ship
 * with `sdlc:wip` as the machine lock and `sdlc:needs-human` / `sdlc:hold` as
 * parks (see prompts/sdlc/README.md).
 *
 * Usage:
 *   node scripts/sdlc.mjs <command> [args...]
 */

import { execFileSync } from 'child_process';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

export const WIP_LABEL = 'sdlc:wip';

/** Ordered pipeline stages. */
export const STAGES = ['intake', 'design', 'queued', 'build', 'verify', 'audit', 'ship'];

/**
 * Legal stage transitions: the forward pipeline edge(s) plus the documented
 * bounces from prompts/sdlc/. `ship` is terminal (the ship worker opens a PR;
 * the merge closes the issue). Any edge not listed here is rejected — that is
 * what fixes the label-typo class of bug.
 */
export const STAGE_GRAPH = {
  intake: ['design', 'queued'],
  design: ['queued', 'intake'],
  queued: ['build'],
  build: ['verify', 'queued', 'design', 'intake'],
  verify: ['audit', 'build'],
  audit: ['ship', 'build'],
  ship: [],
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

// --- Pure dispatcher-side helpers (issue #615) ---------------------------------

/** Lanes that have a worker (queued is the workerless human throttle). */
export const WORKER_LANES = ['intake', 'design', 'build', 'verify', 'audit', 'ship'];

/** Labels that make an item ineligible for a worker to claim. */
export const PARK_LABELS = ['sdlc:needs-human', 'sdlc:hold'];

/** The wip-lock reap threshold: two full hourly cycles. */
export const WIP_STALE_MS = 2 * 60 * 60 * 1000;

/** Priority ordering for CLAIM: lower rank sorts first. Unlabeled sorts last. */
export const PRIORITY_RANK = {
  'priority:critical': 0,
  'priority:medium': 1,
  'priority:future': 2,
};
const NO_PRIORITY_RANK = 3;

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
 * The per-issue wip-lock gate decision (issue #613/#615; per-issue concurrency).
 * `wipItems` is `[{ number, ageMs }]` — age measured from the `sdlc:wip`
 * labeled event, NOT the issue's updatedAt (which any later comment/edit
 * falsely refreshes). Pure.
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
 * Dispatcher singleton-lock decision from the dispatch-lock issue's comments
 * (`[{ body, createdAt }]`, chronological). The most recent `lock <run-id> <ts>`
 * comment holds the mutex unless a matching `unlock <run-id>` follows it or it
 * is older than the threshold (dead dispatcher). Pure.
 */
export function planLock(comments, nowMs, thresholdMs = WIP_STALE_MS) {
  let holder = null;
  for (const c of comments ?? []) {
    const lock = c.body?.match(/^lock\s+(\S+)/);
    const unlock = c.body?.match(/^unlock\s+(\S+)/);
    if (lock) holder = { runId: lock[1], createdAt: c.createdAt };
    else if (unlock && holder && unlock[1] === holder.runId) holder = null;
  }
  if (!holder) return { held: false, holder: null, stale: false };
  const ageMs = nowMs - new Date(holder.createdAt).getTime();
  const stale = ageMs >= thresholdMs;
  return { held: !stale, holder: holder.runId, stale, ageMs };
}

/**
 * Claim-verify race decision from an issue's comments (`[{ body, createdAt }]`,
 * chronological). Considers `sdlc:claim <run-id> <lane>` comments newer than the
 * last outcome EMIT (a comment starting with ADVANCE/BOUNCE/PARK/CONTINUE or a
 * reap). Winner = earliest claim; ties break to the lexicographically lower
 * run-id. Pure.
 */
export function planClaimVerify(comments, myRunId) {
  const OUTCOME = /^(ADVANCE|BOUNCE|PARK|CONTINUE|sdlc-dispatch: reaped)/;
  let claims = [];
  for (const c of comments ?? []) {
    if (OUTCOME.test(c.body ?? '')) {
      claims = []; // claims before an outcome are settled history
      continue;
    }
    const m = c.body?.match(/^sdlc:claim\s+(\S+)/);
    if (m) claims.push({ runId: m[1], createdAt: c.createdAt });
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
 * Per-lane eligibility + depth from ONE open-issue snapshot, plus the ≠1
 * `stage:*` integrity check (issue #614). `issues` is
 * `[{ number, labels:[{name}]|[name], createdAt }]`. Pure.
 *
 * Eligible = has that stage, not wip and not parked/hold; ordered exactly as a
 * worker's CLAIM would pick — priority (critical › medium › future › none),
 * then FIFO by createdAt. `integrity` lists every issue whose stage-label count
 * is not exactly 1 (0 = invisible to every lane, >1 = eligible in two lanes).
 */
export function computeLanes(issues) {
  const norm = (issues ?? []).map((i) => ({
    number: i.number,
    createdAt: i.createdAt ?? '',
    labels: (i.labels ?? []).map((l) => (typeof l === 'string' ? l : l.name)),
  }));

  const lanes = {};
  for (const lane of WORKER_LANES.concat('queued')) {
    lanes[lane] = { depth: 0, eligible: [] };
  }

  const integrity = [];
  for (const issue of norm) {
    const stages = stagesOf(issue.labels);
    // Multiple stage labels are always corrupt. Zero is only corrupt when the
    // issue still carries a machine flag (wip/needs-human) — a stage-less open
    // issue is otherwise a legitimate state: post-ship awaiting PR merge, the
    // dispatch-lock issue, or simply not (yet) in the pipeline.
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

/** Post-worker self-heal check: did the worker clear its lock? Pure. */
export function planHeal(labelNames) {
  return { stillLocked: (labelNames ?? []).includes(WIP_LABEL) };
}

/**
 * Which local branches are safe to delete as ancestry-merged into `dev`.
 * `mergedBranches` is the raw `git branch --merged dev` line list. Never
 * returns dev, master, or the current branch. Pure — the caller still
 * re-confirms each with `git merge-base --is-ancestor` before deleting.
 */
export function planBranchPrune(mergedBranches, currentBranch) {
  const keep = new Set(['dev', 'master', currentBranch]);
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
  const keep = new Set(['dev', 'master', currentBranch]);
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
 * Digest numbers from a snapshot: per-lane depth, parked/hold lists, and — when
 * a previous snapshot's open-issue numbers are supplied — the arrivals/departures
 * diff vs last cycle. `issues` is `[{ number, labels }]`. Pure.
 */
export function computeDigest(issues, prevNumbers = null) {
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
    const prev = new Set(prevNumbers);
    const now = new Set(current);
    arrivals = current.filter((n) => !prev.has(n));
    departures = prevNumbers.filter((n) => !now.has(n));
  }

  return { depths, parked, hold, current, arrivals, departures };
}

// --- I/O boundary: gh / git executors (injectable for tests) -------------------

const defaultGh = (args) => execFileSync('gh', args, { encoding: 'utf8' });
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

// --- Commands ------------------------------------------------------------------

function cmdClaim(args, { gh, git, log }) {
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
  if (runId) {
    // The label is the visibility signal; this comment is the ownership record
    // and race tiebreaker (see prompts/sdlc/README.md CLAIM).
    gh(['issue', 'comment', issue, '--body', `sdlc:claim ${runId} ${lane}`]);
  }
  if (verify) {
    const result = planClaimVerify(fetchLockComments(gh, issue), runId);
    if (!result.won) {
      log(`claim: LOST race on #${issue} to ${result.winner ?? '(unknown)'} — leave the label, pick the next item.`);
      process.exitCode = 1;
      return;
    }
    log(`claim: verified — ${runId} owns #${issue}.`);
  }
  const branch = git(['rev-parse', '--abbrev-ref', 'HEAD']).trim();
  const status = git(['status', '--short']).trimEnd();
  log(runId ? `claimed #${issue} (+${WIP_LABEL}, claim ${runId} ${lane})` : `claimed #${issue} (+${WIP_LABEL})`);
  log(`branch: ${branch}`);
  log(status ? `status:\n${status}` : 'status: clean');
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
  const target = path.resolve(root, '..', `${repoName}-wt-${issue}`);
  git(['fetch', 'origin']);
  // Reuse an existing branch if present; otherwise create it off origin/dev.
  const existing = git(['branch', '--list', branch]).trim();
  if (existing) {
    git(['worktree', 'add', target, branch]);
  } else {
    git(['worktree', 'add', '-b', branch, target, 'origin/dev']);
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

function cmdGate(args, { gh, log }) {
  const reap = args.includes('--reap');
  const snapshot = snapshotOpenIssues(gh, 'number,labels,updatedAt');
  const wip = snapshot.filter((i) => (i.labels ?? []).some((l) => l.name === WIP_LABEL));
  const now = Date.now();
  const wipItems = wip.map((i) => {
    // Accurate age from the sdlc:wip labeled event, not updatedAt (issue #613).
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

/** Find the pinned dispatcher-mutex issue by its exact title. */
function findDispatchLockIssue(gh) {
  const list = JSON.parse(
    gh(['issue', 'list', '--state', 'open', '--search', 'sdlc:dispatch-lock in:title', '--json', 'number,title']),
  );
  const hit = list.find((i) => i.title === 'sdlc:dispatch-lock');
  if (!hit) throw new SdlcError('no open issue titled "sdlc:dispatch-lock" — create + pin it first');
  return hit.number;
}

function fetchLockComments(gh, issue) {
  return JSON.parse(
    gh(['issue', 'view', String(issue), '--json', 'comments', '--jq', '[.comments[] | {body: .body, createdAt: .createdAt}]']),
  );
}

function cmdLock(args, { gh, log }) {
  const runId = args[0];
  if (!runId) throw new SdlcError('lock requires a run-id: sdlc lock <run-id>');
  const issue = findDispatchLockIssue(gh);
  const plan = planLock(fetchLockComments(gh, issue), Date.now());
  if (plan.held) {
    log(`lock: HELD by ${plan.holder} (${fmtAge(plan.ageMs)}) — abort the cycle.`);
    process.exitCode = 1;
    return;
  }
  if (plan.stale) log(`lock: superseding stale lock from ${plan.holder} (${fmtAge(plan.ageMs)}, no unlock).`);
  gh(['issue', 'comment', String(issue), '--body', `lock ${runId} ${new Date().toISOString()}`]);
  log(`lock: ACQUIRED ${runId} (issue #${issue}).`);
}

function cmdUnlock(args, { gh, log }) {
  const runId = args[0];
  if (!runId) throw new SdlcError('unlock requires a run-id: sdlc unlock <run-id>');
  const issue = findDispatchLockIssue(gh);
  gh(['issue', 'comment', String(issue), '--body', `unlock ${runId}`]);
  log(`unlock: RELEASED ${runId} (issue #${issue}).`);
}

function cmdLanes(args, { gh, log }) {
  const snapshot = snapshotOpenIssues(gh);
  const { lanes, integrity } = computeLanes(snapshot);
  for (const lane of WORKER_LANES.concat('queued')) {
    const l = lanes[lane];
    const elig = l.eligible.length ? l.eligible.map((n) => `#${n}`).join(', ') : '—';
    log(`${lane}: depth ${l.depth}, eligible ${elig}`);
  }
  if (integrity.length) {
    log('integrity (≠1 stage label — needs human):');
    for (const v of integrity) {
      const desc = v.stages.length === 0 ? 'no stage label' : `stages: ${v.stages.join(', ')}`;
      log(`  #${v.number}: ${desc}`);
    }
  } else {
    log('integrity: clean (every open issue has exactly one stage label)');
  }
}

function cmdHeal(args, { gh, log }) {
  // Accept `heal <lane> <issue>` or `heal <issue>`; lane is advisory only.
  const issueArg = args.length >= 2 ? args[1] : args[0];
  const lane = args.length >= 2 ? args[0] : null;
  const issue = requireIssue(issueArg);
  const labels = fetchLabelNames(gh, issue);
  const { stillLocked } = planHeal(labels);
  const laneNote = lane ? ` (${lane})` : '';
  if (stillLocked) {
    log(`heal: #${issue}${laneNote} STALLED — still carries ${WIP_LABEL}; worker did not emit an outcome.`);
  } else {
    log(`heal: #${issue}${laneNote} OK — lock cleared; ${stagesOf(labels)[0] ? `now stage:${stagesOf(labels)[0]}` : 'no stage label'}.`);
  }
}

function cmdGitMaint(args, { git, gh, log }) {
  const current = git(['rev-parse', '--abbrev-ref', 'HEAD']).trim();

  git(['fetch', 'origin', '--prune']);

  // Update dev without touching the working tree.
  const devBefore = (() => {
    try {
      return git(['rev-parse', 'dev']).trim();
    } catch {
      return null;
    }
  })();
  try {
    if (current === 'dev') git(['pull', '--ff-only', 'origin', 'dev']);
    else git(['fetch', 'origin', 'dev:dev']);
    const devAfter = git(['rev-parse', 'dev']).trim();
    log(
      devBefore === devAfter
        ? 'dev update: already current'
        : `dev update: ${devBefore?.slice(0, 8)}..${devAfter.slice(0, 8)}`,
    );
  } catch (err) {
    log(`dev update: skipped (${String(err.message).split('\n')[0]})`);
  }

  // Ancestry-merged prune.
  const merged = git(['branch', '--merged', 'dev']).split(/\r?\n/);
  const candidates = planBranchPrune(merged, current);
  const pruned = [];
  for (const b of candidates) {
    try {
      git(['merge-base', '--is-ancestor', b, 'dev']); // throws (nonzero) if not ancestor
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

function cmdDigest(args, { gh, log }, root) {
  const idx = args.indexOf('--state');
  const statePath = idx >= 0 && args[idx + 1] ? args[idx + 1] : path.join(root, '.sdlc-cache', 'last-digest.json');

  let prevNumbers = null;
  try {
    if (fs.existsSync(statePath)) {
      prevNumbers = JSON.parse(fs.readFileSync(statePath, 'utf8')).current ?? null;
    }
  } catch {
    prevNumbers = null;
  }

  const snapshot = snapshotOpenIssues(gh, 'number,labels');
  const d = computeDigest(snapshot, prevNumbers);

  log(
    `depths: ${WORKER_LANES.concat('queued')
      .map((l) => `${l} ${d.depths[l]}`)
      .join(' / ')}`,
  );
  log(`parked (needs-human): ${d.parked.length ? d.parked.map((n) => `#${n}`).join(', ') : 'none'}`);
  log(`hold: ${d.hold.length ? d.hold.map((n) => `#${n}`).join(', ') : 'none'}`);
  if (d.arrivals !== null) {
    log(`arrivals vs last cycle: ${d.arrivals.length ? d.arrivals.map((n) => `#${n}`).join(', ') : 'none'}`);
    log(`departures vs last cycle: ${d.departures.length ? d.departures.map((n) => `#${n}`).join(', ') : 'none'}`);
  } else {
    log('arrivals vs last cycle: (no prior snapshot)');
  }

  try {
    fs.mkdirSync(path.dirname(statePath), { recursive: true });
    fs.writeFileSync(statePath, JSON.stringify({ at: new Date().toISOString(), current: d.current }, null, 2));
  } catch (err) {
    log(`digest: could not persist snapshot (${String(err.message).split('\n')[0]})`);
  }
}

const USAGE = `sdlc — deterministic SDLC pipeline one-shots

  Worker-side:
  sdlc claim    <issue> [<run-id> <lane>] [--verify]  add ${WIP_LABEL} + claim comment; --verify exits 1 on a lost race
  sdlc advance  <issue> <to-stage>  validate + swap stage label, drop ${WIP_LABEL}
  sdlc context  <issue>             branch + status + issue labels/state + PRs
  sdlc worktree <issue> [<branch>]  add a sibling git worktree for the branch
  sdlc comment  <issue> <file>      post a body-file comment (plumbing)

  Dispatcher-side:
  sdlc gate     [--reap]            per-issue wip-lock ages (timeline-based) → LIVE/REAP/CLEAR
  sdlc lock     <run-id>            take the dispatcher singleton lock (exits 1 if held)
  sdlc unlock   <run-id>            release the dispatcher singleton lock
  sdlc lanes                        per-lane depth + eligibility + ≠1 stage integrity
  sdlc heal     [<lane>] <issue>    post-worker self-heal check (still locked?)
  sdlc git-maint                    fetch, ff dev, prune merged branches, PR state
  sdlc digest   [--state <file>]    depths, parked/hold, arrivals-diff vs last cycle

Stages: ${STAGES.join(' → ')}`;

const COMMANDS = {
  claim: cmdClaim,
  advance: cmdAdvance,
  context: cmdContext,
  worktree: cmdWorktree,
  comment: cmdComment,
  gate: cmdGate,
  lock: cmdLock,
  unlock: cmdUnlock,
  lanes: cmdLanes,
  heal: cmdHeal,
  'git-maint': cmdGitMaint,
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
  handler(rest, { gh, git, log }, root);
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
