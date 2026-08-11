// Tests for scripts/sync-claude-config.mjs — zero-dependency, node's built-in runner:
//   node --test              (default discovery finds test/*.test.mjs)
//   node --test "test/*.test.mjs"
// Regression coverage for #118: a fresh git worktree materializes the .claude/
// mirrors with CRLF (core.autocrlf=true, no .gitattributes) while the script
// regenerates them with LF, so strict string equality reported them "out of date".

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

import { normalizeEol, isInSync } from '../scripts/sync-claude-config.mjs';

describe('normalizeEol', () => {
  it('converts CRLF to LF', () => {
    assert.equal(normalizeEol('a\r\nb\r\n'), 'a\nb\n');
  });

  it('leaves LF-only text unchanged (idempotent)', () => {
    const lf = 'a\nb\n';
    assert.equal(normalizeEol(lf), lf);
    assert.equal(normalizeEol(normalizeEol(lf)), lf);
  });

  it('normalizes mixed CRLF/LF to all LF', () => {
    assert.equal(normalizeEol('---\nname: scout\n---\r\nbody\r\n'), '---\nname: scout\n---\nbody\n');
  });

  it('preserves a lone carriage return', () => {
    assert.equal(normalizeEol('a\rb'), 'a\rb');
  });
});

describe('isInSync', () => {
  it('treats CRLF vs LF of the same text as in sync', () => {
    assert.equal(isInSync('a\r\nb\r\n', 'a\nb\n'), true);
  });

  it('treats a mixed-ending mirror vs a pure-CRLF checkout as in sync (.claude/agents/*.md case)', () => {
    // syncAgents() concatenates LF-joined frontmatter onto a CRLF body, so the
    // main checkout holds mixed endings while a fresh worktree holds pure CRLF.
    const mixed = '---\nname: scout\n---\r\nBody line.\r\n';
    const crlf = '---\r\nname: scout\r\n---\r\nBody line.\r\n';
    assert.equal(isInSync(crlf, mixed), true);
  });

  it('treats identical text as in sync', () => {
    assert.equal(isInSync('same\n', 'same\n'), true);
  });

  it('still detects genuine drift with matching line endings', () => {
    assert.equal(isInSync('old\n', 'new\n'), false);
  });

  it('still detects genuine drift when line endings also differ', () => {
    assert.equal(isInSync('old\r\ntext\r\n', 'new\ntext\n'), false);
  });

  it('does not mask added or removed lines', () => {
    assert.equal(isInSync('a\r\nb\r\n', 'a\nb\nc\n'), false);
  });

  it('treats a missing destination as not in sync', () => {
    assert.equal(isInSync(null, 'anything\n'), false);
  });
});
