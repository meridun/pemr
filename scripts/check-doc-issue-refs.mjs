#!/usr/bin/env node
/**
 * Doc ↔ issue supersession guard.
 *
 * Convention (docs/Documentation.md): L3 docs describe current state; GitHub
 * Issues are the canonical history for closed work UNLESS the issue has itself
 * been superseded. L3 docs must cite the *terminal* issue of a supersession
 * chain, never a superseded one on its own.
 *
 * Convention this script enforces:
 *   - An issue that a later issue replaced carries the `superseded` label and a
 *     `Superseded by #N` pointer (closing comment or body).
 *   - Any `<docs-dir>/*.md` citation `#<old>` of a superseded issue must be accompanied,
 *     in the same file, by a citation of its terminal superseder `#<new>`.
 *   - A `superseded` label with no `Superseded by #N` pointer is itself a finding.
 *
 * Intentionally dumb: a `#NNNN` regex over docs, one `gh issue list` dump, and
 * one `gh issue view` per superseded issue (a handful). No markdown parsing.
 *
 * Usage:
 *   node scripts/check-doc-issue-refs.mjs [--docs-dir <dir>]
 *     exit 1 on any violation; exit 2 if gh is unusable. <dir> defaults to docs/
 *     (resolved against the repo root); only its top-level *.md files are scanned.
 *   DOC_ISSUE_REFS_FIXTURE=<file.json>   test hook: {"<num>": [<superseder>, ...]} replaces gh
 */

import { promises as fs } from 'fs';
import path from 'path';
import { execFile } from 'child_process';
import { promisify } from 'util';
import { fileURLToPath } from 'url';

const execFileAsync = promisify(execFile);
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const DEFAULT_DOCS_DIR = 'docs';

export const SUPERSEDED_LABEL = 'superseded';
const LIST_LIMIT = 1000;

/**
 * Issue numbers cited in a markdown text as `#NNN`.
 * Skips HTML entities (`&#123;`), hex-ish runs glued to a word (`abc#12`), and
 * URL fragments (`.md#1234`), which never denote issues here.
 * @param {string} text
 * @returns {Set<number>}
 */
export function extractIssueRefs(text) {
  const refs = new Set();
  const re = /(^|[^\w&.])#(\d{2,6})(?!\w)/g;
  let m;
  while ((m = re.exec(text)) !== null) refs.add(Number(m[2]));
  return refs;
}

/**
 * The `Superseded by #N` pointer(s) in an issue body / comments.
 * @param {string} text
 * @returns {number[]}
 */
export function extractSupersederPointers(text) {
  const out = [];
  const re = /superseded\s+by\s+#(\d+)/gi;
  let m;
  while ((m = re.exec(text)) !== null) out.push(Number(m[1]));
  return [...new Set(out)];
}

/**
 * Follow a supersession chain to its terminal issue(s).
 * @param {number} n
 * @param {Map<number, {superseders: number[]}>} superseded  superseded issue → pointers
 * @returns {number[]} terminal issue numbers (empty if the chain dead-ends)
 */
export function terminalSuperseders(n, superseded, seen = new Set()) {
  const entry = superseded.get(n);
  if (!entry || seen.has(n)) return [];
  seen.add(n);
  const out = [];
  for (const s of entry.superseders) {
    if (superseded.has(s)) out.push(...terminalSuperseders(s, superseded, seen));
    else out.push(s);
  }
  return [...new Set(out)];
}

/**
 * Pure core: which docs cite a superseded issue without its terminal superseder.
 * @param {Array<{file: string, refs: Set<number>}>} docs
 * @param {Map<number, {superseders: number[]}>} superseded
 * @returns {{docViolations: Array<{file: string, issue: number, terminals: number[]}>, danglingSuperseded: number[]}}
 */
export function findViolations(docs, superseded) {
  const danglingSuperseded = [...superseded.entries()]
    .filter(([, v]) => v.superseders.length === 0)
    .map(([n]) => n);
  const docViolations = [];
  for (const { file, refs } of docs) {
    for (const ref of refs) {
      if (!superseded.has(ref)) continue;
      const terminals = terminalSuperseders(ref, superseded);
      if (terminals.length === 0) continue; // reported via danglingSuperseded
      if (!terminals.some((t) => refs.has(t))) docViolations.push({ file, issue: ref, terminals });
    }
  }
  return { docViolations, danglingSuperseded };
}

async function gh(args) {
  const { stdout } = await execFileAsync('gh', args, { cwd: ROOT, maxBuffer: 64 * 1024 * 1024 });
  return stdout;
}

async function loadSupersededIssues() {
  const listed = JSON.parse(await gh(['issue', 'list', '--state', 'all', '--limit', String(LIST_LIMIT), '--json', 'number,labels']));
  if (listed.length >= LIST_LIMIT) {
    console.warn(`check-doc-issue-refs: issue list hit the ${LIST_LIMIT} cap; raise LIST_LIMIT`);
  }
  const superseded = new Map();
  for (const issue of listed) {
    if (!issue.labels?.some((l) => l.name === SUPERSEDED_LABEL)) continue;
    const view = JSON.parse(await gh(['issue', 'view', String(issue.number), '--json', 'body,comments']));
    const text = [view.body ?? '', ...(view.comments ?? []).map((c) => c.body ?? '')].join('\n');
    superseded.set(issue.number, { superseders: extractSupersederPointers(text) });
  }
  return superseded;
}

/**
 * Docs directory from `--docs-dir <dir>` / `--docs-dir=<dir>`, else `docs`.
 * @param {string[]} argv  process.argv.slice(2)
 * @returns {string}
 */
export function parseDocsDir(argv) {
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--docs-dir' && argv[i + 1]) return argv[i + 1];
    if (argv[i].startsWith('--docs-dir=')) return argv[i].slice('--docs-dir='.length);
  }
  return DEFAULT_DOCS_DIR;
}

async function loadDocs(docsDir) {
  const abs = path.resolve(ROOT, docsDir);
  const rel = path.relative(ROOT, abs);
  const label = (rel.startsWith('..') || path.isAbsolute(rel) ? abs : rel || '.').split(path.sep).join('/');
  const names = (await fs.readdir(abs)).filter((n) => n.endsWith('.md'));
  const docs = [];
  for (const name of names) {
    const text = await fs.readFile(path.join(abs, name), 'utf8');
    docs.push({ file: `${label}/${name}`, refs: extractIssueRefs(text) });
  }
  return docs;
}

async function loadFixture(file) {
  const raw = JSON.parse(await fs.readFile(file, 'utf8'));
  return new Map(Object.entries(raw).map(([n, superseders]) => [Number(n), { superseders }]));
}

async function main() {
  let superseded;
  try {
    superseded = process.env.DOC_ISSUE_REFS_FIXTURE
      ? await loadFixture(process.env.DOC_ISSUE_REFS_FIXTURE)
      : await loadSupersededIssues();
  } catch (err) {
    console.error(`check-doc-issue-refs: cannot query GitHub via gh (${err.message.split('\n')[0]})`);
    process.exitCode = 2;
    return;
  }
  const docs = await loadDocs(parseDocsDir(process.argv.slice(2)));
  const { docViolations, danglingSuperseded } = findViolations(docs, superseded);

  for (const n of danglingSuperseded) {
    console.error(`  issue #${n} is labeled '${SUPERSEDED_LABEL}' but has no 'Superseded by #N' pointer`);
  }
  for (const v of docViolations) {
    console.error(`  ${v.file} cites superseded #${v.issue} without its superseder ${v.terminals.map((t) => `#${t}`).join(' / ')}`);
  }
  const total = docViolations.length + danglingSuperseded.length;
  if (total > 0) {
    console.error(`\ncheck-doc-issue-refs: ${total} violation(s). Cite the terminal issue alongside (or instead of) the superseded one.`);
    process.exitCode = 1;
    return;
  }
  console.log(`check-doc-issue-refs: OK (${docs.length} docs, ${superseded.size} superseded issue(s))`);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((err) => {
    console.error(err);
    process.exitCode = 1;
  });
}
