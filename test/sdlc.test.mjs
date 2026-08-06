// Tests for scripts/sdlc.mjs — zero-dependency, node's built-in runner:
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
  STAGES,
  STAGE_GRAPH,
  WIP_LABEL,
  WIP_STALE_MS,
  SdlcError,
  isValidStage,
  isValidTransition,
  currentStage,
  planAdvance,
  planEmit,
  EMIT_OUTCOMES,
  runSdlc,
  planClaimVerify,
  lastUnlabeledAt,
  computeLanes,
  laneIneligibilityBreakdown,
  planHealLane,
  mintRunId,
  parseWorktrees,
  planWorktreeSweep,
  unlinkWorktreeRootLinks,
  classifyStatusForSweep,
  computeDigest,
  computeSweep,
  computeSweepAck,
  planConflictScan,
  conflictCommentBody,
  CONFLICT_BOUNCE_STAGES,
  dupTokens,
  dupQueryTerms,
  scoreDupCandidates,
} from '../scripts/sdlc.mjs';

/** Build a fake gh/git executor that records calls and returns canned output. */
function fakeExec(responses = {}) {
  const calls = [];
  const fn = (args) => {
    calls.push(args);
    const key = args.join(' ');
    return responses[key] ?? '';
  };
  fn.calls = calls;
  return fn;
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
    // 11 (critical) before 10 (future); 12 (wip) and 13 (hold) excluded;
    // 14 is dual-stage but still eligible for build.
    assert.deepEqual(lanes.build.eligible, [11, 10, 14]);
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
    assert.deepEqual(lanes.build.ineligible, { hold: 1, 'needs-human': 0, wip: 1 });
    const b = lanes.build;
    const inelig = b.ineligible.hold + b.ineligible['needs-human'] + b.ineligible.wip;
    assert.equal(inelig, b.depth - b.eligible.length);
  });

  it('buckets each ineligible item once, hold › needs-human › wip precedence', () => {
    const { lanes } = computeLanes([
      { number: 20, createdAt: '2026-07-01T00:00:00Z', labels: ['stage:audit', 'sdlc:hold', WIP_LABEL] },
      { number: 21, createdAt: '2026-07-02T00:00:00Z', labels: ['stage:audit', 'sdlc:needs-human', WIP_LABEL] },
    ]);
    assert.deepEqual(lanes.audit.ineligible, { hold: 1, 'needs-human': 1, wip: 0 });
  });
});

describe('laneIneligibilityBreakdown', () => {
  it('renders only non-zero buckets in hold, needs-human, wip order', () => {
    assert.equal(
      laneIneligibilityBreakdown({ ineligible: { hold: 12, 'needs-human': 4, wip: 0 } }),
      '(hold 12, needs-human 4)',
    );
    assert.equal(laneIneligibilityBreakdown({ ineligible: { hold: 0, 'needs-human': 0, wip: 1 } }), '(wip 1)');
  });

  it('returns empty string for a fully-eligible lane (all buckets zero)', () => {
    assert.equal(laneIneligibilityBreakdown({ ineligible: { hold: 0, 'needs-human': 0, wip: 0 } }), '');
    assert.equal(laneIneligibilityBreakdown({}), '');
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

describe('computeSweep', () => {
  const openIssues = [
    { number: 30, title: 'dependent, blocked', labels: ['blocked'], body: 'blocked on #10' },
    { number: 31, title: 'dependent, ready', labels: ['ready'], body: 'follows #10 nicely' },
    { number: 32, title: 'unrelated', labels: [], body: 'mentions #100 not the closed one' },
    { number: 33, title: 'near-miss', labels: [], body: 'refs #101, a different issue entirely' },
  ];
  const mergedPrs = [
    { number: 500, mergedAt: '2026-07-10T00:00:00Z', title: 'feat: thing', closes: [10] },
  ];

  it('maps a merged PR → closed issue → its dependents, flagging blocked ones', () => {
    const { items, closedIssues, empty } = computeSweep(mergedPrs, openIssues);
    assert.equal(empty, false);
    assert.deepEqual(closedIssues, [10]);
    assert.equal(items.length, 1);
    const closed = items[0].closes[0];
    assert.equal(closed.number, 10);
    assert.deepEqual(
      closed.dependents.map((d) => ({ number: d.number, blocked: d.blocked })),
      [
        { number: 30, blocked: true },
        { number: 31, blocked: false },
      ],
    );
  });

  it('word-boundaries references so #10 never matches #101 or #100', () => {
    const { items } = computeSweep(mergedPrs, openIssues);
    const depNums = items[0].closes[0].dependents.map((d) => d.number);
    assert.ok(!depNums.includes(32)); // #100
    assert.ok(!depNums.includes(33)); // #101
  });

  it('is clear when no merged PR closed an issue', () => {
    const noClose = [{ number: 501, mergedAt: '2026-07-10T00:00:00Z', title: 'chore', closes: [] }];
    const r = computeSweep(noClose, openIssues);
    assert.equal(r.empty, true);
    assert.deepEqual(r.items, []);
  });

  it('skips PRs already in sweptPrs (idempotent)', () => {
    assert.equal(computeSweep(mergedPrs, openIssues, { sweptPrs: [500] }).empty, true);
  });

  it('skips PRs merged at/before the sinceMs cutoff (bounded window)', () => {
    const cutoff = new Date('2026-07-10T12:00:00Z').getTime();
    assert.equal(computeSweep(mergedPrs, openIssues, { sinceMs: cutoff }).empty, true);
    const earlier = new Date('2026-07-09T00:00:00Z').getTime();
    assert.equal(computeSweep(mergedPrs, openIssues, { sinceMs: earlier }).empty, false);
  });

  it('accepts object-form labels and tolerates missing bodies', () => {
    const issues = [{ number: 40, title: 'x', labels: [{ name: 'blocked' }], body: 'needs #10' }];
    const { items } = computeSweep(mergedPrs, issues);
    assert.deepEqual(items[0].closes[0].dependents, [{ number: 40, title: 'x', blocked: true }]);
    const noDeps = computeSweep(mergedPrs, [{ number: 41, body: '' }]);
    assert.deepEqual(noDeps.items[0].closes[0].dependents, []);
  });
});

describe('computeSweepAck', () => {
  const mergedPrs = [
    { number: 500, mergedAt: '2026-07-10T00:00:00Z' },
    { number: 501, mergedAt: '2026-07-08T00:00:00Z' },
  ];

  it('marks all in-window merges and reports the newly-acked ones', () => {
    const { nextSwept, newlyAcked } = computeSweepAck(mergedPrs, { sweptPrs: [500] });
    assert.deepEqual([...nextSwept].sort(), [500, 501]);
    assert.deepEqual(newlyAcked, [501]);
  });

  it('excludes merges at/before the sinceMs cutoff from the marker', () => {
    const cutoff = new Date('2026-07-09T00:00:00Z').getTime();
    const { nextSwept, newlyAcked } = computeSweepAck(mergedPrs, { sinceMs: cutoff });
    assert.deepEqual(nextSwept, [500]);
    assert.deepEqual(newlyAcked, [500]);
  });

  it('drops prior swept PRs no longer in the fetched list (bounded marker)', () => {
    const { nextSwept } = computeSweepAck(mergedPrs, { sweptPrs: [400, 500] });
    assert.ok(!nextSwept.includes(400));
    assert.deepEqual([...nextSwept].sort(), [500, 501]);
  });

  it('is a no-op ack when everything in the window is already swept', () => {
    const { nextSwept, newlyAcked } = computeSweepAck(mergedPrs, { sweptPrs: [500, 501] });
    assert.deepEqual([...nextSwept].sort(), [500, 501]);
    assert.deepEqual(newlyAcked, []);
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
    [`pr list --state merged --base ${DEFAULT_BRANCH} --json number,mergedAt,title,closingIssuesReferences --limit 50`]: '[]',
    'issue list --state open --json number,title,labels,body --limit 200': '[]',
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
        'maint-lock', 'lanes', 'gate', 'sweep',
        'git-maint', 'worktree-sweep', 'conflict-scan', 'maint-release', 'summary',
      ]);
      // Identical semantics to the individual commands.
      assert.ok(out.includes('maint-lock: ACQUIRED'));
      assert.ok(out.includes('intake: depth 0'));
      assert.ok(out.includes('gate: CLEAR'));
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
