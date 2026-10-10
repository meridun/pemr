// Tests for sdlc/bindings/gh-issue/sdlc.mjs — zero-dependency, node's built-in runner:
//   node --test              (default discovery finds test/*.test.mjs)
//   node --test "test/*.test.mjs"
// (On Node ≥22 the positional arg is a glob, so a bare directory like
// `node --test test/` may not resolve on every platform — prefer the forms above.)
// Ported (and generalized) from an adopting project's Mocha/Chai suite.

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import os from 'os';
import path from 'path';
import fs from 'fs';

import {
  DEFAULT_BRANCH,
  PROD_BRANCH,
  STAGES,
  STAGE_GRAPH,
  WIP_LABEL,
  WIP_STALE_MS,
  MAINT_STALE_MS,
  SdlcError,
  isValidStage,
  isValidTransition,
  currentStage,
  planAdvance,
  planEmit,
  EMIT_OUTCOMES,
  summarizeBounces,
  runSdlc,
  stagesOf,
  priorityRank,
  planGate,
  planMaintLock,
  planClaimVerify,
  lastLabeledAt,
  lastUnlabeledAt,
  computeLanes,
  focusLanes,
  formatFocusLine,
  laneIneligibilityBreakdown,
  planHeal,
  planHealLane,
  mintRunId,
  planBranchPrune,
  parseGoneBranches,
  parseWorktrees,
  planWorktreeSweep,
  unlinkWorktreeRootLinks,
  linkWorktreeNodeModules,
  classifyStatusForSweep,
  computeDigest,
  computeSweep,
  computeSweepAck,
  normalizeEdges,
  openBlockers,
  findDependencyCycles,
  computeDeps,
  parseProseDependencies,
  planDependencyMigration,
  BLOCKED_LABEL,
  READY_LABEL,
  planConflictScan,
  conflictCommentBody,
  CONFLICT_BOUNCE_STAGES,
  dupTokens,
  dupQueryTerms,
  scoreDupCandidates,
  planNodeModulesRefresh,
  mainCheckoutRoot,
  INSTALL_STAMP_PATH,
} from '../sdlc/bindings/gh-issue/sdlc.mjs';

/**
 * Build a fake gh/git executor that records calls and returns canned output.
 * A GraphQL call (`gh api graphql -f query=… -f states=OPEN …`) carries the
 * whole query text, so it is keyed by the short form `api graphql states=<S>`
 * instead (see `graphqlKey`); a canned page never claims `hasNextPage`.
 */
function fakeExec(responses = {}) {
  const calls = [];
  const fn = (args) => {
    calls.push(args);
    const key = args.join(' ');
    if (key in responses) return responses[key];
    if (args[0] === 'api' && args[1] === 'graphql') {
      const states = args.find((a) => /^states=/.test(a)) ?? '';
      return responses[`api graphql ${states}`] ?? '';
    }
    return '';
  };
  fn.calls = calls;
  return fn;
}

/** The fakeExec key for the issue-graph GraphQL query in a given state. */
const graphqlKey = (states) => `api graphql states=${states}`;

/** A canned issue-graph GraphQL page (the shape `fetchIssueGraph` parses). */
function graphqlPage(nodes) {
  return JSON.stringify({
    data: {
      repository: {
        issues: {
          pageInfo: { hasNextPage: false, endCursor: null },
          nodes: nodes.map((n) => ({
            number: n.number,
            databaseId: n.id ?? n.number * 1000,
            title: n.title ?? '',
            state: n.state ?? 'OPEN',
            closedAt: n.closedAt ?? null,
            createdAt: n.createdAt ?? null,
            updatedAt: n.updatedAt ?? n.closedAt ?? n.createdAt ?? null,
            body: n.body ?? '',
            labels: { nodes: (n.labels ?? []).map((name) => ({ name })) },
            blockedBy: { nodes: n.blockedBy ?? [] },
            blocking: { nodes: n.blocking ?? [] },
          })),
        },
      },
    },
  });
}

/** assert.throws with an SdlcError class + message-regex check. */
function throwsSdlc(fn, re) {
  assert.throws(fn, (err) => err instanceof SdlcError && (!re || re.test(err.message)));
}

describe('stage graph', () => {
  it('lists every stage in pipeline order', () => {
    assert.deepEqual(STAGES, ['intake', 'design', 'queued', 'build', 'verify', 'audit', 'ship']);
  });

  it('has a graph node for every declared stage and vice versa', () => {
    assert.deepEqual(Object.keys(STAGE_GRAPH).sort(), [...STAGES].sort());
  });

  it('only points to declared stages', () => {
    for (const [from, tos] of Object.entries(STAGE_GRAPH)) {
      for (const to of tos) {
        assert.equal(isValidStage(to), true, `${from} → ${to}`);
      }
    }
  });

  it('gives ship no forward edge — only the conflict bounce back to build', () => {
    assert.deepEqual(STAGE_GRAPH.ship, ['build']);
  });
});

describe('isValidTransition', () => {
  it('accepts the forward pipeline edges', () => {
    assert.equal(isValidTransition('queued', 'build'), true);
    assert.equal(isValidTransition('build', 'verify'), true);
    assert.equal(isValidTransition('audit', 'ship'), true);
  });

  it('accepts documented bounces', () => {
    assert.equal(isValidTransition('build', 'queued'), true);
    assert.equal(isValidTransition('build', 'design'), true);
    assert.equal(isValidTransition('verify', 'build'), true);
    assert.equal(isValidTransition('queued', 'design'), true); // human spec-rejection bounce
    assert.equal(isValidTransition('ship', 'build'), true); // conflict bounce
  });

  it('accepts the intake shortcuts: queued (declared fold deviation) and verify (already-built floor)', () => {
    assert.equal(isValidTransition('intake', 'queued'), true);
    assert.equal(isValidTransition('intake', 'verify'), true);
  });

  it('rejects skip-ahead jumps', () => {
    assert.equal(isValidTransition('build', 'ship'), false);
    assert.equal(isValidTransition('build', 'audit'), false);
    assert.equal(isValidTransition('queued', 'verify'), false);
  });

  it('rejects an unknown source stage', () => {
    assert.equal(isValidTransition('bogus', 'build'), false);
  });
});

describe('currentStage', () => {
  it('returns the single stage in the label set', () => {
    assert.equal(currentStage(['priority:critical', 'stage:build', WIP_LABEL]), 'build');
  });

  it('throws when there is no stage label', () => {
    throwsSdlc(() => currentStage(['bug', WIP_LABEL]), /no stage/);
  });

  it('throws on multiple stage labels', () => {
    throwsSdlc(() => currentStage(['stage:build', 'stage:verify']), /multiple/);
  });

  it('throws on an unrecognized stage label (the typo class)', () => {
    throwsSdlc(() => currentStage(['stage:verfy']), /unknown stage/);
  });
});

describe('planAdvance', () => {
  it('advances forward and drops sdlc:wip when present', () => {
    const plan = planAdvance(['stage:build', WIP_LABEL], 'verify');
    assert.equal(plan.from, 'build');
    assert.equal(plan.to, 'verify');
    assert.deepEqual([...plan.removeLabels].sort(), ['stage:build', WIP_LABEL].sort());
    assert.deepEqual(plan.addLabels, ['stage:verify']);
  });

  it('does not try to remove sdlc:wip when it is absent', () => {
    const plan = planAdvance(['stage:build'], 'queued');
    assert.deepEqual(plan.removeLabels, ['stage:build']);
  });

  it('rejects an illegal transition with a helpful message', () => {
    throwsSdlc(() => planAdvance(['stage:build'], 'ship'), /illegal transition stage:build → stage:ship/);
  });

  it('rejects an unknown target stage', () => {
    throwsSdlc(() => planAdvance(['stage:build'], 'verfy'), /unknown target/);
  });

  it('rejects a no-op advance to the current stage', () => {
    throwsSdlc(() => planAdvance(['stage:build'], 'build'), /already at/);
  });
});

describe('planEmit', () => {
  it('ADVANCE with a target reuses the advance label math', () => {
    assert.deepEqual(planEmit(['stage:build', WIP_LABEL], 'ADVANCE', 'verify'), {
      removeLabels: ['stage:build', WIP_LABEL],
      addLabels: ['stage:verify'],
      close: false,
    });
  });

  it('BOUNCE with a legal bounce edge swaps back', () => {
    const p = planEmit(['stage:verify', WIP_LABEL], 'BOUNCE', 'build');
    assert.deepEqual(p.removeLabels, ['stage:verify', WIP_LABEL]);
    assert.deepEqual(p.addLabels, ['stage:build']);
  });

  it('terminal ADVANCE from ship needs no target and removes the stage', () => {
    assert.deepEqual(planEmit(['stage:ship', WIP_LABEL], 'ADVANCE'), {
      removeLabels: ['stage:ship', WIP_LABEL],
      addLabels: [],
      close: false,
    });
  });

  it('ADVANCE without a target off any other stage is rejected', () => {
    throwsSdlc(() => planEmit(['stage:build', WIP_LABEL], 'ADVANCE'), /requires a target stage/);
  });

  it('BOUNCE always requires a target', () => {
    throwsSdlc(() => planEmit(['stage:ship', WIP_LABEL], 'BOUNCE'), /requires a target stage/);
  });

  it('ADVANCE/BOUNCE reject illegal transitions', () => {
    throwsSdlc(() => planEmit(['stage:build', WIP_LABEL], 'ADVANCE', 'ship'), /illegal transition/);
  });

  it('PARK drops wip and parks for a human, keeping the stage', () => {
    assert.deepEqual(planEmit(['stage:design', WIP_LABEL], 'PARK'), {
      removeLabels: [WIP_LABEL],
      addLabels: ['sdlc:needs-human'],
      close: false,
    });
  });

  it('CONTINUE only drops wip', () => {
    assert.deepEqual(planEmit(['stage:build', WIP_LABEL], 'CONTINUE'), {
      removeLabels: [WIP_LABEL],
      addLabels: [],
      close: false,
    });
  });

  it('CLOSE drops wip and closes', () => {
    assert.deepEqual(planEmit(['stage:intake', WIP_LABEL], 'CLOSE'), {
      removeLabels: [WIP_LABEL],
      addLabels: [],
      close: true,
    });
  });

  it('rejects unknown outcomes', () => {
    throwsSdlc(() => planEmit(['stage:build'], 'FROBNICATE'), /unknown outcome/);
    assert.deepEqual(EMIT_OUTCOMES, ['ADVANCE', 'BOUNCE', 'PARK', 'CONTINUE', 'CLOSE']);
  });
});

describe('summarizeBounces (bounce cap)', () => {
  const bounce = (runId, to, createdAt) => ({
    body: `sdlc:emit ${runId} BOUNCE → stage:${to}`,
    createdAt,
  });

  it('returns nothing for empty or missing input', () => {
    assert.deepEqual(summarizeBounces([]), []);
    assert.deepEqual(summarizeBounces(null), []);
  });

  it('counts same-pair bounces, deriving the source lane from the run-id', () => {
    const comments = [
      bounce('dispatch-20260823-1707-ce23-verify', 'build', '2026-08-23T10:00:00Z'),
      bounce('dispatch-20260823-1807-aa11-verify', 'build', '2026-08-23T12:00:00Z'),
    ];
    assert.deepEqual(summarizeBounces(comments), [{ from: 'verify', to: 'build', count: 2 }]);
  });

  it('ignores non-BOUNCE markers and prose that merely mentions a bounce', () => {
    const comments = [
      { body: 'sdlc:emit run-a ADVANCE → stage:verify', createdAt: '2026-08-23T10:00:00Z' },
      { body: 'sdlc:emit run-b PARK', createdAt: '2026-08-23T10:05:00Z' },
      { body: 'sdlc:emit run-c CONTINUE', createdAt: '2026-08-23T10:10:00Z' },
      {
        body: 'sdlc:emit run-d ADVANCE → stage:audit\n\nprior sdlc:emit run-x BOUNCE → stage:build was fixed',
        createdAt: '2026-08-23T10:15:00Z',
      },
      { body: 'sdlc:claim run-e build', createdAt: '2026-08-23T10:20:00Z' },
    ];
    assert.deepEqual(summarizeBounces(comments), []);
  });

  it('marks an unrecognizable lane or a missing target as ?', () => {
    const comments = [
      bounce('handrolled', 'design', '2026-08-23T10:00:00Z'),
      { body: 'sdlc:emit dispatch-20260823-1707-ce23-audit BOUNCE', createdAt: '2026-08-23T11:00:00Z' },
    ];
    assert.deepEqual(summarizeBounces(comments), [
      { from: '?', to: 'design', count: 1 },
      { from: 'audit', to: '?', count: 1 },
    ]);
  });

  it('accepts the ASCII arrow fallback alongside the Unicode arrow cmdEmit writes', () => {
    const comments = [
      { body: 'sdlc:emit run-a-verify BOUNCE -> stage:build', createdAt: '2026-08-23T10:00:00Z' },
      bounce('run-b-verify', 'build', '2026-08-23T11:00:00Z'),
    ];
    assert.deepEqual(summarizeBounces(comments), [{ from: 'verify', to: 'build', count: 2 }]);
  });

  it('orders pairs by count desc, then from, then to', () => {
    const comments = [
      bounce('r1-audit', 'build', '2026-08-23T10:00:00Z'),
      bounce('r2-verify', 'build', '2026-08-23T11:00:00Z'),
      bounce('r3-verify', 'build', '2026-08-23T12:00:00Z'),
      bounce('r4-build', 'design', '2026-08-23T13:00:00Z'),
    ];
    assert.deepEqual(summarizeBounces(comments), [
      { from: 'verify', to: 'build', count: 2 },
      { from: 'audit', to: 'build', count: 1 },
      { from: 'build', to: 'design', count: 1 },
    ]);
  });
});

describe('runSdlc context (bounce advisory)', () => {
  const COMMENTS_KEY = 'issue view 7 --json comments --jq [.comments[] | {body: .body, createdAt: .createdAt}]';
  const contextGh = (comments) => {
    const responses = {
      'issue view 7 --json number,title,state,labels':
        JSON.stringify({ number: 7, title: 'T', state: 'OPEN', labels: [{ name: 'stage:build' }] }),
      'pr list --head feat/7 --json number,title,state': '[]',
    };
    if (comments !== undefined) responses[COMMENTS_KEY] = comments;
    return fakeExec(responses);
  };
  const git = fakeExec({ 'rev-parse --abbrev-ref HEAD': 'feat/7\n', 'status --short': '' });

  it('reports the per-pair bounce counts', () => {
    const gh = contextGh(JSON.stringify([
      { body: 'sdlc:emit r1-verify BOUNCE → stage:build', createdAt: '2026-08-23T10:00:00Z' },
      { body: 'sdlc:emit r2-verify BOUNCE → stage:build', createdAt: '2026-08-23T11:00:00Z' },
    ]));
    const logs = [];
    runSdlc(['context', '7'], { gh, git, log: (m) => logs.push(m) });
    assert.ok(logs.includes('bounces: verify→build ×2'), logs.join('\n'));
  });

  it('reports none when the issue never bounced', () => {
    const logs = [];
    runSdlc(['context', '7'], { gh: contextGh('[]'), git, log: (m) => logs.push(m) });
    assert.ok(logs.includes('bounces: none'), logs.join('\n'));
  });

  it('degrades to "unavailable" instead of throwing when the comment fetch fails', () => {
    const gh = contextGh();
    const wrapped = (args) => {
      if (args.join(' ') === COMMENTS_KEY) throw new Error('gh exploded');
      return gh(args);
    };
    const logs = [];
    assert.doesNotThrow(() => runSdlc(['context', '7'], { gh: wrapped, git, log: (m) => logs.push(m) }));
    assert.ok(logs.includes('bounces: unavailable'), logs.join('\n'));
  });
});

describe('stagesOf / priorityRank', () => {
  it('extracts every stage suffix', () => {
    assert.deepEqual(stagesOf(['bug', 'stage:build', 'stage:verify']), ['build', 'verify']);
    assert.deepEqual(stagesOf(['bug']), []);
  });

  // pemr deviation (sdlc/PROFILE.md): priority:medium is retired; unlabeled is the
  // default tier and sorts between critical and future.
  it('ranks critical < unlabeled < future (priority:medium is not a tier)', () => {
    assert.equal(priorityRank(['priority:critical']), 0);
    assert.equal(priorityRank(['bug']), 1);
    assert.equal(priorityRank(['priority:medium']), 1);
    assert.equal(priorityRank(['priority:future']), 2);
  });
});

describe('planGate', () => {
  it('treats a fresh lock as a live worker, never an abort', () => {
    const plan = planGate([{ number: 1, ageMs: WIP_STALE_MS + 1 }, { number: 2, ageMs: 5 }]);
    assert.equal(plan.decision, 'reap');
    assert.deepEqual(plan.live.map((i) => i.number), [2]);
    assert.deepEqual(plan.reap.map((i) => i.number), [1]);
  });

  it('reports live when only fresh locks exist', () => {
    const plan = planGate([{ number: 2, ageMs: 5 }]);
    assert.equal(plan.decision, 'live');
    assert.deepEqual(plan.reap, []);
  });

  it('reaps every lock at/over the threshold', () => {
    const plan = planGate([{ number: 3, ageMs: WIP_STALE_MS }, { number: 4, ageMs: WIP_STALE_MS * 2 }]);
    assert.equal(plan.decision, 'reap');
    assert.deepEqual(plan.reap.map((i) => i.number), [3, 4]);
  });

  it('is clear with no locks', () => {
    assert.equal(planGate([]).decision, 'clear');
  });
});

describe('planMaintLock', () => {
  it('acquires when no lock exists', () => {
    assert.equal(planMaintLock({ exists: false }).action, 'acquire');
    assert.equal(planMaintLock(null).action, 'acquire');
  });

  it('skips (never aborts) while a live run holds the lock', () => {
    assert.equal(planMaintLock({ exists: true, ageMs: MAINT_STALE_MS - 1 }).action, 'skip');
  });

  it('reaps a stale lock (holder presumed dead at the threshold)', () => {
    assert.equal(planMaintLock({ exists: true, ageMs: MAINT_STALE_MS }).action, 'reap');
    assert.equal(planMaintLock({ exists: true, ageMs: MAINT_STALE_MS * 4 }).action, 'reap');
  });
});

describe('runSdlc emit', () => {
  const COMMENTS_KEY = 'issue view 609 --json comments --jq [.comments[] | {body: .body, createdAt: .createdAt}]';
  const LABELS_KEY = 'issue view 609 --json labels --jq .labels[].name';
  const ownClaim = JSON.stringify([
    { body: 'sdlc:claim run-a build', createdAt: '2026-07-09T12:00:00Z' },
  ]);

  it('posts the marker comment before the label edit', () => {
    const gh = fakeExec({
      [COMMENTS_KEY]: ownClaim,
      [LABELS_KEY]: 'stage:build\nsdlc:wip\n',
    });
    const logs = [];
    runSdlc(['emit', '609', 'run-a', 'ADVANCE', '--to', 'verify', '--body', 'tests green'], {
      gh,
      log: (m) => logs.push(m),
    });

    const commentIdx = gh.calls.findIndex((c) => c[1] === 'comment');
    const editIdx = gh.calls.findIndex((c) => c[1] === 'edit');
    assert.ok(commentIdx > -1);
    assert.ok(editIdx > commentIdx, 'marker comment must precede the label edit');
    assert.deepEqual(gh.calls[commentIdx], [
      'issue', 'comment', '609', '--body',
      'sdlc:emit run-a ADVANCE → stage:verify\n\ntests green',
    ]);
    assert.deepEqual(gh.calls[editIdx], [
      'issue', 'edit', '609',
      '--remove-label', 'stage:build', '--remove-label', WIP_LABEL,
      '--add-label', 'stage:verify',
    ]);
    assert.ok(logs.join('\n').includes('ADVANCE → stage:verify'));
  });

  it('refuses to emit when another run-id owns the live claim', () => {
    const gh = fakeExec({
      [COMMENTS_KEY]: JSON.stringify([
        { body: 'sdlc:claim run-old build', createdAt: '2026-07-09T11:00:00Z' },
        { body: 'sdlc:claim run-a build', createdAt: '2026-07-09T12:00:00Z' },
      ]),
    });
    throwsSdlc(
      () => runSdlc(['emit', '609', 'run-a', 'ADVANCE', '--to', 'verify'], { gh, log: () => {} }),
      /owned by run-old/,
    );
    assert.equal(gh.calls.some((c) => c[1] === 'comment' || c[1] === 'edit'), false);
  });

  it('refuses to emit when the claim was already settled', () => {
    const gh = fakeExec({
      [COMMENTS_KEY]: JSON.stringify([
        { body: 'sdlc:claim run-a build', createdAt: '2026-07-09T12:00:00Z' },
        { body: 'sdlc:emit run-a ADVANCE → stage:verify', createdAt: '2026-07-09T12:30:00Z' },
      ]),
    });
    throwsSdlc(
      () => runSdlc(['emit', '609', 'run-a', 'ADVANCE', '--to', 'verify'], { gh, log: () => {} }),
      /no live sdlc:claim/,
    );
  });

  it('CLOSE comments, drops wip, and closes the issue', () => {
    const gh = fakeExec({
      [COMMENTS_KEY]: JSON.stringify([
        { body: 'sdlc:claim run-a intake', createdAt: '2026-07-09T12:00:00Z' },
      ]),
      [LABELS_KEY]: 'stage:intake\nsdlc:wip\n',
    });
    runSdlc(['emit', '609', 'run-a', 'CLOSE', '--body', 'duplicate of #123'], { gh, log: () => {} });
    assert.equal(gh.calls.some((c) => c[1] === 'close'), true);
    const edit = gh.calls.find((c) => c[1] === 'edit');
    assert.deepEqual(edit, ['issue', 'edit', '609', '--remove-label', WIP_LABEL]);
  });

  it('requires run-id and outcome', () => {
    throwsSdlc(() => runSdlc(['emit', '609', 'run-a'], { gh: fakeExec(), log: () => {} }), /requires a run-id and outcome/);
  });
});

describe('planClaimVerify', () => {
  const t = (min) => new Date(Date.UTC(2026, 6, 9, 12, min)).toISOString();

  it('wins when mine is the only claim', () => {
    const r = planClaimVerify([{ body: 'sdlc:claim run-a build', createdAt: t(1) }], 'run-a');
    assert.equal(r.won, true);
  });

  it('loses to an earlier claim', () => {
    const r = planClaimVerify(
      [
        { body: 'sdlc:claim run-a build', createdAt: t(1) },
        { body: 'sdlc:claim run-b build', createdAt: t(2) },
      ],
      'run-b',
    );
    assert.equal(r.won, false);
    assert.equal(r.winner, 'run-a');
  });

  it('breaks a timestamp tie to the lexicographically lower run-id', () => {
    const r = planClaimVerify(
      [
        { body: 'sdlc:claim run-b build', createdAt: t(1) },
        { body: 'sdlc:claim run-a build', createdAt: t(1) },
      ],
      'run-a',
    );
    assert.equal(r.won, true);
  });

  it('ignores claims settled by a prior legacy outcome comment', () => {
    const r = planClaimVerify(
      [
        { body: 'sdlc:claim run-old build', createdAt: t(0) },
        { body: 'BOUNCE → stage:build — flaky test', createdAt: t(1) },
        { body: 'sdlc:claim run-a build', createdAt: t(2) },
      ],
      'run-a',
    );
    assert.equal(r.won, true);
  });

  it('reports not-found when my claim comment is absent', () => {
    const r = planClaimVerify([], 'run-a');
    assert.equal(r.won, false);
    assert.equal(r.reason, 'own claim not found');
  });

  it('treats an sdlc:emit marker as settling prior claims', () => {
    const r = planClaimVerify(
      [
        { body: 'sdlc:claim run-old intake', createdAt: t(0) },
        { body: 'sdlc:emit run-old ADVANCE → stage:queued\n\nIntake summary: …', createdAt: t(1) },
        { body: 'sdlc:claim run-a build', createdAt: t(2) },
      ],
      'run-a',
    );
    assert.equal(r.won, true);
  });

  it('does NOT settle on a freeform prose outcome (the phantom-lock bug)', () => {
    // Regression: an "Intake summary: …" comment is not a completion signal;
    // only sdlc:emit / legacy outcome prefixes / a reap / the timeline
    // boundary settle claims.
    const r = planClaimVerify(
      [
        { body: 'sdlc:claim run-old intake', createdAt: t(0) },
        { body: 'Intake summary: deterministic work-list generator …', createdAt: t(1) },
        { body: 'sdlc:claim run-a build', createdAt: t(2) },
      ],
      'run-a',
    );
    assert.equal(r.won, false);
    assert.equal(r.winner, 'run-old');
  });

  it('the timeline boundary settles claims at/before the last sdlc:wip unlabel', () => {
    // A reaper strip removes the label with NO outcome comment — the unlabeled
    // event is the machine-visible boundary invalidating the dead worker's claim.
    const r = planClaimVerify(
      [
        { body: 'sdlc:claim run-dead build', createdAt: t(0) },
        { body: 'sdlc:claim run-a build', createdAt: t(2) },
      ],
      'run-a',
      t(1), // boundary: reaper stripped sdlc:wip between the two claims
    );
    assert.equal(r.won, true);
  });

  it('a null boundary degrades to outcome-marker settling only', () => {
    const r = planClaimVerify(
      [
        { body: 'sdlc:claim run-dead build', createdAt: t(0) },
        { body: 'sdlc:claim run-a build', createdAt: t(2) },
      ],
      'run-a',
      null,
    );
    assert.equal(r.won, false);
    assert.equal(r.winner, 'run-dead');
  });

  it('a boundary equal to my own claim time settles mine too (own claim not found)', () => {
    const r = planClaimVerify(
      [{ body: 'sdlc:claim run-a build', createdAt: t(1) }],
      'run-a',
      t(1),
    );
    assert.equal(r.won, false);
    assert.equal(r.reason, 'own claim not found');
  });
});

describe('lastLabeledAt', () => {
  it('returns the most recent matching labeled event', () => {
    const events = [
      { event: 'labeled', label: { name: WIP_LABEL }, created_at: '2026-07-08T01:00:00Z' },
      { event: 'labeled', label: { name: 'bug' }, created_at: '2026-07-08T05:00:00Z' },
      { event: 'labeled', label: { name: WIP_LABEL }, created_at: '2026-07-08T03:00:00Z' },
    ];
    assert.equal(lastLabeledAt(events, WIP_LABEL), '2026-07-08T03:00:00Z');
  });

  it('returns null when the label was never added', () => {
    assert.equal(lastLabeledAt([{ event: 'commented' }], WIP_LABEL), null);
  });
});

describe('lastUnlabeledAt', () => {
  it('returns the most recent matching unlabeled event (the claim boundary)', () => {
    const events = [
      { event: 'labeled', label: { name: WIP_LABEL }, created_at: '2026-07-08T01:00:00Z' },
      { event: 'unlabeled', label: { name: WIP_LABEL }, created_at: '2026-07-08T02:00:00Z' },
      { event: 'unlabeled', label: { name: 'bug' }, created_at: '2026-07-08T05:00:00Z' },
      { event: 'unlabeled', label: { name: WIP_LABEL }, created_at: '2026-07-08T04:00:00Z' },
    ];
    assert.equal(lastUnlabeledAt(events, WIP_LABEL), '2026-07-08T04:00:00Z');
  });

  it('returns null when the label was never removed', () => {
    assert.equal(
      lastUnlabeledAt(
        [{ event: 'labeled', label: { name: WIP_LABEL }, created_at: '2026-07-08T01:00:00Z' }],
        WIP_LABEL,
      ),
      null,
    );
  });
});

describe('computeLanes', () => {
  const issues = [
    { number: 10, createdAt: '2026-07-01T00:00:00Z', labels: ['stage:build', 'priority:future'] },
    { number: 11, createdAt: '2026-07-02T00:00:00Z', labels: ['stage:build', 'priority:critical'] },
    { number: 12, createdAt: '2026-07-03T00:00:00Z', labels: ['stage:build', WIP_LABEL] },
    { number: 13, createdAt: '2026-07-04T00:00:00Z', labels: ['stage:build', 'sdlc:hold'] },
    { number: 14, createdAt: '2026-07-05T00:00:00Z', labels: ['stage:build', 'stage:verify'] },
    { number: 15, createdAt: '2026-07-06T00:00:00Z', labels: ['bug'] },
    { number: 16, createdAt: '2026-07-07T00:00:00Z', labels: [WIP_LABEL] },
  ];

  it('counts depth including locked and parked items', () => {
    const { lanes } = computeLanes(issues);
    assert.equal(lanes.build.depth, 5); // 10,11,12,13,14
  });

  it('excludes wip/hold/needs-human from eligible and orders by priority then FIFO', () => {
    const { lanes } = computeLanes(issues);
    // 11 (critical) › 14 (unlabeled) › 10 (future); 12 (wip) and 13 (hold)
    // excluded; 14 is dual-stage but still eligible for build.
    assert.deepEqual(lanes.build.eligible, [11, 14, 10]);
  });

  it('sorts unlabeled between critical and future, FIFO within a tier', () => {
    const { lanes } = computeLanes([
      { number: 20, createdAt: '2026-07-01T00:00:00Z', labels: ['stage:build', 'priority:future'] },
      { number: 21, createdAt: '2026-07-02T00:00:00Z', labels: ['stage:build'] },
      { number: 22, createdAt: '2026-07-03T00:00:00Z', labels: ['stage:build', 'priority:critical'] },
      { number: 23, createdAt: '2026-07-04T00:00:00Z', labels: ['stage:build', 'priority:critical'] },
      { number: 24, createdAt: '2026-07-05T00:00:00Z', labels: ['stage:build'] },
    ]);
    assert.deepEqual(lanes.build.eligible, [22, 23, 21, 24, 20]);
  });

  it('flags multi-stage issues and stage-less issues carrying machine flags', () => {
    const { integrity } = computeLanes(issues);
    const byNum = Object.fromEntries(integrity.map((v) => [v.number, v.stages]));
    assert.deepEqual(byNum[14], ['build', 'verify']);
    // 15 has no stage label and no machine flag — a legitimate state.
    assert.equal(byNum[15], undefined);
    // 16 has no stage label but a stuck sdlc:wip — corrupt, flag it.
    assert.deepEqual(byNum[16], []);
    assert.equal(byNum[10], undefined);
  });

  it('buckets ineligible items by hold/needs-human/wip, summing to depth − eligible', () => {
    const { lanes } = computeLanes(issues);
    // build depth 5, eligible [11,10,14] → 2 ineligible: #12 wip, #13 hold.
    assert.deepEqual(lanes.build.ineligible, { hold: 1, 'needs-human': 0, wip: 1, blocked: 0 });
    const b = lanes.build;
    const inelig = b.ineligible.hold + b.ineligible['needs-human'] + b.ineligible.wip + b.ineligible.blocked;
    assert.equal(inelig, b.depth - b.eligible.length);
  });

  it('buckets each ineligible item once, hold › needs-human › wip › blocked precedence', () => {
    const { lanes } = computeLanes([
      { number: 20, createdAt: '2026-07-01T00:00:00Z', labels: ['stage:audit', 'sdlc:hold', WIP_LABEL] },
      { number: 21, createdAt: '2026-07-02T00:00:00Z', labels: ['stage:audit', 'sdlc:needs-human', WIP_LABEL] },
      { number: 22, createdAt: '2026-07-03T00:00:00Z', labels: ['stage:audit', WIP_LABEL], blockedBy: [{ number: 1, state: 'OPEN' }] },
    ]);
    assert.deepEqual(lanes.audit.ineligible, { hold: 1, 'needs-human': 1, wip: 1, blocked: 0 });
    assert.deepEqual(lanes.audit.blocked, []);
  });

  describe('native dependency gate (#27)', () => {
    const at = (d) => `2026-07-0${d}T00:00:00Z`;

    it('never returns an issue with an OPEN native blocker as eligible', () => {
      const { lanes } = computeLanes([
        { number: 30, createdAt: at(1), labels: ['stage:build', 'priority:critical'], blockedBy: [{ number: 29, state: 'OPEN' }] },
        { number: 31, createdAt: at(2), labels: ['stage:build'] },
      ]);
      assert.deepEqual(lanes.build.eligible, [31]);
      assert.equal(lanes.build.ineligible.blocked, 1);
      assert.deepEqual(lanes.build.blocked, [{ number: 30, blockers: [29] }]);
    });

    it('a CLOSED blocker is history — the dependent is eligible', () => {
      const { lanes } = computeLanes([
        { number: 30, createdAt: at(1), labels: ['stage:build'], blockedBy: [{ number: 29, state: 'CLOSED' }] },
      ]);
      assert.deepEqual(lanes.build.eligible, [30]);
      assert.equal(lanes.build.ineligible.blocked, 0);
    });

    it('mixed blockers: one still OPEN keeps the dependent blocked, listing only the open ones', () => {
      const { lanes } = computeLanes([
        {
          number: 30, createdAt: at(1), labels: ['stage:build'],
          blockedBy: [{ number: 28, state: 'CLOSED' }, { number: 29, state: 'OPEN' }, { number: 27, state: 'OPEN' }],
        },
      ]);
      assert.deepEqual(lanes.build.eligible, []);
      assert.deepEqual(lanes.build.blocked, [{ number: 30, blockers: [27, 29] }]);
    });

    it('the blocked/ready labels are NOT consulted — only edges gate', () => {
      const { lanes } = computeLanes([
        // `ready` label but an open edge → blocked (label drift can't unblock).
        { number: 40, createdAt: at(1), labels: ['stage:build', READY_LABEL], blockedBy: [{ number: 1, state: 'OPEN' }] },
        // `blocked` label but no edge → eligible (label drift can't block).
        { number: 41, createdAt: at(2), labels: ['stage:build', BLOCKED_LABEL] },
      ]);
      assert.deepEqual(lanes.build.eligible, [41]);
      assert.deepEqual(lanes.build.blocked, [{ number: 40, blockers: [1] }]);
    });

    it('accepts the raw GraphQL connection shape and a snapshot without edges', () => {
      const { lanes } = computeLanes([
        { number: 50, createdAt: at(1), labels: ['stage:verify'], blockedBy: { nodes: [{ number: 2, state: 'OPEN' }] } },
        { number: 51, createdAt: at(2), labels: ['stage:verify'] },
      ]);
      assert.deepEqual(lanes.verify.eligible, [51]);
      assert.equal(lanes.verify.ineligible.blocked, 1);
    });
  });
});

describe('dependency edge helpers', () => {
  it('normalizeEdges accepts a connection, a node array, or nothing', () => {
    assert.deepEqual(normalizeEdges({ nodes: [{ number: 3, state: 'open' }] }), [{ number: 3, state: 'OPEN' }]);
    assert.deepEqual(normalizeEdges([{ number: '4' }]), [{ number: 4, state: 'OPEN' }]);
    assert.deepEqual(normalizeEdges(null), []);
    assert.deepEqual(normalizeEdges({ nodes: [null, {}] }), []);
  });

  it('openBlockers returns only OPEN blockers, ascending', () => {
    assert.deepEqual(
      openBlockers([{ number: 9, state: 'OPEN' }, { number: 4, state: 'CLOSED' }, { number: 2, state: 'OPEN' }]),
      [2, 9],
    );
    assert.deepEqual(openBlockers(undefined), []);
  });
});

describe('laneIneligibilityBreakdown', () => {
  it('renders only non-zero buckets in hold, needs-human, wip order', () => {
    assert.equal(
      laneIneligibilityBreakdown({ ineligible: { hold: 12, 'needs-human': 4, wip: 0 } }),
      '(hold 12, needs-human 4)',
    );
    assert.equal(laneIneligibilityBreakdown({ ineligible: { hold: 0, 'needs-human': 0, wip: 1 } }), '(wip 1)');
    assert.equal(
      laneIneligibilityBreakdown({ ineligible: { hold: 0, 'needs-human': 0, wip: 1, blocked: 2 } }),
      '(wip 1, blocked 2)',
    );
  });

  it('returns empty string for a fully-eligible lane (all buckets zero)', () => {
    assert.equal(laneIneligibilityBreakdown({ ineligible: { hold: 0, 'needs-human': 0, wip: 0 } }), '');
    assert.equal(laneIneligibilityBreakdown({}), '');
  });
});

describe('focusLanes / formatFocusLine (focused cycle --issue)', () => {
  const at = (d) => `2026-07-0${d}T00:00:00Z`;
  const snapshot = [
    { number: 1, createdAt: at(1), labels: ['stage:build', 'priority:critical'] },
    { number: 2, createdAt: at(2), labels: ['stage:build'] },
    { number: 3, createdAt: at(3), labels: ['stage:verify', 'sdlc:needs-human'] },
    { number: 4, createdAt: at(4), labels: ['stage:build', WIP_LABEL], blockedBy: [{ number: 9, state: 'OPEN' }] },
    { number: 5, createdAt: at(5), labels: ['stage:audit'], blockedBy: [{ number: 8, state: 'OPEN' }, { number: 7, state: 'OPEN' }] },
    { number: 6, createdAt: at(6), labels: ['stage:queued'] },
    { number: 7, createdAt: at(7), labels: ['bug'] },
    { number: 8, createdAt: at(8), labels: ['stage:build', 'stage:verify'] },
    { number: 10, createdAt: at(9), labels: ['stage:build', 'sdlc:hold', 'sdlc:needs-human'] },
    { number: 11, createdAt: at(9), labels: ['stage:shipping'] },
  ];
  const line = (n) => formatFocusLine(focusLanes(snapshot, n).focus);

  it('filters eligible/blocked to the focus issue while depths, breakdowns and integrity stay global', () => {
    const full = computeLanes(snapshot);
    const { lanes, integrity, focus } = focusLanes(snapshot, 2);
    assert.deepEqual(full.lanes.build.eligible, [1, 2, 8]);
    assert.deepEqual(lanes.build.eligible, [2]);
    assert.deepEqual(lanes.verify.eligible, []);
    assert.deepEqual(lanes.audit.blocked, []);
    for (const lane of Object.keys(full.lanes)) {
      assert.equal(lanes[lane].depth, full.lanes[lane].depth);
      assert.deepEqual(lanes[lane].ineligible, full.lanes[lane].ineligible);
    }
    assert.deepEqual(integrity, full.integrity);
    assert.deepEqual(focus, { number: 2, verdict: 'eligible', lane: 'build' });
  });

  it('keeps the focus issue in its lane blocked list', () => {
    assert.deepEqual(focusLanes(snapshot, 5).lanes.audit.blocked, [{ number: 5, blockers: [7, 8] }]);
  });

  it('accepts a string issue number (as requireIssue returns)', () => {
    assert.deepEqual(focusLanes(snapshot, '2').lanes.build.eligible, [2]);
  });

  it('verdict: eligible', () => {
    assert.equal(line(1), 'focus #1: eligible in build');
  });

  it('verdict: ineligible (needs-human)', () => {
    assert.equal(line(3), 'focus #3: ineligible in verify (needs-human)');
  });

  it('verdict: ineligible lists every reason in hold › needs-human › wip › blocked order', () => {
    assert.equal(line(4), 'focus #4: ineligible in build (wip, blocked by #9)');
    assert.equal(line(10), 'focus #10: ineligible in build (hold, needs-human)');
    assert.deepEqual(focusLanes(snapshot, 4).focus, {
      number: 4, verdict: 'ineligible', lane: 'build', reasons: ['wip', 'blocked'], blockers: [9],
    });
  });

  it('verdict: ineligible (blocked by several open blockers, ascending)', () => {
    assert.equal(line(5), 'focus #5: ineligible in audit (blocked by #7, #8)');
  });

  it('verdict: queued is a human gate even when unflagged', () => {
    assert.equal(line(6), 'focus #6: queued (human gate)');
  });

  it('verdict: no stage label', () => {
    assert.equal(line(7), 'focus #7: no stage label');
  });

  it('verdict: multiple stage labels', () => {
    assert.equal(line(8), 'focus #8: multiple stage labels (build, verify)');
  });

  it('verdict: unknown stage label', () => {
    assert.equal(line(11), 'focus #11: unknown stage label (shipping)');
  });

  it('verdict: not open (absent from the open snapshot)', () => {
    const { lanes, focus } = focusLanes(snapshot, 999);
    assert.equal(formatFocusLine(focus), 'focus #999: not open (closed or nonexistent)');
    assert.equal(Object.values(lanes).every((l) => l.eligible.length === 0), true);
  });
});

describe('runSdlc lanes --issue (focused cycle)', () => {
  const snapshotKey = 'issue list --state open --json number,labels,createdAt,title --limit 200';
  const makeGh = () => fakeExec({
    [snapshotKey]: JSON.stringify([
      { number: 1, createdAt: '2026-07-01T00:00:00Z', labels: [{ name: 'stage:build' }] },
      { number: 2, createdAt: '2026-07-02T00:00:00Z', labels: [{ name: 'stage:build' }] },
      { number: 3, createdAt: '2026-07-03T00:00:00Z', labels: [{ name: 'bug' }, { name: WIP_LABEL }] },
    ]),
    [graphqlKey('OPEN')]: graphqlPage([
      { number: 1 }, { number: 2, blockedBy: [{ number: 3, state: 'OPEN' }] }, { number: 3 },
    ]),
  });
  const run = (argv) => {
    const logs = [];
    runSdlc(argv, { gh: makeGh(), log: (m) => logs.push(m) });
    return logs;
  };

  it('without --issue the output is unchanged (no focus line, global eligibility)', () => {
    assert.deepEqual(run(['lanes']), [
      'intake: depth 0, eligible —',
      'design: depth 0, eligible —',
      'build: depth 2, eligible #1 (blocked 1)',
      'verify: depth 0, eligible —',
      'audit: depth 0, eligible —',
      'ship: depth 0, eligible —',
      'queued: depth 0, eligible —',
      'blocked (open native blockers): #2 ← #3',
      'integrity (corrupt stage-label state — needs human):',
      '  #3: no stage label (but wip/needs-human)',
    ]);
  });

  it('filters eligible/blocked to the focus issue, keeps depth + integrity global, prints the verdict', () => {
    assert.deepEqual(run(['lanes', '--issue', '2']), [
      'intake: depth 0, eligible —',
      'design: depth 0, eligible —',
      'build: depth 2, eligible — (blocked 1)',
      'verify: depth 0, eligible —',
      'audit: depth 0, eligible —',
      'ship: depth 0, eligible —',
      'queued: depth 0, eligible —',
      'blocked (open native blockers): #2 ← #3',
      'focus #2: ineligible in build (blocked by #3)',
      'integrity (corrupt stage-label state — needs human):',
      '  #3: no stage label (but wip/needs-human)',
    ]);
    const other = run(['lanes', '--issue', '#1']);
    assert.ok(other.includes('build: depth 2, eligible #1 (blocked 1)'));
    assert.equal(other.some((l) => l.startsWith('blocked (open native blockers)')), false);
    assert.ok(other.includes('focus #1: eligible in build'));
  });

  it('rejects an invalid or missing --issue value before fetching anything', () => {
    const gh = makeGh();
    throwsSdlc(() => runSdlc(['lanes', '--issue', 'abc'], { gh, log: () => {} }), /expected an issue number/);
    throwsSdlc(() => runSdlc(['lanes', '--issue'], { gh, log: () => {} }), /expected an issue number/);
    assert.deepEqual(gh.calls, []);
  });
});

describe('planHeal', () => {
  it('reports still-locked when sdlc:wip remains', () => {
    assert.equal(planHeal(['stage:verify', WIP_LABEL]).stillLocked, true);
    assert.equal(planHeal(['stage:verify']).stillLocked, false);
  });
});

describe('planHealLane', () => {
  const issues = [
    { number: 10, labels: ['stage:build', WIP_LABEL] },
    { number: 11, labels: ['stage:build'] }, // healthy: emitted, wip gone
    { number: 12, labels: ['stage:verify', WIP_LABEL] }, // different lane
    { number: 13, labels: [{ name: 'stage:build' }, { name: WIP_LABEL }] }, // object labels
  ];

  it('finds only stage:<lane> issues that still carry sdlc:wip', () => {
    assert.deepEqual(planHealLane(issues, 'build'), [10, 13]);
  });

  it('is empty when no wip issue sits in the lane (the zero case)', () => {
    assert.deepEqual(planHealLane(issues, 'audit'), []);
    assert.deepEqual(planHealLane([], 'build'), []);
  });

  it('returns the single stalled issue (the one case)', () => {
    assert.deepEqual(planHealLane(issues, 'verify'), [12]);
  });
});

describe('runSdlc heal (lane auto-discovery arg parsing)', () => {
  it('heal <lane> with no issue auto-discovers every stalled wip issue', () => {
    const gh = fakeExec({
      'issue list --state open --json number,labels --limit 200': JSON.stringify([
        { number: 7, labels: [{ name: 'stage:build' }, { name: WIP_LABEL }] },
        { number: 8, labels: [{ name: 'stage:build' }] }, // healthy
        { number: 9, labels: [{ name: 'stage:build' }, { name: WIP_LABEL }] },
      ]),
    });
    const logs = [];
    runSdlc(['heal', 'build'], { gh, log: (m) => logs.push(m) });
    const out = logs.join('\n');
    assert.ok(out.includes('#7 (build) STALLED'));
    assert.ok(out.includes('#9 (build) STALLED'));
    assert.ok(!out.includes('#8'));
  });

  it('heal <lane> with no stalled issues reports the lane OK', () => {
    const gh = fakeExec({
      'issue list --state open --json number,labels --limit 200': JSON.stringify([
        { number: 8, labels: [{ name: 'stage:build' }] },
      ]),
    });
    const logs = [];
    runSdlc(['heal', 'build'], { gh, log: (m) => logs.push(m) });
    assert.ok(logs.join('\n').includes('build OK — no stalled'));
  });

  it('heal <lane> <issue> still checks the explicit issue', () => {
    const gh = fakeExec({
      'issue view 5 --json labels --jq .labels[].name': `stage:build\n${WIP_LABEL}\n`,
    });
    const logs = [];
    runSdlc(['heal', 'build', '5'], { gh, log: (m) => logs.push(m) });
    assert.ok(logs.join('\n').includes('STALLED'));
  });

  it('heal rejects an unknown lane-only argument', () => {
    throwsSdlc(() => runSdlc(['heal', 'bogus'], { gh: fakeExec(), log: () => {} }), /unknown lane/);
  });
});

describe('runSdlc claim --next', () => {
  const LANES_KEY = 'issue list --state open --json number,labels,createdAt,title --limit 200';
  const commentsKey = (n) =>
    `issue view ${n} --json comments --jq [.comments[] | {body: .body, createdAt: .createdAt}]`;

  it('picks the highest-priority/oldest eligible issue and claims it', () => {
    const gh = fakeExec({
      [LANES_KEY]: JSON.stringify([
        { number: 10, labels: [{ name: 'stage:build' }, { name: 'priority:future' }], createdAt: '2026-07-01T00:00:00Z' },
        { number: 11, labels: [{ name: 'stage:build' }, { name: 'priority:critical' }], createdAt: '2026-07-05T00:00:00Z' },
        { number: 12, labels: [{ name: 'stage:build' }, { name: 'priority:critical' }], createdAt: '2026-07-02T00:00:00Z' },
      ]),
      [commentsKey(12)]: JSON.stringify([
        { body: 'sdlc:claim run-a build', createdAt: '2026-07-09T12:00:00Z' },
      ]),
    });
    const git = fakeExec({ 'rev-parse --abbrev-ref HEAD': 'feat/12\n' });
    const logs = [];
    runSdlc(['claim', '--next', 'build', 'run-a'], { gh, git, log: (m) => logs.push(m) });

    // #12 is critical and oldest of the two criticals → picked first.
    assert.ok(gh.calls.some((c) => JSON.stringify(c) === JSON.stringify(['issue', 'edit', '12', '--add-label', WIP_LABEL])));
    assert.ok(logs.join('\n').includes('claimed #12'));
    assert.notEqual(process.exitCode, 1);
  });

  it('retries the next eligible item after a lost race', () => {
    const gh = fakeExec({
      [LANES_KEY]: JSON.stringify([
        { number: 20, labels: [{ name: 'stage:build' }], createdAt: '2026-07-01T00:00:00Z' },
        { number: 21, labels: [{ name: 'stage:build' }], createdAt: '2026-07-02T00:00:00Z' },
      ]),
      // #20 was claimed earlier by run-b → run-a loses; #21 is uncontested.
      [commentsKey(20)]: JSON.stringify([
        { body: 'sdlc:claim run-b build', createdAt: '2026-07-09T11:00:00Z' },
        { body: 'sdlc:claim run-a build', createdAt: '2026-07-09T12:00:00Z' },
      ]),
      [commentsKey(21)]: JSON.stringify([
        { body: 'sdlc:claim run-a build', createdAt: '2026-07-09T12:00:00Z' },
      ]),
    });
    const git = fakeExec({ 'rev-parse --abbrev-ref HEAD': 'feat/21\n' });
    const logs = [];
    runSdlc(['claim', '--next', 'build', 'run-a'], { gh, git, log: (m) => logs.push(m) });

    assert.ok(logs.join('\n').includes('LOST race on #20'));
    assert.ok(logs.join('\n').includes('claimed #21'));
  });

  it('skips an issue with an OPEN native blocker even when it ranks first (#27)', () => {
    const gh = fakeExec({
      [LANES_KEY]: JSON.stringify([
        { number: 30, labels: [{ name: 'stage:build' }, { name: 'priority:critical' }], createdAt: '2026-07-01T00:00:00Z' },
        { number: 31, labels: [{ name: 'stage:build' }], createdAt: '2026-07-02T00:00:00Z' },
      ]),
      [graphqlKey('OPEN')]: graphqlPage([
        { number: 30, blockedBy: [{ number: 29, state: 'OPEN' }] },
        { number: 31 },
      ]),
      [commentsKey(31)]: JSON.stringify([{ body: 'sdlc:claim run-a build', createdAt: '2026-07-09T12:00:00Z' }]),
    });
    const git = fakeExec({ 'rev-parse --abbrev-ref HEAD': 'feat/31\n' });
    const logs = [];
    runSdlc(['claim', '--next', 'build', 'run-a'], { gh, git, log: (m) => logs.push(m) });
    assert.equal(gh.calls.some((c) => c[0] === 'issue' && c[1] === 'edit' && c[2] === '30'), false);
    assert.ok(logs.join('\n').includes('claimed #31'));
  });

  it('degrades to the label-only gate, loudly, when the edge query fails', () => {
    const gh = fakeExec({
      [LANES_KEY]: JSON.stringify([
        { number: 30, labels: [{ name: 'stage:build' }], createdAt: '2026-07-01T00:00:00Z' },
      ]),
      // no graphql response → JSON parse failure → degrade
      [commentsKey(30)]: JSON.stringify([{ body: 'sdlc:claim run-a build', createdAt: '2026-07-09T12:00:00Z' }]),
    });
    const git = fakeExec({ 'rev-parse --abbrev-ref HEAD': 'feat/30\n' });
    const logs = [];
    runSdlc(['claim', '--next', 'build', 'run-a'], { gh, git, log: (m) => logs.push(m) });
    assert.ok(logs.join('\n').includes('deps: edge query FAILED'));
    assert.ok(logs.join('\n').includes('claimed #30'));
  });

  it('exits 1 with idle on an empty lane', () => {
    const prev = process.exitCode;
    const gh = fakeExec({ [LANES_KEY]: JSON.stringify([]) });
    const logs = [];
    runSdlc(['claim', '--next', 'build', 'run-a'], { gh, git: fakeExec(), log: (m) => logs.push(m) });

    assert.ok(logs.join('\n').includes('idle'));
    assert.equal(process.exitCode, 1);
    process.exitCode = prev; // don't leak into the test runner's exit
  });

  it('rejects an unknown lane and requires a run-id', () => {
    throwsSdlc(
      () => runSdlc(['claim', '--next', 'nonsense', 'run-a'], { gh: fakeExec(), git: fakeExec(), log: () => {} }),
      /unknown lane/,
    );
    throwsSdlc(
      () => runSdlc(['claim', '--next', 'build'], { gh: fakeExec(), git: fakeExec(), log: () => {} }),
      /run-id/,
    );
  });
});

describe('mintRunId', () => {
  it('formats dispatch-<yyyymmdd>-<hhmm>-<hex4> from a fixed clock + hex', () => {
    const now = new Date(Date.UTC(2026, 6, 10, 1, 22));
    assert.equal(mintRunId(now, '46d3'), 'dispatch-20260710-0122-46d3');
  });

  it('zero-pads month, day, hour, and minute', () => {
    const now = new Date(Date.UTC(2026, 0, 3, 4, 5));
    assert.equal(mintRunId(now, 'abcd'), 'dispatch-20260103-0405-abcd');
  });

  it('mints a 4-hex-char suffix by default', () => {
    assert.match(mintRunId(new Date(Date.UTC(2026, 6, 10, 1, 22))), /^dispatch-20260710-0122-[0-9a-f]{4}$/);
  });
});

describe('branch-prune helpers', () => {
  it('planBranchPrune excludes the default branch, the prod branch, and current', () => {
    const merged = [`  ${DEFAULT_BRANCH}`, '* feat/615-x', '  feat/600-done', `  ${PROD_BRANCH}`, '  chore/1'];
    assert.deepEqual(planBranchPrune(merged, 'feat/615-x'), ['feat/600-done', 'chore/1']);
  });

  it('planBranchPrune excludes `+ ` worktree-held branches, never captures the marker', () => {
    // `git branch` prefixes a linked-worktree branch with `+ ` — off-limits to
    // `git branch -D`, and the marker must not leak into the branch name.
    const merged = [`  ${DEFAULT_BRANCH}`, '* feat/615-x', '+ docs/held-elsewhere', '  feat/600-done'];
    assert.deepEqual(planBranchPrune(merged, 'feat/615-x'), ['feat/600-done']);
  });

  it('parseGoneBranches finds [gone] upstreams, skipping current/default/prod', () => {
    const out = [
      '* feat/615-x   abc1234 [origin/feat/615-x] wip',
      '  feat/600-done def5678 [origin/feat/600-done: gone] done',
      `  ${DEFAULT_BRANCH}          aaa1111 [origin/${DEFAULT_BRANCH}] base`,
      `  ${PROD_BRANCH}       bbb2222 [origin/${PROD_BRANCH}: gone] x`,
    ].join('\n');
    assert.deepEqual(parseGoneBranches(out, 'feat/615-x'), ['feat/600-done']);
  });

  it('parseGoneBranches skips `+ ` worktree-held branches even when upstream is gone', () => {
    const out = [
      '* feat/615-x            abc1234 [origin/feat/615-x] wip',
      '+ docs/held-elsewhere   ccc3333 [origin/docs/held-elsewhere: gone] wt',
      '  feat/600-done         def5678 [origin/feat/600-done: gone] done',
    ].join('\n');
    assert.deepEqual(parseGoneBranches(out, 'feat/615-x'), ['feat/600-done']);
  });
});

describe('worktree-sweep helpers', () => {
  const porcelain = [
    'worktree /x/example-repo',
    'HEAD aaa',
    `branch refs/heads/${DEFAULT_BRANCH}`,
    '',
    'worktree /x/example-repo-wt-500',
    'HEAD bbb',
    'branch refs/heads/feat/500',
    '',
    'worktree /x/example-repo-wt-641',
    'HEAD ccc',
    'detached',
    '',
    'worktree /x/my-scratch-tree',
    'HEAD ddd',
    'branch refs/heads/wip/scratch',
  ].join('\n');

  it('parseWorktrees keeps only worktreeName-pattern trees, extracting issue + branch', () => {
    const trees = parseWorktrees(porcelain, 'example-repo');
    assert.deepEqual(trees, [
      { path: '/x/example-repo-wt-500', branch: 'feat/500', issue: 500 },
      { path: '/x/example-repo-wt-641', branch: null, issue: 641 },
    ]);
  });

  it('parseWorktrees ignores the main checkout and human-named worktrees', () => {
    const paths = parseWorktrees(porcelain, 'example-repo').map((t) => t.path);
    assert.ok(!paths.includes('/x/example-repo'));
    assert.ok(!paths.includes('/x/my-scratch-tree'));
  });

  it('parseWorktrees returns [] on empty input', () => {
    assert.deepEqual(parseWorktrees('', 'example-repo'), []);
  });

  it('planWorktreeSweep removes clean + done, leaves active/dirty', () => {
    const trees = [
      { path: 'a', branch: 'feat/1', issue: 1, clean: true, branchGone: false, issueClosed: true },
      { path: 'b', branch: 'feat/2', issue: 2, clean: true, branchGone: true, issueClosed: false },
      { path: 'c', branch: 'feat/3', issue: 3, clean: false, branchGone: true, issueClosed: true },
      { path: 'd', branch: 'feat/4', issue: 4, clean: true, branchGone: false, issueClosed: false },
    ];
    const { remove, leave } = planWorktreeSweep(trees);
    assert.deepEqual(remove.map((t) => t.issue), [1, 2]);
    assert.deepEqual(leave.map((t) => t.issue), [3, 4]);
    assert.match(leave.find((t) => t.issue === 3).reason, /dirty/);
    assert.match(leave.find((t) => t.issue === 4).reason, /active/);
  });

  it('planWorktreeSweep never removes a dirty tree even when fully done', () => {
    const { remove, leave } = planWorktreeSweep([
      { path: 'x', branch: 'feat/9', issue: 9, clean: false, branchGone: true, issueClosed: true },
    ]);
    assert.deepEqual(remove, []);
    assert.equal(leave.length, 1);
  });

  it('planWorktreeSweep removes a done tree that is clean-modulo-strays', () => {
    const { remove, leave } = planWorktreeSweep([
      { path: 's', branch: 'feat/5', issue: 5, clean: true, strays: ['stray'], branchGone: true, issueClosed: true },
    ]);
    assert.deepEqual(remove.map((t) => t.issue), [5]);
    assert.deepEqual(remove[0].strays, ['stray']);
    assert.deepEqual(leave, []);
  });
});

describe('unlinkWorktreeRootLinks (#723 — junction-safe sweep)', () => {
  /** Make a scratch area with a link target holding a canary file. */
  function makeScratch() {
    const base = fs.mkdtempSync(path.join(os.tmpdir(), 'sdlc-wt-links-'));
    const target = path.join(base, 'shared-target');
    fs.mkdirSync(path.join(target, 'sub'), { recursive: true });
    fs.writeFileSync(path.join(target, 'canary.txt'), 'canary');
    fs.writeFileSync(path.join(target, 'sub', 'deep.txt'), 'deep');
    const tree = path.join(base, 'wt');
    fs.mkdirSync(tree);
    return { base, target, tree };
  }

  it('unlinks a root node_modules junction without touching its target', () => {
    const { base, target, tree } = makeScratch();
    try {
      // 'junction' type is honored on Windows (no admin needed) and ignored
      // elsewhere, where a plain directory symlink is created instead.
      fs.symlinkSync(target, path.join(tree, 'node_modules'), 'junction');
      fs.writeFileSync(path.join(tree, 'regular.txt'), 'keep');

      const removed = unlinkWorktreeRootLinks(tree);

      assert.deepEqual(removed, ['node_modules']);
      assert.equal(fs.existsSync(path.join(tree, 'node_modules')), false);
      assert.equal(fs.existsSync(path.join(tree, 'regular.txt')), true);
      // The whole point: the shared target survives intact.
      assert.equal(fs.readFileSync(path.join(target, 'canary.txt'), 'utf8'), 'canary');
      assert.equal(fs.readFileSync(path.join(target, 'sub', 'deep.txt'), 'utf8'), 'deep');
    } finally {
      fs.rmSync(base, { recursive: true, force: true });
    }
  });

  it('leaves plain files and directories alone, returns [] when no links', () => {
    const { base, tree } = makeScratch();
    try {
      fs.mkdirSync(path.join(tree, 'real-dir'));
      fs.writeFileSync(path.join(tree, 'file.txt'), 'x');
      assert.deepEqual(unlinkWorktreeRootLinks(tree), []);
      assert.equal(fs.existsSync(path.join(tree, 'real-dir')), true);
      assert.equal(fs.existsSync(path.join(tree, 'file.txt')), true);
    } finally {
      fs.rmSync(base, { recursive: true, force: true });
    }
  });

  it('returns [] for a missing tree path', () => {
    assert.deepEqual(unlinkWorktreeRootLinks(path.join(os.tmpdir(), 'sdlc-no-such-tree-xyz')), []);
  });
});

describe('linkWorktreeNodeModules (#17 — creation half of the junction convention)', () => {
  function makeScratch({ withSource = true } = {}) {
    const base = fs.mkdtempSync(path.join(os.tmpdir(), 'sdlc-wt-link-'));
    const root = path.join(base, 'main');
    fs.mkdirSync(root);
    if (withSource) {
      fs.mkdirSync(path.join(root, 'node_modules', '.bin'), { recursive: true });
      fs.writeFileSync(path.join(root, 'node_modules', 'canary.txt'), 'canary');
    }
    const tree = path.join(base, 'wt');
    fs.mkdirSync(tree);
    return { base, root, tree };
  }

  it('links node_modules to the main checkout install and reports linked', () => {
    const { base, root, tree } = makeScratch();
    try {
      const logs = [];
      assert.equal(linkWorktreeNodeModules(root, tree, (m) => logs.push(m)), 'linked');
      const link = path.join(tree, 'node_modules');
      assert.equal(fs.lstatSync(link).isSymbolicLink(), true);
      assert.equal(fs.readFileSync(path.join(link, 'canary.txt'), 'utf8'), 'canary');
      assert.ok(logs.some((m) => m.includes('junction ->')));
      // Round-trip with the teardown twin: unlink removes the link, not the target.
      assert.deepEqual(unlinkWorktreeRootLinks(tree), ['node_modules']);
      assert.equal(fs.existsSync(path.join(root, 'node_modules', 'canary.txt')), true);
    } finally {
      fs.rmSync(base, { recursive: true, force: true });
    }
  });

  it('reports no-source (and creates nothing) when the main checkout has no node_modules', () => {
    const { base, root, tree } = makeScratch({ withSource: false });
    try {
      assert.equal(linkWorktreeNodeModules(root, tree), 'no-source');
      assert.throws(() => fs.lstatSync(path.join(tree, 'node_modules')));
    } finally {
      fs.rmSync(base, { recursive: true, force: true });
    }
  });

  it('leaves a real node_modules directory alone and reports exists', () => {
    const { base, root, tree } = makeScratch();
    try {
      fs.mkdirSync(path.join(tree, 'node_modules'));
      fs.writeFileSync(path.join(tree, 'node_modules', 'own.txt'), 'own');
      assert.equal(linkWorktreeNodeModules(root, tree), 'exists');
      assert.equal(fs.lstatSync(path.join(tree, 'node_modules')).isSymbolicLink(), false);
      assert.equal(fs.existsSync(path.join(tree, 'node_modules', 'own.txt')), true);
    } finally {
      fs.rmSync(base, { recursive: true, force: true });
    }
  });

  it('regression: a dangling junction counts as present (lstat, not existsSync)', () => {
    const { base, root, tree } = makeScratch();
    try {
      const gone = path.join(base, 'deleted-install');
      fs.mkdirSync(gone);
      fs.symlinkSync(gone, path.join(tree, 'node_modules'), 'junction');
      fs.rmSync(gone, { recursive: true, force: true });
      assert.equal(fs.existsSync(path.join(tree, 'node_modules')), false); // the trap
      // Must not throw EEXIST and must not replace the dangling link.
      assert.equal(linkWorktreeNodeModules(root, tree), 'exists');
      assert.equal(fs.lstatSync(path.join(tree, 'node_modules')).isSymbolicLink(), true);
    } finally {
      fs.rmSync(base, { recursive: true, force: true });
    }
  });

  it('reports error without throwing when the link cannot be created', () => {
    const { base, root } = makeScratch();
    try {
      const missingTree = path.join(base, 'no-such-wt');
      const logs = [];
      assert.equal(linkWorktreeNodeModules(root, missingTree, (m) => logs.push(m)), 'error');
      assert.ok(logs.some((m) => m.includes('could not link')));
    } finally {
      fs.rmSync(base, { recursive: true, force: true });
    }
  });
});

describe('classifyStatusForSweep', () => {
  it('empty status → clean, no strays', () => {
    assert.deepEqual(classifyStatusForSweep('', () => 0), { clean: true, strays: [] });
  });

  it('only 0-byte untracked strays → clean-modulo-strays', () => {
    const r = classifyStatusForSweep('?? stray\n?? stray)\n', () => 0);
    assert.equal(r.clean, true);
    assert.deepEqual(r.strays, ['stray', 'stray)']);
  });

  it('an untracked file with content → dirty, no strays', () => {
    assert.deepEqual(classifyStatusForSweep('?? notes.txt\n', () => 42), { clean: false, strays: [] });
  });

  it('a tracked modification → dirty even alongside 0-byte strays', () => {
    assert.deepEqual(classifyStatusForSweep(' M src/x.js\n?? stray\n', () => 0), { clean: false, strays: [] });
  });

  it('an unreadable stray path (sizeOf throws) → dirty', () => {
    const r = classifyStatusForSweep('?? stray\n', () => {
      throw new Error('ENOENT');
    });
    assert.deepEqual(r, { clean: false, strays: [] });
  });

  it('a quoted special-char path → dirty (not cleared)', () => {
    assert.deepEqual(classifyStatusForSweep('?? "wë ird"\n', () => 0), { clean: false, strays: [] });
  });
});

describe('runSdlc worktree-sweep', () => {
  const porcelain = [
    'worktree /x/example-repo',
    'HEAD aaa',
    `branch refs/heads/${DEFAULT_BRANCH}`,
    '',
    'worktree /x/example-repo-wt-500',
    'HEAD bbb',
    'branch refs/heads/feat/500',
    '',
    'worktree /x/example-repo-wt-641',
    'HEAD ccc',
    'branch refs/heads/feat/641',
  ].join('\n');

  /** Exec fake that throws for the given arg-join keys (simulates git nonzero exit). */
  function exec(responses = {}, throwKeys = []) {
    const calls = [];
    const fn = (args) => {
      calls.push(args);
      const key = args.join(' ');
      if (throwKeys.includes(key)) throw new Error(`nonzero: ${key}`);
      return responses[key] ?? '';
    };
    fn.calls = calls;
    return fn;
  }

  it('removes a clean closed-issue worktree, leaves an active one (with --apply)', () => {
    const git = exec(
      { 'worktree list --porcelain': porcelain },
      [
        `merge-base --is-ancestor feat/500 origin/${DEFAULT_BRANCH}`,
        `merge-base --is-ancestor feat/641 origin/${DEFAULT_BRANCH}`,
      ],
    );
    const gh = exec({
      'pr list --head feat/500 --state merged --json number': '[]',
      'pr list --head feat/641 --state merged --json number': '[]',
      'issue view 500 --json state': '{"state":"CLOSED"}',
      'issue view 641 --json state': '{"state":"OPEN"}',
    });
    const logs = [];
    runSdlc(['worktree-sweep', '--apply'], { git, gh, log: (m) => logs.push(m), root: '/x/example-repo' });
    const out = logs.join('\n');
    assert.ok(out.includes('REMOVE /x/example-repo-wt-500 (#500)'));
    assert.ok(out.includes('LEAVE /x/example-repo-wt-641 (#641)'));
    assert.ok(git.calls.some((c) => JSON.stringify(c) === JSON.stringify(['worktree', 'remove', '/x/example-repo-wt-500'])));
    assert.equal(git.calls.some((c) => c[0] === 'worktree' && c[1] === 'remove' && c[2] === '/x/example-repo-wt-641'), false);
    assert.ok(git.calls.some((c) => JSON.stringify(c) === JSON.stringify(['worktree', 'prune'])));
  });

  it('without --apply, reports the plan and removes nothing', () => {
    const git = exec(
      { 'worktree list --porcelain': porcelain },
      [
        `merge-base --is-ancestor feat/500 origin/${DEFAULT_BRANCH}`,
        `merge-base --is-ancestor feat/641 origin/${DEFAULT_BRANCH}`,
      ],
    );
    const gh = exec({
      'pr list --head feat/500 --state merged --json number': '[]',
      'pr list --head feat/641 --state merged --json number': '[]',
      'issue view 500 --json state': '{"state":"CLOSED"}',
      'issue view 641 --json state': '{"state":"OPEN"}',
    });
    const logs = [];
    runSdlc(['worktree-sweep'], { git, gh, log: (m) => logs.push(m), root: '/x/example-repo' });
    assert.ok(logs.join('\n').includes('run with --apply'));
    assert.equal(git.calls.some((c) => c[0] === 'worktree' && c[1] === 'remove'), false);
  });

  it('clears a 0-byte untracked stray on a done tree, then removes it (--apply)', () => {
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'sdlc-sweep-'));
    const wtPath = path.join(tmp, 'example-repo-wt-500');
    fs.mkdirSync(wtPath);
    const strayPath = path.join(wtPath, 'stray');
    fs.writeFileSync(strayPath, ''); // 0-byte stray
    try {
      const listPorcelain = [
        `worktree ${tmp}/example-repo`,
        'HEAD aaa',
        `branch refs/heads/${DEFAULT_BRANCH}`,
        '',
        `worktree ${wtPath}`,
        'HEAD bbb',
        'branch refs/heads/feat/500',
      ].join('\n');
      const git = exec(
        {
          'worktree list --porcelain': listPorcelain,
          [`-C ${wtPath} status --porcelain`]: '?? stray\n',
        },
        [`merge-base --is-ancestor feat/500 origin/${DEFAULT_BRANCH}`],
      );
      const gh = exec({
        'pr list --head feat/500 --state merged --json number': '[]',
        'issue view 500 --json state': '{"state":"CLOSED"}',
      });
      const logs = [];
      runSdlc(['worktree-sweep', '--apply'], {
        git,
        gh,
        log: (m) => logs.push(m),
        root: `${tmp}/example-repo`,
      });
      const out = logs.join('\n');
      assert.ok(out.includes(`REMOVE ${wtPath} (#500)`));
      assert.ok(out.includes('cleared 1 0-byte stray'));
      assert.equal(fs.existsSync(strayPath), false);
      assert.ok(git.calls.some((c) => JSON.stringify(c) === JSON.stringify(['worktree', 'remove', wtPath])));
    } finally {
      fs.rmSync(tmp, { recursive: true, force: true });
    }
  });

  it('leaves a done tree whose untracked stray has content (>0 bytes)', () => {
    const git = exec(
      {
        'worktree list --porcelain': porcelain,
        '-C /x/example-repo-wt-641 status --porcelain': '?? notes.txt\n',
        '-C /x/example-repo-wt-500 status --porcelain': '?? notes.txt\n',
      },
      [
        `merge-base --is-ancestor feat/500 origin/${DEFAULT_BRANCH}`,
        `merge-base --is-ancestor feat/641 origin/${DEFAULT_BRANCH}`,
      ],
    );
    const gh = exec({
      'pr list --head feat/500 --state merged --json number': '[]',
      'pr list --head feat/641 --state merged --json number': '[]',
      'issue view 500 --json state': '{"state":"CLOSED"}',
      'issue view 641 --json state': '{"state":"CLOSED"}',
    });
    const logs = [];
    // statSize reports content → not a stray → dirty → left.
    runSdlc(['worktree-sweep', '--apply'], {
      git,
      gh,
      log: (m) => logs.push(m),
      root: '/x/example-repo',
      statSize: () => 42,
    });
    const out = logs.join('\n');
    assert.match(out, /LEAVE \/x\/example-repo-wt-500 \(#500\).*dirty/);
    assert.equal(git.calls.some((c) => c[1] === 'remove'), false);
  });
});

describe('computeDigest', () => {
  const issues = [
    { number: 20, labels: ['stage:queued'] },
    { number: 21, labels: ['stage:build'] },
    { number: 22, labels: ['stage:build', 'sdlc:needs-human'] },
    { number: 23, labels: ['stage:design', 'sdlc:hold'] },
  ];

  it('tallies depths and parked/hold lists', () => {
    const d = computeDigest(issues);
    assert.equal(d.depths.build, 2);
    assert.equal(d.depths.queued, 1);
    assert.deepEqual(d.parked, [22]);
    assert.deepEqual(d.hold, [23]);
  });

  it('diffs arrivals and departures against a prior snapshot', () => {
    const d = computeDigest(issues, [20, 21, 99]);
    assert.deepEqual(d.arrivals, [22, 23]);
    assert.deepEqual(d.departures, [99]);
  });

  it('reports no diff when no prior snapshot given', () => {
    assert.equal(computeDigest(issues).arrivals, null);
  });

  it('reports parked/hold deltas as unchanged when the prior lists match', () => {
    const d = computeDigest(issues, { current: [20, 21, 22, 23], parked: [22], hold: [23] });
    assert.deepEqual(d.parkedDelta, { added: [], removed: [], changed: false });
    assert.deepEqual(d.holdDelta, { added: [], removed: [], changed: false });
  });

  it('reports parked/hold additions and removals against the prior lists', () => {
    const d = computeDigest(issues, { current: [21, 22, 99], parked: [99], hold: [] });
    assert.deepEqual(d.parkedDelta, { added: [22], removed: [99], changed: true });
    assert.deepEqual(d.holdDelta, { added: [23], removed: [], changed: true });
  });

  it('treats a legacy array prev as current-numbers-only (no parked/hold delta)', () => {
    const d = computeDigest(issues, [20, 21, 99]);
    assert.equal(d.parkedDelta, null);
    assert.equal(d.holdDelta, null);
    assert.deepEqual(d.arrivals, [22, 23]);
  });
});

describe('computeSweep (edge-based, #27)', () => {
  // The issue's demo graph: B←A, C←A, C←B. Prose in bodies is deliberately
  // misleading and must be ignored.
  const A = 10;
  const B = 30;
  const C = 31;
  const openAfterA = [
    { number: B, title: 'B', labels: [BLOCKED_LABEL], blockedBy: [{ number: A, state: 'CLOSED' }] },
    { number: C, title: 'C', labels: [BLOCKED_LABEL], blockedBy: [{ number: A, state: 'CLOSED' }, { number: B, state: 'OPEN' }] },
    { number: 32, title: 'unrelated', labels: [], body: 'None depends on #10' },
  ];
  const closedA = [
    { number: A, closedAt: '2026-07-10T00:00:00Z', title: 'A', blocking: [{ number: B, state: 'OPEN' }, { number: C, state: 'OPEN' }] },
  ];

  it('closed issue → its OPEN blocking targets; flips only those with no remaining open blocker', () => {
    const { items, closedIssues, empty } = computeSweep(closedA, openAfterA);
    assert.equal(empty, false);
    assert.deepEqual(closedIssues, [A]);
    assert.equal(items.length, 1);
    assert.equal(items[0].number, A);
    assert.deepEqual(
      items[0].dependents.map((d) => ({ number: d.number, blocked: d.blocked, flip: d.flip, remaining: d.remainingBlockers })),
      [
        { number: B, blocked: true, flip: true, remaining: [] },
        { number: C, blocked: true, flip: false, remaining: [B] },
      ],
    );
    // #32 mentions #10 in prose — no edge, so it is not a dependent.
    assert.ok(!items[0].dependents.some((d) => d.number === 32));
  });

  it('cascades: closing B then flips C', () => {
    const closedB = [{ number: B, closedAt: '2026-07-11T00:00:00Z', title: 'B', blocking: [{ number: C, state: 'OPEN' }] }];
    const openAfterB = [
      { number: C, title: 'C', labels: [BLOCKED_LABEL], blockedBy: [{ number: A, state: 'CLOSED' }, { number: B, state: 'CLOSED' }] },
    ];
    const { items } = computeSweep(closedB, openAfterB);
    assert.deepEqual(items[0].dependents.map((d) => [d.number, d.flip]), [[C, true]]);
  });

  it('a hand-closed issue cascades the same as a PR-closed one (no PR involved)', () => {
    const { items } = computeSweep(
      [{ number: 5, closedAt: '2026-07-10T00:00:00Z', blocking: [{ number: 6, state: 'OPEN' }] }],
      [{ number: 6, blockedBy: [{ number: 5, state: 'CLOSED' }] }],
    );
    assert.deepEqual(items[0].dependents.map((d) => [d.number, d.flip]), [[6, true]]);
  });

  it('is clear when the closed issues blocked nothing open', () => {
    const r = computeSweep(
      [{ number: 7, closedAt: '2026-07-10T00:00:00Z', blocking: [{ number: 8, state: 'CLOSED' }] }],
      [],
    );
    assert.equal(r.empty, true);
    assert.deepEqual(r.items, []);
  });

  it('ignores a blocking target missing from the open snapshot (closed since)', () => {
    const r = computeSweep(closedA, [openAfterA[1]]); // only C in the snapshot
    assert.deepEqual(r.items[0].dependents.map((d) => d.number), [C]);
  });

  it('skips issues already in sweptIssues (idempotent)', () => {
    assert.equal(computeSweep(closedA, openAfterA, { sweptIssues: [A] }).empty, true);
  });

  it('skips issues closed at/before the sinceMs cutoff (bounded window)', () => {
    const cutoff = new Date('2026-07-10T12:00:00Z').getTime();
    assert.equal(computeSweep(closedA, openAfterA, { sinceMs: cutoff }).empty, true);
    const earlier = new Date('2026-07-09T00:00:00Z').getTime();
    assert.equal(computeSweep(closedA, openAfterA, { sinceMs: earlier }).empty, false);
  });

  it('accepts object-form labels and raw GraphQL connections', () => {
    const { items } = computeSweep(
      [{ number: A, closedAt: '2026-07-10T00:00:00Z', blocking: { nodes: [{ number: 40, state: 'OPEN' }] } }],
      [{ number: 40, title: 'x', labels: [{ name: BLOCKED_LABEL }], blockedBy: { nodes: [{ number: A, state: 'CLOSED' }] } }],
    );
    assert.deepEqual(items[0].dependents, [{ number: 40, title: 'x', blocked: true, remainingBlockers: [], flip: true }]);
  });
});

describe('computeSweepAck', () => {
  const closed = [
    { number: 500, closedAt: '2026-07-10T00:00:00Z' },
    { number: 501, closedAt: '2026-07-08T00:00:00Z' },
  ];

  it('marks all in-window closes and reports the newly-acked ones', () => {
    const { nextSwept, newlyAcked } = computeSweepAck(closed, { sweptIssues: [500] });
    assert.deepEqual([...nextSwept].sort(), [500, 501]);
    assert.deepEqual(newlyAcked, [501]);
  });

  it('excludes closes at/before the sinceMs cutoff from the marker', () => {
    const cutoff = new Date('2026-07-09T00:00:00Z').getTime();
    const { nextSwept, newlyAcked } = computeSweepAck(closed, { sinceMs: cutoff });
    assert.deepEqual(nextSwept, [500]);
    assert.deepEqual(newlyAcked, [500]);
  });

  it('drops prior swept issues no longer in the fetched list (bounded marker)', () => {
    const { nextSwept } = computeSweepAck(closed, { sweptIssues: [400, 500] });
    assert.ok(!nextSwept.includes(400));
    assert.deepEqual([...nextSwept].sort(), [500, 501]);
  });

  it('is a no-op ack when everything in the window is already swept', () => {
    const { nextSwept, newlyAcked } = computeSweepAck(closed, { sweptIssues: [500, 501] });
    assert.deepEqual([...nextSwept].sort(), [500, 501]);
    assert.deepEqual(newlyAcked, []);
  });
});

describe('findDependencyCycles', () => {
  it('finds each cycle once, from its lowest member, ignoring closed blockers', () => {
    const cycles = findDependencyCycles([
      { number: 3, blockedBy: [{ number: 9, state: 'OPEN' }] },
      { number: 5, blockedBy: [{ number: 3, state: 'OPEN' }] },
      { number: 9, blockedBy: [{ number: 5, state: 'OPEN' }, { number: 1, state: 'CLOSED' }] },
      { number: 11, blockedBy: [{ number: 11, state: 'OPEN' }] }, // self-loop
      { number: 12, blockedBy: [{ number: 3, state: 'OPEN' }] }, // hangs off the cycle, not in it
    ]);
    assert.deepEqual(cycles, [[3, 9, 5], [11]]);
  });

  it('is empty for a DAG and for blockers outside the snapshot', () => {
    assert.deepEqual(
      findDependencyCycles([
        { number: 1, blockedBy: [] },
        { number: 2, blockedBy: [{ number: 1, state: 'OPEN' }, { number: 99, state: 'OPEN' }] },
      ]),
      [],
    );
  });
});

describe('computeDeps (derived readiness labels + lint)', () => {
  it('derives blocked/ready from edge state and plans only the needed label edits', () => {
    const { edits, findings, blocked, ready } = computeDeps([
      { number: 1, labels: [], blockedBy: [{ number: 9, state: 'OPEN' }] },                  // +blocked
      { number: 2, labels: [READY_LABEL], blockedBy: [{ number: 9, state: 'OPEN' }] },       // +blocked -ready
      { number: 3, labels: [BLOCKED_LABEL], blockedBy: [{ number: 9, state: 'OPEN' }] },     // already right
      { number: 4, labels: [BLOCKED_LABEL], blockedBy: [{ number: 8, state: 'CLOSED' }] },   // +ready -blocked
      { number: 5, labels: [READY_LABEL], blockedBy: [{ number: 8, state: 'CLOSED' }] },     // already right
      { number: 6, labels: [], blockedBy: [] },                                              // no edges: nothing
    ]);
    assert.deepEqual(blocked, [1, 2, 3]);
    assert.deepEqual(ready, [4, 5]);
    assert.deepEqual(
      edits.map((e) => [e.number, e.add, e.remove]),
      [
        [1, [BLOCKED_LABEL], []],
        [2, [BLOCKED_LABEL], [READY_LABEL]],
        [4, [READY_LABEL], [BLOCKED_LABEL]],
      ],
    );
    assert.deepEqual(findings, []);
  });

  it('a blocked label with no edge is ambiguous — lint, never auto-repaired', () => {
    const { edits, findings } = computeDeps([{ number: 7, labels: [BLOCKED_LABEL], blockedBy: [] }]);
    assert.deepEqual(edits, []);
    assert.equal(findings.length, 1);
    assert.equal(findings[0].kind, 'label-only-blocked');
    assert.equal(findings[0].number, 7);
  });

  it('reports dependency cycles', () => {
    const { findings } = computeDeps([
      { number: 1, labels: [], blockedBy: [{ number: 2, state: 'OPEN' }] },
      { number: 2, labels: [], blockedBy: [{ number: 1, state: 'OPEN' }] },
    ]);
    assert.deepEqual(findings.map((f) => f.kind), ['cycle']);
    assert.match(findings[0].detail, /#1 ← #2 ← #1/);
  });
});

describe('parseProseDependencies (migration parser)', () => {
  it('matches only line-leading declarations, with list/emphasis/colon decoration', () => {
    const body = [
      'Depends on #12',
      '- Blocked by: #13, #14 and #15',
      '* **Requires** #16',
      '1. after #17 lands',
      '> depends on #18', // blockquote — not a declaration
      'None depends on #1337',
      'This does not depend on #1161 at all.',
      'Requires #12 again (dup)',
      'Blocked by #99 — self',
      '**Dependencies:** Depends on K.3b #19', // roadmap-identifier form
      '**Dependencies:** ~~Depends on K.3a #20~~ ✅ K.3a complete', // struck through — still an edge (closed blocker)
    ].join('\n');
    assert.deepEqual(parseProseDependencies(body, 99), [12, 13, 14, 15, 16, 17, 19, 20]);
  });

  it('takes only the leading run of references on a line', () => {
    assert.deepEqual(parseProseDependencies('Depends on #3 and the API rework in #4'), [3]);
    assert.deepEqual(parseProseDependencies('Blocked by #3 / #4'), [3, 4]);
  });

  it('is empty for no body or no declarations', () => {
    assert.deepEqual(parseProseDependencies(null), []);
    assert.deepEqual(parseProseDependencies('see #5 for context'), []);
  });
});

describe('planDependencyMigration', () => {
  it('proposes edges for known blockers not already native; skips unknown numbers', () => {
    const { proposals, skipped } = planDependencyMigration(
      [
        { number: 20, body: 'Depends on #10, #11, #12', blockedBy: [{ number: 11, state: 'OPEN' }] },
        { number: 21, body: 'Blocked by #500', blockedBy: [] },
      ],
      { 10: { id: 1000, state: 'CLOSED' }, 11: { id: 1100, state: 'OPEN' }, 12: { id: 1200, state: 'OPEN' } },
    );
    assert.deepEqual(proposals, [
      { number: 20, blocker: 10, blockerState: 'CLOSED', blockerId: 1000 },
      { number: 20, blocker: 12, blockerState: 'OPEN', blockerId: 1200 },
    ]);
    assert.equal(skipped.length, 1);
    assert.equal(skipped[0].blocker, 500);
  });
});

describe('runSdlc deps', () => {
  const openGraph = () => graphqlPage([
    { number: 1, labels: ['stage:build'], blockedBy: [{ number: 9, state: 'OPEN' }] },
    { number: 4, labels: ['stage:queued', BLOCKED_LABEL], blockedBy: [{ number: 8, state: 'CLOSED' }] },
    { number: 7, labels: ['stage:queued', BLOCKED_LABEL], blockedBy: [] },
    { number: 9, labels: ['stage:build'] },
  ]);

  it('previews derived-label edits + lint without writing', () => {
    const gh = fakeExec({ [graphqlKey('OPEN')]: openGraph() });
    const logs = [];
    runSdlc(['deps'], { gh, git: fakeExec(), log: (m) => logs.push(m) });
    const out = logs.join('\n');
    assert.ok(out.includes('deps: 1 blocked (#1), 1 ready (#4)'));
    assert.ok(out.includes(`would relabel #1 +${BLOCKED_LABEL}`));
    assert.ok(out.includes(`would relabel #4 +${READY_LABEL} -${BLOCKED_LABEL}`));
    assert.ok(out.includes('LINT label-only-blocked #7'));
    assert.equal(gh.calls.some((c) => c[0] === 'issue' && c[1] === 'edit'), false);
  });

  it('--apply writes exactly the planned label edits', () => {
    const gh = fakeExec({ [graphqlKey('OPEN')]: openGraph() });
    runSdlc(['deps', '--apply'], { gh, git: fakeExec(), log: () => {} });
    const edits = gh.calls.filter((c) => c[0] === 'issue' && c[1] === 'edit').map((c) => c.join(' '));
    assert.deepEqual(edits, [
      `issue edit 1 --add-label ${BLOCKED_LABEL}`,
      `issue edit 4 --remove-label ${BLOCKED_LABEL} --add-label ${READY_LABEL}`,
    ]);
  });

  it('--migrate proposes edges from prose (dry run) and --apply POSTs them by databaseId', () => {
    const gh = fakeExec({
      [graphqlKey('OPEN')]: graphqlPage([
        { number: 20, id: 2000, body: 'Depends on #10 and #21\nBlocked by #1337\nNone depends on #4242', blockedBy: [] },
        { number: 21, id: 2100, body: '' },
      ]),
      // A closed blocker is resolved by one REST lookup, not by paging the closed backlog.
      'api repos/{owner}/{repo}/issues/10 --jq {id: .id, state: .state, pull_request: (.pull_request != null)}':
        JSON.stringify({ id: 1000, state: 'closed', pull_request: false }),
      // #1337 gets no canned response → parse failure → unknown → skipped.
    });
    const logs = [];
    runSdlc(['deps', '--migrate'], { gh, git: fakeExec(), log: (m) => logs.push(m) });
    const out = logs.join('\n');
    // Bodies are requested only by the migration; the OPEN query for it carries `body`.
    const graphqlCalls = gh.calls.filter((c) => c[0] === 'api' && c[1] === 'graphql');
    assert.ok(graphqlCalls.every((c) => c.some((a) => /^query=/.test(a) && a.includes(' body'))));
    assert.ok(!gh.calls.some((c) => c.includes('states=CLOSED')));
    assert.ok(out.includes('#20 blocked_by #10 (CLOSED)'));
    assert.ok(out.includes('#20 blocked_by #21 (OPEN)'));
    assert.ok(out.includes('skipped #20 → #1337'));
    assert.ok(!out.includes('4242')); // narrative mention — not a declaration, not even a skip
    assert.ok(out.includes('dry run'));
    assert.equal(gh.calls.some((c) => c.includes('POST')), false);

    runSdlc(['deps', '--migrate', '--apply'], { gh, git: fakeExec(), log: () => {} });
    const posts = gh.calls.filter((c) => c.includes('POST')).map((c) => c.join(' '));
    assert.deepEqual(posts, [
      'api -X POST repos/{owner}/{repo}/issues/20/dependencies/blocked_by -F issue_id=1000',
      'api -X POST repos/{owner}/{repo}/issues/20/dependencies/blocked_by -F issue_id=2100',
    ]);
  });
});

describe('runSdlc sweep (edge-based)', () => {
  const closedGraph = graphqlPage([
    { number: 10, title: 'A', state: 'CLOSED', closedAt: '2026-07-10T00:00:00Z', blocking: [{ number: 30, state: 'OPEN' }, { number: 31, state: 'OPEN' }] },
  ]);
  const openGraph = graphqlPage([
    { number: 30, title: 'B', labels: [BLOCKED_LABEL], blockedBy: [{ number: 10, state: 'CLOSED' }] },
    { number: 31, title: 'C', labels: [BLOCKED_LABEL], blockedBy: [{ number: 10, state: 'CLOSED' }, { number: 30, state: 'OPEN' }] },
  ]);

  it('lists closed issues → dependents with flip/still-blocked verdicts; --ack persists by issue number', () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'sdlc-sweep-test-'));
    const statePath = path.join(dir, 'last-sweep.json');
    try {
      const gh = fakeExec({ [graphqlKey('CLOSED')]: closedGraph, [graphqlKey('OPEN')]: openGraph });
      const logs = [];
      // No window: the canned close is in the past.
      runSdlc(['sweep', '--state', statePath, '--window', 'x'], { gh, git: fakeExec(), log: (m) => logs.push(m) });
      const out = logs.join('\n');
      assert.ok(out.includes('closed #10 (2026-07-10T00:00:00Z) A'));
      assert.ok(out.includes('dependent #30 [unblocked — blocked → ready] B'));
      assert.ok(out.includes('dependent #31 [still blocked by #30] C'));
      // Read-only: no issue writes; and no bodies requested (a closed backlog's bodies run to MBs).
      assert.equal(gh.calls.some((c) => c[0] === 'issue'), false);
      assert.ok(!gh.calls.some((c) => c.some((a) => /^query=/.test(a) && a.includes(' body'))));

      runSdlc(['sweep', '--ack', '--state', statePath, '--window', 'x'], { gh, git: fakeExec(), log: (m) => logs.push(m) });
      assert.deepEqual(JSON.parse(fs.readFileSync(statePath, 'utf8')).sweptIssues, [10]);
      assert.ok(logs.join('\n').includes('sweep: acked 1 close (#10)'));

      const again = [];
      runSdlc(['sweep', '--state', statePath, '--window', 'x'], { gh, git: fakeExec(), log: (m) => again.push(m) });
      assert.deepEqual(again, ['sweep: clear']);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe('planConflictScan', () => {
  const BASE_TS = '2026-07-05T00:00:00Z';
  const info = (over = {}) => ({ state: 'OPEN', labels: [], comments: [], ...over });

  it('plans a comment (no advance) for a build-stage issue', () => {
    const prs = [
      { number: 90, headRefName: 'feat/1-x', baseRefName: DEFAULT_BRANCH, mergeable: 'CONFLICTING', linkedIssues: [1] },
    ];
    const { actions, skipped } = planConflictScan(prs, { 1: info({ labels: ['stage:build'] }) }, BASE_TS);
    assert.deepEqual(skipped, []);
    assert.equal(actions.length, 1);
    assert.equal(actions[0].pr, 90);
    assert.equal(actions[0].issue, 1);
    assert.equal(actions[0].advanceTo, null);
    assert.equal(actions[0].comment, `sdlc-dispatch: branch feat/1-x conflicts with ${DEFAULT_BRANCH} — needs a merge`);
  });

  it('plans a bounce back to build for verify/audit/ship issues', () => {
    for (const stage of CONFLICT_BOUNCE_STAGES) {
      const prs = [
        { number: 9, headRefName: 'feat/9', baseRefName: DEFAULT_BRANCH, mergeable: 'CONFLICTING', linkedIssues: [9] },
      ];
      const { actions } = planConflictScan(prs, { 9: info({ labels: [`stage:${stage}`] }) }, BASE_TS);
      assert.equal(actions[0].advanceTo, 'build', stage);
      assert.equal(actions[0].stage, stage);
    }
  });

  it('ignores non-conflicting PRs and PRs not targeting the base branch', () => {
    const prs = [
      { number: 1, headRefName: 'a', baseRefName: DEFAULT_BRANCH, mergeable: 'MERGEABLE', linkedIssues: [1] },
      { number: 2, headRefName: 'b', baseRefName: 'some-other-branch', mergeable: 'CONFLICTING', linkedIssues: [2] },
    ];
    const out = planConflictScan(prs, { 1: info(), 2: info() }, BASE_TS);
    assert.deepEqual(out.actions, []);
    assert.deepEqual(out.skipped, []);
  });

  it('excludes wip / needs-human / hold and closed/missing issues', () => {
    const prs = [
      { number: 1, headRefName: 'a', baseRefName: DEFAULT_BRANCH, mergeable: 'CONFLICTING', linkedIssues: [10] },
      { number: 2, headRefName: 'b', baseRefName: DEFAULT_BRANCH, mergeable: 'CONFLICTING', linkedIssues: [11] },
      { number: 3, headRefName: 'c', baseRefName: DEFAULT_BRANCH, mergeable: 'CONFLICTING', linkedIssues: [12] },
      { number: 4, headRefName: 'd', baseRefName: DEFAULT_BRANCH, mergeable: 'CONFLICTING', linkedIssues: [13] },
    ];
    const issueInfo = {
      10: info({ labels: ['stage:build', WIP_LABEL] }),
      11: info({ labels: ['stage:build', 'sdlc:needs-human'] }),
      12: info({ state: 'CLOSED', labels: ['stage:build'] }),
      // 13 intentionally absent (not found)
    };
    const { actions, skipped } = planConflictScan(prs, issueInfo, BASE_TS);
    assert.deepEqual(actions, []);
    assert.deepEqual(skipped.map((s) => s.issue), [10, 11, 12, 13]);
  });

  it('skips a PR with no linked issue', () => {
    const prs = [
      { number: 7, headRefName: 'z', baseRefName: DEFAULT_BRANCH, mergeable: 'CONFLICTING', linkedIssues: [] },
    ];
    const { actions, skipped } = planConflictScan(prs, {}, BASE_TS);
    assert.deepEqual(actions, []);
    assert.deepEqual(skipped, [{ pr: 7, reason: 'no linked issue' }]);
  });

  it('is idempotent: suppresses a comment already posted at/after the base watermark', () => {
    const prs = [
      { number: 1, headRefName: 'feat/1-x', baseRefName: DEFAULT_BRANCH, mergeable: 'CONFLICTING', linkedIssues: [1] },
    ];
    const body = conflictCommentBody('feat/1-x');
    const issueInfo = {
      1: info({ labels: ['stage:build'], comments: [{ body, createdAt: '2026-07-06T00:00:00Z' }] }),
    };
    const { actions, skipped } = planConflictScan(prs, issueInfo, BASE_TS);
    assert.deepEqual(actions, []);
    assert.ok(skipped[0].reason.includes('already posted'));
  });

  it('re-nudges when the prior comment predates the last base-branch update', () => {
    const prs = [
      { number: 1, headRefName: 'feat/1-x', baseRefName: DEFAULT_BRANCH, mergeable: 'CONFLICTING', linkedIssues: [1] },
    ];
    const body = conflictCommentBody('feat/1-x');
    const issueInfo = {
      1: info({ labels: ['stage:build'], comments: [{ body, createdAt: '2026-07-04T00:00:00Z' }] }),
    };
    // watermark compares timestamps, not strings (Z vs +00:00 offsets differ lexically).
    const { actions } = planConflictScan(prs, issueInfo, '2026-07-05T00:00:00+00:00');
    assert.equal(actions.length, 1);
  });
});

describe('dup-check scoring', () => {
  it('tokenizes defensively and drops stopwords / short terms from a query', () => {
    assert.deepEqual(dupTokens('Add a Bottom-Menu bar!'), ['add', 'a', 'bottom', 'menu', 'bar']);
    assert.deepEqual(dupTokens(null), []);
    assert.deepEqual(dupQueryTerms('add a bar to the menu'), ['add', 'bar', 'menu']);
  });

  it('weights title hits over body hits and adds a label-hint bonus', () => {
    const issues = [
      { number: 10, title: 'Bottom menu bar', body: 'unrelated', labels: ['ui'] },
      { number: 11, title: 'unrelated', body: 'a bottom menu bar someday', labels: [] },
      { number: 12, title: 'nothing here', body: 'nothing', labels: [] },
    ];
    const ranked = scoreDupCandidates('bottom menu bar', issues);
    // #10: 3 title terms ×3 = 9; #11: 3 body terms ×1 = 3; #12 drops (score 0).
    assert.deepEqual(ranked.map((c) => c.number), [10, 11]);
    assert.equal(ranked[0].score, 9);
    assert.equal(ranked[1].score, 3);
  });

  it('label token hits add on top of a title/body hit', () => {
    const issues = [{ number: 20, title: 'x', body: 'y', labels: [{ name: 'client' }, 'ui'] }];
    const ranked = scoreDupCandidates('ui', issues);
    assert.equal(ranked[0].score, 1); // label-only hit → labelWeight 1
  });

  it('ranks by score desc then issue number asc, and honours limit', () => {
    const issues = [
      { number: 3, title: 'menu', body: '', labels: [] },
      { number: 1, title: 'menu', body: '', labels: [] },
      { number: 2, title: 'menu bar', body: '', labels: [] },
    ];
    const ranked = scoreDupCandidates('menu bar', issues, { limit: 2 });
    assert.deepEqual(ranked.map((c) => c.number), [2, 1]); // #2 score 6; tie 1<3
  });

  it('excludes the query’s own issue and returns nothing for an all-stopword query', () => {
    const issues = [{ number: 5, title: 'menu bar', body: '', labels: [] }];
    assert.deepEqual(scoreDupCandidates('menu', issues, { exclude: 5 }), []);
    assert.deepEqual(scoreDupCandidates('menu', issues, { exclude: '#5' }), []);
    assert.deepEqual(scoreDupCandidates('the a of', issues), []);
  });
});

describe('sdlc dup-check command', () => {
  const listKey = 'issue list --state open --json number,title,body,labels --limit 200';

  it('prints ranked candidates and sets exit code 2 when any are found', () => {
    const prev = process.exitCode;
    process.exitCode = undefined;
    const gh = fakeExec({
      [listKey]: JSON.stringify([
        { number: 10, title: 'Bottom menu bar', body: '', labels: ['ui'] },
        { number: 11, title: 'unrelated thing', body: '', labels: [] },
      ]),
    });
    const logs = [];
    runSdlc(['dup-check', 'bottom menu bar'], { gh, log: (m) => logs.push(m) });
    assert.ok(logs.join('\n').includes('#10 Bottom menu bar (score'));
    assert.ok(!logs.join('\n').includes('#11'));
    assert.equal(process.exitCode, 2);
    process.exitCode = prev; // don't leak into the runner's exit
  });

  it('reports clean and leaves exit code untouched when nothing matches', () => {
    const prev = process.exitCode;
    process.exitCode = undefined;
    const gh = fakeExec({
      [listKey]: JSON.stringify([{ number: 11, title: 'unrelated thing', body: '', labels: [] }]),
    });
    const logs = [];
    runSdlc(['dup-check', 'bottom menu bar'], { gh, log: (m) => logs.push(m) });
    assert.ok(logs.join('\n').includes('no candidates'));
    assert.notEqual(process.exitCode, 2);
    process.exitCode = prev;
  });

  it('errors on a missing query or an all-stopword query', () => {
    throwsSdlc(() => runSdlc(['dup-check'], { gh: fakeExec(), log: () => {} }), /requires a query/);
    throwsSdlc(() => runSdlc(['dup-check', 'the a of'], { gh: fakeExec(), log: () => {} }), /no usable search terms/);
  });
});

describe('maint-lock / maint-release (filesystem machine lock)', () => {
  /** A throwaway repo root with a .git dir for the lock to live in. */
  const makeRoot = () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'sdlc-maint-test-'));
    fs.mkdirSync(path.join(root, '.git'));
    return root;
  };
  const lockDir = (root) => path.join(root, '.git', 'sdlc-maint.lock');

  it('acquires when free, then a second contender is HELD (exit 1)', () => {
    const root = makeRoot();
    try {
      const logs = [];
      runSdlc(['maint-lock', 'run-a'], { gh: fakeExec(), log: (m) => logs.push(m), root });
      assert.ok(logs.join('\n').includes('maint-lock: ACQUIRED run-a'));
      assert.equal(fs.existsSync(lockDir(root)), true);

      const prevExit = process.exitCode;
      const logs2 = [];
      runSdlc(['maint-lock', 'run-b'], { gh: fakeExec(), log: (m) => logs2.push(m), root });
      assert.ok(logs2.join('\n').includes('HELD by run-a'));
      assert.equal(process.exitCode, 1);
      process.exitCode = prevExit; // don't fail the test run itself
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it('reaps a stale lock (owner stamp past the threshold) and acquires', () => {
    const root = makeRoot();
    try {
      fs.mkdirSync(lockDir(root));
      const old = new Date(Date.now() - MAINT_STALE_MS - 60_000).toISOString();
      fs.writeFileSync(path.join(lockDir(root), 'owner.txt'), `run-dead ${old}\n`);

      const logs = [];
      runSdlc(['maint-lock', 'run-b'], { gh: fakeExec(), log: (m) => logs.push(m), root });
      const out = logs.join('\n');
      assert.ok(out.includes('REAPED stale lock from run-dead'), out);
      assert.ok(out.includes('ACQUIRED run-b'), out);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it('release is idempotent and owner-checked', () => {
    const root = makeRoot();
    try {
      // Releasing a never-taken lock is a successful no-op.
      const logs0 = [];
      runSdlc(['maint-release', 'run-a'], { gh: fakeExec(), log: (m) => logs0.push(m), root });
      assert.ok(logs0.join('\n').includes('already clear'));

      runSdlc(['maint-lock', 'run-a'], { gh: fakeExec(), log: () => {}, root });

      // A different run-id must not release it.
      const prevExit = process.exitCode;
      const logs1 = [];
      runSdlc(['maint-release', 'run-b'], { gh: fakeExec(), log: (m) => logs1.push(m), root });
      assert.ok(logs1.join('\n').includes('NOT OWNER'));
      assert.equal(fs.existsSync(lockDir(root)), true);
      assert.equal(process.exitCode, 1);
      process.exitCode = prevExit;

      // The owner releases cleanly.
      const logs2 = [];
      runSdlc(['maint-release', 'run-a'], { gh: fakeExec(), log: (m) => logs2.push(m), root });
      assert.ok(logs2.join('\n').includes('RELEASED run-a'));
      assert.equal(fs.existsSync(lockDir(root)), false);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });
});

describe('cycle-prep (one-shot pre-dispatch sequence)', () => {
  /** A throwaway repo root with a .git dir for the maintenance lock. */
  const makeRoot = () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'sdlc-cycle-prep-test-'));
    fs.mkdirSync(path.join(root, '.git'));
    return root;
  };
  const lockDir = (root) => path.join(root, '.git', 'sdlc-maint.lock');

  /** Canned gh/git responses for an empty, idle pipeline. */
  const idleGh = (extra = {}) => fakeExec({
    'issue list --state open --json number,labels,createdAt,title --limit 200': '[]',
    'issue list --state open --json number,labels,updatedAt --limit 200': '[]',
    [graphqlKey('OPEN')]: graphqlPage([]),
    [graphqlKey('CLOSED')]: graphqlPage([]),
    'pr list --state open --json number,title,headRefName,mergeable,reviewDecision,isDraft': '[]',
    [`pr list --state open --base ${DEFAULT_BRANCH} --json number,headRefName,baseRefName,mergeable,closingIssuesReferences --limit 200`]: '[]',
    ...extra,
  });
  const idleGit = () => fakeExec({
    'rev-parse --abbrev-ref HEAD': `${DEFAULT_BRANCH}\n`,
    [`rev-parse ${DEFAULT_BRANCH}`]: 'abc123def\n',
    [`branch --merged ${DEFAULT_BRANCH}`]: '',
    'branch -vv': '',
    'worktree list --porcelain': '',
    [`log -1 --format=%cI origin/${DEFAULT_BRANCH}`]: '2026-07-05T00:00:00+00:00\n',
  });

  it('runs the full sequence on an idle cycle: run-id first, delimited sections, lock released', () => {
    const root = makeRoot();
    try {
      const logs = [];
      runSdlc(['cycle-prep'], { gh: idleGh(), git: idleGit(), log: (m) => logs.push(m), root });
      const out = logs.join('\n');
      // run-id line preserved verbatim, printed first (single source of truth).
      assert.match(logs[0], /^run-id: dispatch-\d{8}-\d{4}-[0-9a-f]{4}$/);
      assert.match(logs[1], /^started: \d{4}-\d{2}-\d{2}T/);
      // Every section, clearly delimited, in order.
      const sections = logs.filter((l) => l.startsWith('\n=== ')).map((l) => l.replace(/[\n= ]/g, ''));
      assert.deepEqual(sections, [
        'maint-lock', 'lanes', 'gate', 'deps-migrate', 'deps', 'sweep',
        'git-maint', 'node-modules', 'worktree-sweep', 'conflict-scan', 'maint-release', 'summary',
      ]);
      assert.ok(out.includes('deps --migrate: no prose dependency declarations to migrate.'));
      // No lockfile in the throwaway root → a skip line, never an install.
      assert.ok(out.includes('node-modules: skipped (no readable package-lock.json in main checkout)'));
      // Identical semantics to the individual commands.
      assert.ok(out.includes('maint-lock: ACQUIRED'));
      assert.ok(out.includes('intake: depth 0'));
      assert.ok(out.includes('gate: CLEAR'));
      assert.ok(out.includes('deps: labels match edges; lint clean'));
      assert.ok(out.includes('sweep: clear'));
      assert.ok(out.includes('worktree-sweep: no issue-scoped worktrees.'));
      assert.ok(out.includes(`conflict-scan: no conflicting PRs into ${DEFAULT_BRANCH}.`));
      assert.ok(out.includes('maint-release: RELEASED'));
      assert.ok(out.includes('maintenance previewed'));
      // Lock released at the end of the maintenance section.
      assert.equal(fs.existsSync(lockDir(root)), false);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it('converts prose dependency declarations to native edges every cycle — proposed on a dry run, POSTed under --apply', () => {
    const root = makeRoot();
    try {
      // #20 was filed between cycles with a prose dependency and no edge yet.
      const gh = idleGh({
        [graphqlKey('OPEN')]: graphqlPage([
          { number: 20, id: 2000, body: 'Depends on #21', labels: ['stage:intake'], blockedBy: [] },
          { number: 21, id: 2100, body: '', labels: ['stage:queued'] },
        ]),
      });
      const logs = [];
      runSdlc(['cycle-prep'], { gh, git: idleGit(), log: (m) => logs.push(m), root });
      const out = logs.join('\n');
      assert.ok(out.includes('deps --migrate: #20 blocked_by #21 (OPEN)'));
      assert.ok(out.includes('dry run'));
      assert.equal(gh.calls.some((c) => c.includes('POST')), false);
      // deps-migrate runs before the derived-label pass, so a new edge is labelled the same cycle.
      assert.ok(out.indexOf('=== deps-migrate ===') < out.indexOf('=== deps ==='));

      runSdlc(['cycle-prep', '--apply'], { gh, git: idleGit(), log: () => {}, root });
      const posts = gh.calls.filter((c) => c.includes('POST')).map((c) => c.join(' '));
      assert.deepEqual(posts, ['api -X POST repos/{owner}/{repo}/issues/20/dependencies/blocked_by -F issue_id=2100']);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it('lock held by a live run → maintenance skipped, prep continues, no failure exit, lock untouched', () => {
    const root = makeRoot();
    try {
      fs.mkdirSync(lockDir(root));
      fs.writeFileSync(path.join(lockDir(root), 'owner.txt'), `run-live ${new Date().toISOString()}\n`);

      const prevExit = process.exitCode;
      const logs = [];
      const git = idleGit();
      runSdlc(['cycle-prep'], { gh: idleGh(), git, log: (m) => logs.push(m), root });
      const out = logs.join('\n');
      assert.ok(out.includes('HELD by run-live'));
      // Step 0 still runs...
      assert.ok(out.includes('gate: CLEAR'));
      assert.ok(out.includes('sweep: clear'));
      // ...but the maintenance trio does not.
      assert.ok(out.includes('maintenance: skipped (lock held by run-live'));
      assert.ok(!out.includes('=== git-maint ==='));
      assert.ok(!out.includes('=== node-modules ==='));
      assert.equal(git.calls.some((c) => c[0] === 'fetch'), false);
      // A held lock is a normal outcome, never a cycle-prep failure...
      assert.equal(process.exitCode ?? undefined, prevExit ?? undefined);
      // ...and the live holder's lock is left in place (no release).
      assert.equal(fs.existsSync(lockDir(root)), true);
      assert.ok(!out.includes('maint-release'));
    } finally {
      process.exitCode = undefined;
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it('--issue focuses only the lanes section: header + summary name the focus, every global section still runs', () => {
    const root = makeRoot();
    try {
      const gh = idleGh({
        'issue list --state open --json number,labels,createdAt,title --limit 200': JSON.stringify([
          { number: 41, createdAt: '2026-07-01T00:00:00Z', labels: [{ name: 'stage:build' }] },
          { number: 42, createdAt: '2026-07-02T00:00:00Z', labels: [{ name: 'stage:build' }] },
        ]),
      });
      const logs = [];
      runSdlc(['cycle-prep', '--issue', '42'], { gh, git: idleGit(), log: (m) => logs.push(m), root });
      const out = logs.join('\n');
      assert.match(logs[0], /^run-id: dispatch-\d{8}-\d{4}-[0-9a-f]{4}$/);
      assert.match(logs[1], /^started: \d{4}-\d{2}-\d{2}T/);
      assert.equal(logs[2], 'focus: #42');
      const sections = logs.filter((l) => l.startsWith('\n=== ')).map((l) => l.replace(/[\n= ]/g, ''));
      assert.deepEqual(sections, [
        'maint-lock', 'lanes', 'gate', 'deps-migrate', 'deps', 'sweep',
        'git-maint', 'node-modules', 'worktree-sweep', 'conflict-scan', 'maint-release', 'summary',
      ]);
      assert.ok(logs.includes('build: depth 2, eligible #42'));
      assert.ok(out.includes('focus #42: eligible in build'));
      assert.ok(out.includes('gate: CLEAR'));
      assert.ok(out.includes('sweep: clear'));
      assert.ok(out.includes(`conflict-scan: no conflicting PRs into ${DEFAULT_BRANCH}.`));
      assert.ok(out.includes('maint-release: RELEASED'));
      assert.match(logs[logs.length - 1], /^cycle-prep: run-id \S+, started \S+, maintenance previewed, focus #42\.$/);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it('without --issue the header and summary carry no focus', () => {
    const root = makeRoot();
    try {
      const logs = [];
      runSdlc(['cycle-prep'], { gh: idleGh(), git: idleGit(), log: (m) => logs.push(m), root });
      assert.equal(logs.some((l) => /focus/.test(l)), false);
      assert.match(logs[logs.length - 1], /maintenance previewed\.$/);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it('rejects an invalid --issue before minting, locking or fetching', () => {
    const root = makeRoot();
    try {
      const gh = idleGh();
      const logs = [];
      throwsSdlc(
        () => runSdlc(['cycle-prep', '--issue', 'x1'], { gh, git: idleGit(), log: (m) => logs.push(m), root }),
        /expected an issue number/,
      );
      assert.deepEqual(logs, []);
      assert.deepEqual(gh.calls, []);
      assert.equal(fs.existsSync(lockDir(root)), false);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it('reap path: a stale wip lock inside the report is reaped (gate --reap semantics)', () => {
    const root = makeRoot();
    try {
      const oldIso = new Date(Date.now() - 5 * 3600000).toISOString();
      const gh = idleGh({
        'issue list --state open --json number,labels,updatedAt --limit 200': JSON.stringify([
          { number: 7, labels: [{ name: 'stage:build' }, { name: WIP_LABEL }], updatedAt: oldIso },
        ]),
        'api repos/{owner}/{repo}/issues/7/timeline --paginate': JSON.stringify([
          { event: 'labeled', label: { name: WIP_LABEL }, created_at: oldIso },
        ]),
        'issue view 7 --json comments --jq [.comments[] | {body: .body, createdAt: .createdAt}]':
          JSON.stringify([{ body: 'sdlc:claim run-dead build', createdAt: oldIso }]),
      });
      const logs = [];
      runSdlc(['cycle-prep'], { gh, git: idleGit(), log: (m) => logs.push(m), root });
      const out = logs.join('\n');
      assert.ok(out.includes('gate: REAP #7'));
      assert.ok(out.includes(`reaped: removed ${WIP_LABEL} from #7`));
      assert.equal(gh.calls.some((c) => c[0] === 'issue' && c[1] === 'edit' && c[2] === '7'), true);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });
});

describe('runSdlc gate --reap verify-before-write', () => {
  it('re-verifies against the newest claim and skips a fresh one', () => {
    // Snapshot says #5 is wip and its labeled event is ancient (stale), but by
    // write time another run has reaped it and a NEW worker claimed it — the
    // fresh claim must suppress the reap.
    const oldIso = new Date(Date.now() - 5 * 3600000).toISOString();
    const freshIso = new Date(Date.now() - 5 * 60000).toISOString();
    const gh = fakeExec({
      'issue list --state open --json number,labels,updatedAt --limit 200': JSON.stringify([
        { number: 5, labels: [{ name: 'stage:build' }, { name: WIP_LABEL }], updatedAt: oldIso },
      ]),
      'api repos/{owner}/{repo}/issues/5/timeline --paginate': JSON.stringify([
        { event: 'labeled', label: { name: WIP_LABEL }, created_at: oldIso },
      ]),
      'issue view 5 --json comments --jq [.comments[] | {body: .body, createdAt: .createdAt}]':
        JSON.stringify([{ body: 'sdlc:claim run-new build', createdAt: freshIso }]),
    });
    const logs = [];
    runSdlc(['gate', '--reap'], { gh, log: (m) => logs.push(m) });
    assert.ok(logs.join('\n').includes('reap skipped — fresh claim'));
    assert.equal(gh.calls.some((c) => c[0] === 'issue' && c[1] === 'edit'), false);
  });
});

describe('planNodeModulesRefresh / node-modules (shared install drift)', () => {
  it('no drift: the lockfile hash matches the stamp, so no install churn', () => {
    const plan = planNodeModulesRefresh({ lockHash: 'a'.repeat(64), stampHash: 'a'.repeat(64), apply: true });
    assert.equal(plan.action, 'none');
    assert.equal(plan.reason, 'current');
    assert.ok(plan.message.includes('node-modules: current (lock aaaaaaaa)'));
  });

  it('drift with --apply: installs', () => {
    const plan = planNodeModulesRefresh({ lockHash: 'b'.repeat(64), stampHash: 'a'.repeat(64), apply: true });
    assert.equal(plan.action, 'install');
    assert.equal(plan.reason, 'drift');
    assert.ok(plan.drift.includes('aaaaaaaa'));
    assert.ok(plan.drift.includes('bbbbbbbb'));
  });

  it('a missing stamp counts as drift — the one-time self-heal on a fresh or wiped node_modules', () => {
    const plan = planNodeModulesRefresh({ lockHash: 'b'.repeat(64), stampHash: null, apply: true });
    assert.equal(plan.action, 'install');
    assert.ok(plan.drift.includes('none'));
  });

  it('never installs over human dependency WIP in the main checkout', () => {
    const plan = planNodeModulesRefresh({
      lockHash: 'b'.repeat(64),
      stampHash: 'a'.repeat(64),
      dirtyDepFiles: ['package-lock.json'],
      apply: true,
    });
    assert.equal(plan.action, 'skip');
    assert.equal(plan.reason, 'dirty');
    assert.ok(plan.message.includes('package-lock.json dirty in main checkout'));
  });

  it('drift without --apply previews, like every other mutator', () => {
    const plan = planNodeModulesRefresh({ lockHash: 'b'.repeat(64), stampHash: 'a'.repeat(64) });
    assert.equal(plan.action, 'plan');
    assert.ok(plan.message.includes('would install'));
    assert.ok(plan.message.includes('dry run'));
  });

  it('an unreadable lockfile is a skip, not a guessed install', () => {
    const plan = planNodeModulesRefresh({ lockHash: null, apply: true });
    assert.equal(plan.action, 'skip');
    assert.equal(plan.reason, 'no-lockfile');
  });

  describe('runSdlc node-modules', () => {
    /** A throwaway main checkout: .git dir, a lockfile, and an empty node_modules. */
    const makeMain = (lockBody = '{"lockfileVersion":3}') => {
      const main = fs.mkdtempSync(path.join(os.tmpdir(), 'sdlc-node-modules-test-'));
      fs.mkdirSync(path.join(main, '.git'));
      fs.mkdirSync(path.join(main, 'node_modules'));
      fs.writeFileSync(path.join(main, 'package-lock.json'), lockBody);
      return main;
    };
    /** git fake answering the common-dir probe for `main`, from a worktree `root`. */
    const gitFor = (main, extra = {}) => fakeExec({
      'rev-parse --path-format=absolute --git-common-dir': `${path.join(main, '.git')}\n`,
      [`-C ${main} status --porcelain -- package.json package-lock.json`]: '',
      [`-C ${main} status --porcelain -- package-lock.json`]: '',
      ...extra,
    });
    /** npm fake recording argv AND the cwd it was told to install in. */
    const fakeNpm = () => {
      const calls = [];
      const fn = (args, opts) => {
        calls.push({ args, opts });
        return '';
      };
      fn.calls = calls;
      return fn;
    };
    const worktreeRoot = path.join(os.tmpdir(), 'acme-wt-12');

    it('installs in the MAIN checkout (not the worktree it was run from), then stamps the lock hash', () => {
      const main = makeMain();
      try {
        const npm = fakeNpm();
        const logs = [];
        // `root` is an issue worktree whose node_modules is a junction into `main`.
        runSdlc(['node-modules', '--apply'], { git: gitFor(main), npm, log: (m) => logs.push(m), root: worktreeRoot });

        assert.equal(npm.calls.length, 1);
        assert.deepEqual(npm.calls[0].args, ['install', '--no-audit', '--no-fund']);
        assert.equal(npm.calls[0].opts.cwd, main);
        assert.ok(logs.join('\n').includes('installed'));
        // Stamp written next to the install it describes.
        const stamp = JSON.parse(fs.readFileSync(path.join(main, ...INSTALL_STAMP_PATH), 'utf8'));
        assert.equal(typeof stamp.lockHash, 'string');
        assert.equal(stamp.lockHash.length, 64);
        assert.match(stamp.installedAt, /^\d{4}-\d{2}-\d{2}T/);

        // Second run: the stamp now matches, so npm is not called again.
        const npm2 = fakeNpm();
        const logs2 = [];
        runSdlc(['node-modules', '--apply'], { git: gitFor(main), npm: npm2, log: (m) => logs2.push(m), root: worktreeRoot });
        assert.equal(npm2.calls.length, 0);
        assert.ok(logs2.join('\n').includes('node-modules: current'));
      } finally {
        fs.rmSync(main, { recursive: true, force: true });
      }
    });

    it('a dirty main-checkout lockfile skips the install entirely', () => {
      const main = makeMain();
      try {
        const git = gitFor(main, {
          [`-C ${main} status --porcelain -- package.json package-lock.json`]: ' M package-lock.json\n',
        });
        const npm = fakeNpm();
        const logs = [];
        runSdlc(['node-modules', '--apply'], { git, npm, log: (m) => logs.push(m), root: main });
        assert.equal(npm.calls.length, 0);
        assert.ok(logs.join('\n').includes('node-modules: skipped (package-lock.json dirty in main checkout)'));
      } finally {
        fs.rmSync(main, { recursive: true, force: true });
      }
    });

    it('a failed install is one greppable line, never a throw — the next cycle retries', () => {
      const main = makeMain();
      try {
        const npm = () => {
          throw new Error('npm ERR! code ENOENT\nmore noise');
        };
        const logs = [];
        assert.doesNotThrow(() =>
          runSdlc(['node-modules', '--apply'], { git: gitFor(main), npm, log: (m) => logs.push(m), root: main }),
        );
        assert.ok(logs.join('\n').includes('node-modules: install FAILED (npm ERR! code ENOENT)'));
        // No stamp: the drift is still live, so the next cycle tries again.
        assert.equal(fs.existsSync(path.join(main, ...INSTALL_STAMP_PATH)), false);
      } finally {
        fs.rmSync(main, { recursive: true, force: true });
      }
    });

    it('reports a lockfile the install rewrote instead of committing it', () => {
      const main = makeMain();
      try {
        const git = gitFor(main, {
          [`-C ${main} status --porcelain -- package-lock.json`]: ' M package-lock.json\n',
        });
        const logs = [];
        runSdlc(['node-modules', '--apply'], { git, npm: fakeNpm(), log: (m) => logs.push(m), root: main });
        assert.ok(logs.join('\n').includes('install rewrote package-lock.json — left uncommitted for a human'));
        assert.equal(git.calls.some((c) => c[0] === 'commit' || c.includes('add')), false);
      } finally {
        fs.rmSync(main, { recursive: true, force: true });
      }
    });

    it('falls back to the relative --git-common-dir on a git that rejects --path-format', () => {
      const main = makeMain();
      try {
        const git = fakeExec({ 'rev-parse --git-common-dir': '.git\n' });
        // --path-format returns '' here (unanswered key), so the fallback resolves against root.
        assert.equal(mainCheckoutRoot(git, main), path.resolve(main, '.git', '..'));
      } finally {
        fs.rmSync(main, { recursive: true, force: true });
      }
    });
  });
});

describe('usage', () => {
  it('prints usage with no command', () => {
    const logs = [];
    runSdlc([], { log: (m) => logs.push(m) });
    assert.ok(logs.join('\n').includes('deterministic SDLC pipeline'));
  });

  it('rejects an unknown command', () => {
    throwsSdlc(() => runSdlc(['frobnicate'], { log: () => {} }), /unknown command/);
  });

  it('WIP_STALE_MS is two hourly cycles', () => {
    assert.equal(WIP_STALE_MS, 2 * 60 * 60 * 1000);
  });
});
