// Tests for the pure planning helpers in sdlc.mjs. Run with `npm test`
// (node:test, built in — no dependencies). The main-guard in sdlc.mjs keeps
// importing side-effect free.
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { planClaimVerify, lastUnlabeledAt } from './sdlc.mjs';

// Regression for issue #13: a formatted EMIT comment (e.g.
// `**Intake triage: ADVANCE -> stage:queued**`) must NOT be mistaken for
// anything that supersedes or blocks a later claim. Supersession is driven
// solely by the `sdlc:wip` unlabeled timeline boundary, never by parsing
// outcome keywords out of comment text — so no "ghost lost race".
test('planClaimVerify: formatted EMIT comment does not cause a ghost lost race', () => {
  const boundary = '2026-07-12T22:28:22Z'; // wip removed at intake's ADVANCE
  const comments = [
    { body: 'sdlc:claim dispatch-intake intake', createdAt: '2026-07-12T22:26:46Z' },
    {
      // The exact shape that broke the old `^(ADVANCE|BOUNCE|PARK|CONTINUE)`
      // regex: outcome keyword buried behind Markdown emphasis and a label.
      body: '**Intake triage: ADVANCE -> stage:queued**\n\nConfirmed at scripts/sdlc.mjs...',
      createdAt: '2026-07-12T22:28:22Z',
    },
    { body: 'sdlc:claim dispatch-build build', createdAt: '2026-07-17T01:19:04Z' },
  ];

  const result = planClaimVerify(comments, 'dispatch-build', boundary);
  assert.equal(result.won, true, 'post-boundary build claim must win');
  assert.equal(result.winner, 'dispatch-build');
});

test('planClaimVerify: claims at/before the boundary are settled history', () => {
  const boundary = '2026-07-12T22:28:22Z';
  // Only the pre-boundary intake claim exists; my own (build) claim is absent.
  const comments = [
    { body: 'sdlc:claim dispatch-intake intake', createdAt: '2026-07-12T22:26:46Z' },
  ];
  const result = planClaimVerify(comments, 'dispatch-build', boundary);
  assert.equal(result.won, false);
  assert.equal(result.reason, 'own claim not found');
});

test('planClaimVerify: earliest live claim wins; ties break to lower run-id', () => {
  // No boundary → all claims live. Two claims at the same instant: the
  // lexicographically lower run-id wins.
  const comments = [
    { body: 'sdlc:claim run-b build', createdAt: '2026-07-17T01:00:00Z' },
    { body: 'sdlc:claim run-a build', createdAt: '2026-07-17T01:00:00Z' },
  ];
  assert.equal(planClaimVerify(comments, 'run-a').won, true);
  assert.equal(planClaimVerify(comments, 'run-b').won, false);

  // Earlier claim beats a later one regardless of run-id ordering.
  const staggered = [
    { body: 'sdlc:claim zzz build', createdAt: '2026-07-17T01:00:00Z' },
    { body: 'sdlc:claim aaa build', createdAt: '2026-07-17T01:05:00Z' },
  ];
  assert.equal(planClaimVerify(staggered, 'zzz').won, true);
  assert.equal(planClaimVerify(staggered, 'aaa').won, false);
});

test('lastUnlabeledAt: returns the most recent removal of the label', () => {
  const timeline = [
    { event: 'unlabeled', label: { name: 'sdlc:wip' }, created_at: '2026-07-12T22:28:22Z' },
    { event: 'labeled', label: { name: 'sdlc:wip' }, created_at: '2026-07-17T01:18:00Z' },
    { event: 'unlabeled', label: { name: 'sdlc:wip' }, created_at: '2026-07-17T03:00:00Z' },
    { event: 'unlabeled', label: { name: 'other' }, created_at: '2026-07-18T00:00:00Z' },
  ];
  assert.equal(lastUnlabeledAt(timeline, 'sdlc:wip'), '2026-07-17T03:00:00Z');
  assert.equal(lastUnlabeledAt([], 'sdlc:wip'), null);
  assert.equal(lastUnlabeledAt(timeline, 'never'), null);
});
