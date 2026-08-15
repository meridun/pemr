#!/usr/bin/env node
// Point git at the versioned hooks in .githooks/.
//
// Hooks in .git/hooks are per-clone and untracked, so they are exactly the
// thing a fresh clone silently lacks. `core.hooksPath` keeps them in the repo
// and under review.
//
//   npm run setup:hooks          # install
//   npm run setup:hooks -- --check   # verify (used by CI)

import { execFileSync } from 'child_process';
import fs from 'fs';
import path from 'path';

const DIR = '.githooks';
const HOOKS = ['commit-msg', 'pre-push'];
const check = process.argv.includes('--check');

function git(args) {
  return execFileSync('git', args, { encoding: 'utf8' }).trim();
}

const missing = HOOKS.filter((h) => !fs.existsSync(path.join(DIR, h)));
if (missing.length) {
  console.error(`setup-hooks: missing hook script(s): ${missing.join(', ')}`);
  process.exit(1);
}

let current = '';
try {
  current = git(['config', '--get', 'core.hooksPath']);
} catch {
  current = '';
}

if (check) {
  if (current !== DIR) {
    console.error(`setup-hooks: core.hooksPath is '${current || '(unset)'}', expected '${DIR}'.`);
    console.error('Run: npm run setup:hooks');
    process.exit(1);
  }
  console.log(`setup-hooks: ok (core.hooksPath=${DIR})`);
  process.exit(0);
}

if (current !== DIR) {
  git(['config', 'core.hooksPath', DIR]);
}

// core.hooksPath ignores the executable bit on Windows, but honours it on
// POSIX — set it where the filesystem supports it.
for (const h of HOOKS) {
  const p = path.join(DIR, h);
  try {
    fs.chmodSync(p, 0o755);
  } catch {
    /* windows: no-op */
  }
}

console.log(`setup-hooks: core.hooksPath=${DIR} (${HOOKS.join(', ')})`);
