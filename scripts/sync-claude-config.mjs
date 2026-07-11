#!/usr/bin/env node
/**
 * Mirrors AI agent configuration from the GitHub Copilot layout (.github/) into the
 * Claude Code layout (.claude/), since Claude Code only discovers skills/agents under
 * .claude/.
 *
 * - .github/skills/**          -> .claude/skills/**            (copied as-is; frontmatter
 *                                                                 format is already shared)
 * - .github/agents/*.agent.md  -> .claude/agents/*.md          (frontmatter rewritten for
 *                                                                 Claude Code subagents)
 *
 * Usage:
 *   node scripts/sync-claude-config.mjs          # write mirrored files
 *   node scripts/sync-claude-config.mjs --check  # exit 1 if mirrors are out of date
 */

import { promises as fs } from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const SRC_SKILLS = path.join(ROOT, '.github', 'skills');
const DEST_SKILLS = path.join(ROOT, '.claude', 'skills');
const SRC_AGENTS = path.join(ROOT, '.github', 'agents');
const DEST_AGENTS = path.join(ROOT, '.claude', 'agents');

const CHECK_MODE = process.argv.includes('--check');

// Frontmatter fields copied verbatim to the Claude Code agent. Copilot ignores
// them in the source .agent.md; on the Claude side model/effort pin the role to
// a cost tier and disallowedTools hard-blocks tools (e.g. a read-only verifier).
const PASSTHROUGH_FIELDS = ['model', 'effort', 'disallowedTools'];

// Maps Copilot's short tool names to Claude Code tool names.
const TOOL_MAP = {
  read: ['Read'],
  search: ['Grep', 'Glob'],
  agent: ['Agent'],
  edit: ['Edit', 'Write'],
  execute: ['Bash'],
  'mcp_github/*': ['Bash'],
};

let outOfDate = false;

async function listFilesRecursive(dir) {
  const entries = await fs.readdir(dir, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      files.push(...(await listFilesRecursive(full)));
    } else {
      files.push(full);
    }
  }
  return files;
}

async function writeIfChanged(destPath, content) {
  let existing = null;
  try {
    existing = await fs.readFile(destPath, 'utf8');
  } catch {
    // destination doesn't exist yet
  }

  if (existing === content) {
    return;
  }

  const relPath = path.relative(ROOT, destPath);
  if (CHECK_MODE) {
    console.error(`Out of date: ${relPath}`);
    outOfDate = true;
    return;
  }

  await fs.mkdir(path.dirname(destPath), { recursive: true });
  await fs.writeFile(destPath, content, 'utf8');
  console.log(`Wrote ${relPath}`);
}

async function syncSkills() {
  const files = await listFilesRecursive(SRC_SKILLS);
  for (const srcPath of files) {
    const relPath = path.relative(SRC_SKILLS, srcPath);
    const destPath = path.join(DEST_SKILLS, relPath);
    const content = await fs.readFile(srcPath, 'utf8');
    await writeIfChanged(destPath, content);
  }
}

function parseFrontmatter(raw) {
  const match = raw.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n([\s\S]*)$/);
  if (!match) {
    throw new Error('Agent file is missing frontmatter');
  }
  const [, frontmatter, body] = match;
  const fields = {};
  for (const line of frontmatter.split(/\r?\n/)) {
    const fieldMatch = line.match(/^([a-zA-Z0-9_-]+):\s*(.*)$/);
    if (fieldMatch) {
      fields[fieldMatch[1]] = fieldMatch[2].trim();
    }
  }
  return { fields, body };
}

function mapTools(toolsValue) {
  // toolsValue looks like "[read, search, agent, mcp_github/*]"
  const inner = toolsValue.replace(/^\[|\]$/g, '');
  const names = inner
    .split(',')
    .map((name) => name.trim())
    .filter(Boolean);

  const mapped = new Set();
  for (const name of names) {
    for (const claudeTool of TOOL_MAP[name] ?? []) {
      mapped.add(claudeTool);
    }
  }
  return [...mapped].join(', ');
}

function stripQuotes(value) {
  return value.replace(/^"(.*)"$/, '$1');
}

async function syncAgents() {
  const entries = await fs.readdir(SRC_AGENTS, { withFileTypes: true });
  for (const entry of entries) {
    if (!entry.isFile() || !entry.name.endsWith('.agent.md')) {
      continue;
    }

    const name = entry.name.replace(/\.agent\.md$/, '');
    const srcPath = path.join(SRC_AGENTS, entry.name);
    const raw = await fs.readFile(srcPath, 'utf8');
    const { fields, body } = parseFrontmatter(raw);

    const description = stripQuotes(fields.description ?? '');
    const tools = fields.tools ? mapTools(fields.tools) : '';

    const frontmatterLines = [`name: ${name}`, `description: ${JSON.stringify(description)}`];
    if (tools) {
      frontmatterLines.push(`tools: ${tools}`);
    }
    for (const field of PASSTHROUGH_FIELDS) {
      if (fields[field]) {
        frontmatterLines.push(`${field}: ${stripQuotes(fields[field])}`);
      }
    }

    const content = `---\n${frontmatterLines.join('\n')}\n---\n${body}`;
    const destPath = path.join(DEST_AGENTS, `${name}.md`);
    await writeIfChanged(destPath, content);
  }
}

await syncSkills();
await syncAgents();

if (CHECK_MODE && outOfDate) {
  console.error('\n.claude/ mirrors are out of date. Run `npm run sync:claude-config`.');
  process.exit(1);
}
