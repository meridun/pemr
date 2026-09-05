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
 *   2. Dangling `pemr-*` references. Any `pemr-<name>` mentioned in the lane
 *      prompts, agent shims, or copilot-instructions must resolve to a real
 *      `.github/skills/<name>/SKILL.md` OR a `.github/agents/<name>.agent.md`
 *      (finding #2/#3 — a prompt hard-depending on a skill that isn't there).
 *   3. SKILL.md files exceeding the L2 length cap from `pemr-agent-skill`.
 *   4. The Caveman-mode hook drifting from its canonical L1 text. The
 *      `## Caveman mode` section of copilot-instructions.md is the source of
 *      truth; the `UserPromptSubmit` hook in `.claude/settings.json` must carry
 *      the same paragraph verbatim (whitespace-normalised).
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
const SDLC_DIR = path.join(ROOT, 'sdlc');
const SDLC_LANES_DIR = path.join(SDLC_DIR, 'lanes');
const COPILOT_INSTRUCTIONS = path.join(ROOT, '.github', 'copilot-instructions.md');
const CLAUDE_SETTINGS = path.join(ROOT, '.claude', 'settings.json');

// Agent files are shims; the largest legitimate shim body today is ~54 lines
// (pemr-researcher). A content-copy drift blows well past this. Headroom
// keeps the check green on current files while still catching the drift class.
export const AGENT_BODY_LINE_CAP = 70;

// L2 length cap declared by the pemr-agent-skill skill.
export const SKILL_LINE_CAP = 400;

// Matches an pemr-* reference token. Trailing punctuation (backticks, commas,
// slashes for possessive `pemr-x/y`) is excluded by the char class.
const REF_RE = /pemr-[a-z0-9]+(?:-[a-z0-9]+)*/g;

// Tokens that match REF_RE but are not skill/agent references: `pemr-wt` is
// the issue-worktree path prefix (`C:\Claude\pemr-wt-<issue>`).
const IGNORED_REFS = new Set(['pemr-wt']);

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

/** Extract the unique set of pemr-* reference tokens from text. */
export function extractRefs(text) {
  const refs = (text.match(REF_RE) ?? []).filter(
    (ref) => !IGNORED_REFS.has(ref) && ![...IGNORED_REFS].some((ig) => ref.startsWith(`${ig}-`)),
  );
  return new Set(refs);
}

const CAVEMAN_PREFIX = 'Caveman mode: ';

function normalizeWs(text) {
  return text.replace(/\s+/g, ' ').trim();
}

/** Body of the `## Caveman mode` section in L1 (up to the next `## ` heading), or null. */
export function extractCavemanSection(l1Text) {
  const m = l1Text.match(/^## Caveman mode\r?\n([\s\S]*?)(?=^## |(?![\s\S]))/m);
  return m ? m[1] : null;
}

/** The rule text the UserPromptSubmit hook injects (after the `Caveman mode: ` prefix), or null. */
export function extractCavemanHookRule(settingsJson) {
  const hooks = settingsJson?.hooks?.UserPromptSubmit ?? [];
  for (const group of hooks) {
    for (const hook of group.hooks ?? []) {
      const cmd = hook.command ?? '';
      const idx = cmd.indexOf(CAVEMAN_PREFIX);
      if (idx === -1) {
        continue;
      }
      // Payload is a JSON string literal inside an echo; take up to the closing quote.
      const rest = cmd.slice(idx + CAVEMAN_PREFIX.length);
      const end = rest.indexOf('"');
      return end === -1 ? rest : rest.slice(0, end);
    }
  }
  return null;
}

/** True when the hook rule text appears verbatim (whitespace-normalised) in the L1 section. */
export function cavemanHookMatchesL1(l1Text, settingsJson) {
  const section = extractCavemanSection(l1Text);
  const rule = extractCavemanHookRule(settingsJson);
  if (section === null || rule === null) {
    return false;
  }
  return normalizeWs(section).includes(normalizeWs(rule));
}

async function readDirEntries(dir) {
  try {
    return await fs.readdir(dir, { withFileTypes: true });
  } catch {
    return [];
  }
}

/** Set of valid pemr-* names: skill directory names + agent file basenames. */
async function collectKnownNames() {
  const known = new Set();

  for (const entry of await readDirEntries(SKILLS_DIR)) {
    if (entry.isDirectory() && entry.name.startsWith('pemr-')) {
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

/** Files whose pemr-* references must all resolve. */
async function referenceSources() {
  const files = [
    ...(await listFiles(SDLC_DIR, (n) => n.endsWith('.md'))),
    ...(await listFiles(SDLC_LANES_DIR, (n) => n.endsWith('.md'))),
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

  // Rule 2: dangling pemr-* references.
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

  // Rule 4: caveman hook text == L1 canonical text.
  try {
    const l1 = await fs.readFile(COPILOT_INSTRUCTIONS, 'utf8');
    const settings = JSON.parse(await fs.readFile(CLAUDE_SETTINGS, 'utf8'));
    if (extractCavemanHookRule(settings) !== null && !cavemanHookMatchesL1(l1, settings)) {
      errors.push(
        '.claude/settings.json: the Caveman-mode UserPromptSubmit hook text is not present verbatim in ' +
          'the `## Caveman mode` section of .github/copilot-instructions.md. L1 is canonical; update the hook.',
      );
    }
  } catch {
    // Either file absent or unparsable — nothing to compare.
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
