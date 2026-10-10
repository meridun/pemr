import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  extractIssueRefs,
  extractSupersederPointers,
  terminalSuperseders,
  findViolations,
  parseDocsDir,
  SUPERSEDED_LABEL,
} from '../scripts/check-doc-issue-refs.mjs';

const execFileAsync = promisify(execFile);
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const SCRIPT = path.join(ROOT, 'scripts', 'check-doc-issue-refs.mjs');

// Runs the CLI with the gh lookup replaced by a fixture. `docs` (name -> text) seeds a temp
// docs dir passed via --docs-dir; omit it to scan this repo's real docs/.
async function runWithFixture(fixture, docs) {
  const tmp = await fs.mkdtemp(path.join(os.tmpdir(), 'doc-issue-refs-'));
  const file = path.join(tmp, 'fixture.json');
  await fs.writeFile(file, JSON.stringify(fixture));
  const args = [SCRIPT];
  if (docs) {
    const dir = path.join(tmp, 'docs');
    await fs.mkdir(dir);
    for (const [name, text] of Object.entries(docs)) await fs.writeFile(path.join(dir, name), text);
    args.push('--docs-dir', dir);
  }
  try {
    const { stdout, stderr } = await execFileAsync(process.execPath, args, {
      cwd: ROOT,
      env: { ...process.env, DOC_ISSUE_REFS_FIXTURE: file },
    });
    return { code: 0, stdout, stderr };
  } catch (err) {
    return { code: err.code, stdout: err.stdout, stderr: err.stderr };
  } finally {
    await fs.rm(tmp, { recursive: true, force: true });
  }
}

test('CLI passes against the real docs when nothing is superseded', async () => {
  const r = await runWithFixture({});
  assert.equal(r.code, 0);
  assert.match(r.stdout, /OK \(\d+ docs, 0 superseded/);
});

test('CLI fails when a doc cites a superseded issue without its superseder', async () => {
  const r = await runWithFixture({ 1039: [999999] }, { 'A.md': 'Established in #1039.\n' });
  assert.equal(r.code, 1);
  assert.match(r.stderr, /A\.md cites superseded #1039 without its superseder #999999/);
});

test('CLI passes when the superseder is cited alongside', async () => {
  const r = await runWithFixture({ 1039: [2000] }, { 'A.md': 'Established in #1039, replaced by #2000.\n' });
  assert.equal(r.code, 0);
  assert.match(r.stdout, /OK \(1 docs, 1 superseded/);
});

test('CLI fails on a superseded label with no pointer', async () => {
  const r = await runWithFixture({ 999998: [] }, { 'A.md': 'nothing cited\n' });
  assert.equal(r.code, 1);
  assert.ok(r.stderr.includes("issue #999998 is labeled 'superseded' but has no 'Superseded by #N' pointer"));
});

test('parseDocsDir defaults to docs and accepts both flag forms', () => {
  assert.equal(parseDocsDir([]), 'docs');
  assert.equal(parseDocsDir(['--docs-dir', 'handbook']), 'handbook');
  assert.equal(parseDocsDir(['--docs-dir=wiki']), 'wiki');
});

test('extractIssueRefs finds bare, parenthesised, and heading-embedded refs', () => {
  const refs = extractIssueRefs('Fixed in #12 (see #1234).\n### Hysteresis (#646)\n- #99/#100');
  assert.deepEqual([...refs].sort((a, b) => a - b), [12, 99, 100, 646, 1234]);
});

test('extractIssueRefs ignores HTML entities, URL fragments, and markdown headings', () => {
  const refs = extractIssueRefs('&#123; Glossary.md#1234 ## Heading #1\nfoo#55');
  assert.equal(refs.size, 0);
});

test('extractSupersederPointers parses case-insensitive pointers and dedupes', () => {
  assert.deepEqual(extractSupersederPointers('Superseded by #42.\nsuperseded  by #42\nSuperseded by #43'), [42, 43]);
});

test('extractSupersederPointers returns empty when absent', () => {
  assert.deepEqual(extractSupersederPointers('closed as done'), []);
});

const chain = new Map([
  [1, { superseders: [2] }],
  [2, { superseders: [3] }],
  [7, { superseders: [8, 9] }],
  [10, { superseders: [11] }],
  [11, { superseders: [10] }],
]);

test('terminalSuperseders follows a chain to the terminal issue', () => {
  assert.deepEqual(terminalSuperseders(1, chain), [3]);
});

test('terminalSuperseders returns multiple terminals for a fan-out', () => {
  assert.deepEqual(terminalSuperseders(7, chain), [8, 9]);
});

test('terminalSuperseders does not loop on a cycle', () => {
  assert.deepEqual(terminalSuperseders(10, chain), []);
});

test('terminalSuperseders returns empty for a non-superseded issue', () => {
  assert.deepEqual(terminalSuperseders(99, chain), []);
});

const superseded = new Map([
  [100, { superseders: [200] }],
  [200, { superseders: [300] }],
  [400, { superseders: [] }],
]);

test('findViolations flags a doc citing a superseded issue without its terminal', () => {
  const docs = [{ file: 'docs/A.md', refs: new Set([100, 200]) }];
  assert.deepEqual(findViolations(docs, superseded).docViolations, [
    { file: 'docs/A.md', issue: 100, terminals: [300] },
    { file: 'docs/A.md', issue: 200, terminals: [300] },
  ]);
});

test('findViolations passes when the terminal superseder is cited in the same file', () => {
  const docs = [{ file: 'docs/A.md', refs: new Set([100, 300]) }];
  assert.deepEqual(findViolations(docs, superseded).docViolations, []);
});

test('findViolations reports a superseded label with no pointer once, not per doc', () => {
  const docs = [
    { file: 'docs/A.md', refs: new Set([400]) },
    { file: 'docs/B.md', refs: new Set([400]) },
  ];
  const { docViolations, danglingSuperseded } = findViolations(docs, superseded);
  assert.deepEqual(docViolations, []);
  assert.deepEqual(danglingSuperseded, [400]);
});

test('findViolations ignores docs that cite only live issues', () => {
  const docs = [{ file: 'docs/A.md', refs: new Set([1, 2, 300]) }];
  assert.deepEqual(findViolations(docs, superseded).docViolations, []);
});

test('SUPERSEDED_LABEL is the label name the convention relies on', () => {
  assert.equal(SUPERSEDED_LABEL, 'superseded');
});
