#!/usr/bin/env node
/**
 * Meta-drift guard for AI agent/skill/lane-prompt config.
 *
 * Catches the mechanical-detectable drift class that `sync:claude-config:check`
 * does not (see issue #490 / the #454 meta-audit follow-up):
 *
 *   1. Agent shims growing back into content-copies. `.github/agents/*.agent.md`
 *      are shims — the real content lives in skills. If a body balloons past the
 *      cap it is almost certainly a restated skill table (finding #1).
 *   2. Dangling `proj-*` references. Any `proj-<name>` mentioned in the lane
 *      prompts, agent shims, or copilot-instructions must resolve to a real
 *      `.github/skills/<name>/SKILL.md` OR a `.github/agents/<name>.agent.md`
 *      (finding #2/#3 — a prompt hard-depending on a skill that isn't there).
 *   3. SKILL.md files exceeding the L2 length cap from `proj-agent-skill`.
 *
 * Intentionally dumb: line counts + a reference regex, no markdown parsing.
 *
 * Usage:
 *   node scripts/check-meta-drift.mjs   # exit 1 if any drift is detected
 */

import { promises as fs } from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const SKILLS_DIR = path.join(ROOT, '.github', 'skills');
const AGENTS_DIR = path.join(ROOT, '.github', 'agents');
const SDLC_PROMPTS_DIR = path.join(ROOT, 'prompts', 'sdlc');
const COPILOT_INSTRUCTIONS = path.join(ROOT, '.github', 'copilot-instructions.md');

// Agent files are shims; the largest legitimate shim body today is ~54 lines
// (proj-researcher). A content-copy drift blows well past this. Headroom
// keeps the check green on current files while still catching the drift class.
export const AGENT_BODY_LINE_CAP = 70;

// L2 length cap declared by the proj-agent-skill skill.
export const SKILL_LINE_CAP = 400;

// Matches an proj-* reference token. Trailing punctuation (backticks, commas,
// slashes for possessive `proj-x/y`) is excluded by the char class.
const REF_RE = /proj-[a-z0-9]+(?:-[a-z0-9]+)*/g;

/** Count body lines of an agent file (everything after the closing frontmatter `---`). */
export function agentBodyLineCount(raw) {
  const match = raw.match(/^---\r?\n[\s\S]*?\r?\n---\r?\n([\s\S]*)$/);
  const body = match ? match[1] : raw;
  // Drop a single trailing newline so a file ending in "\n" isn't counted as an extra blank line.
  const normalized = body.replace(/\r?\n$/, '');
  if (normalized === '') {
    return 0;
  }
  return normalized.split(/\r?\n/).length;
}

/** Extract the unique set of proj-* reference tokens from text. */
export function extractRefs(text) {
  return new Set(text.match(REF_RE) ?? []);
}

async function readDirEntries(dir) {
  try {
    return await fs.readdir(dir, { withFileTypes: true });
  } catch {
    return [];
  }
}

/** Set of valid proj-* names: skill directory names + agent file basenames. */
async function collectKnownNames() {
  const known = new Set();

  for (const entry of await readDirEntries(SKILLS_DIR)) {
    if (entry.isDirectory() && entry.name.startsWith('proj-')) {
      known.add(entry.name);
    }
  }
  for (const entry of await readDirEntries(AGENTS_DIR)) {
    if (entry.isFile() && entry.name.endsWith('.agent.md')) {
      known.add(entry.name.replace(/\.agent\.md$/, ''));
    }
  }
  return known;
}

async function listFiles(dir, filter) {
  return (await readDirEntries(dir))
    .filter((e) => e.isFile() && filter(e.name))
    .map((e) => path.join(dir, e.name));
}

/** Files whose proj-* references must all resolve. */
async function referenceSources() {
  const files = [
    ...(await listFiles(SDLC_PROMPTS_DIR, (n) => n.endsWith('.md'))),
    ...(await listFiles(AGENTS_DIR, (n) => n.endsWith('.agent.md'))),
  ];
  try {
    await fs.access(COPILOT_INSTRUCTIONS);
    files.push(COPILOT_INSTRUCTIONS);
  } catch {
    // copilot-instructions.md absent — skip.
  }
  return files;
}

async function main() {
  const errors = [];

  // Rule 1: agent shim body size.
  for (const file of await listFiles(AGENTS_DIR, (n) => n.endsWith('.agent.md'))) {
    const raw = await fs.readFile(file, 'utf8');
    const lines = agentBodyLineCount(raw);
    if (lines > AGENT_BODY_LINE_CAP) {
      errors.push(
        `${path.relative(ROOT, file)}: agent body is ${lines} lines (cap ${AGENT_BODY_LINE_CAP}). ` +
          `Agents are shims — move content into the matching skill.`,
      );
    }
  }

  // Rule 2: dangling proj-* references.
  const known = await collectKnownNames();
  for (const file of await referenceSources()) {
    const text = await fs.readFile(file, 'utf8');
    for (const ref of extractRefs(text)) {
      if (!known.has(ref)) {
        errors.push(
          `${path.relative(ROOT, file)}: references "${ref}" but no ` +
            `.github/skills/${ref}/SKILL.md or .github/agents/${ref}.agent.md exists.`,
        );
      }
    }
  }

  // Rule 3: SKILL.md length cap.
  for (const entry of await readDirEntries(SKILLS_DIR)) {
    if (!entry.isDirectory()) {
      continue;
    }
    const skillPath = path.join(SKILLS_DIR, entry.name, 'SKILL.md');
    let raw;
    try {
      raw = await fs.readFile(skillPath, 'utf8');
    } catch {
      continue; // no SKILL.md in this dir
    }
    const lines = raw.replace(/\r?\n$/, '').split(/\r?\n/).length;
    if (lines > SKILL_LINE_CAP) {
      errors.push(
        `${path.relative(ROOT, skillPath)}: ${lines} lines exceeds the L2 cap of ${SKILL_LINE_CAP}.`,
      );
    }
  }

  if (errors.length > 0) {
    console.error('Meta-drift check failed:\n');
    for (const err of errors) {
      console.error(`  - ${err}`);
    }
    console.error('\nSee scripts/check-meta-drift.mjs for the rules.');
    process.exit(1);
  }

  console.log('Meta-drift check passed.');
}

// Only run when invoked directly, so tests can import the pure helpers.
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await main();
}
