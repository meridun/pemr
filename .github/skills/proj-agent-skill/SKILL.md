---
name: proj-agent-skill
description: Create and maintain Agent Skills for this repository. Use when adding new skills, updating existing skills, or registering skills in copilot-instructions.md.
---

## When to Create a New Skill

Create a skill when you identify a **repeatable pattern** that:
- Requires specific, detailed instructions that would bloat copilot-instructions.md
- Applies to multiple future tasks (not one-off situations)
- Has clear "when to use" criteria based on user requests or task types
- Benefits from examples, templates, or reference code

**Don't create a skill for:**
- Simple one-liner rules (put these in copilot-instructions.md)
- Highly specific one-time tasks
- Information that changes frequently (link to docs instead)

## Skill File Structure

Each skill lives in its own directory under `.github/skills/`:

```
.github/skills/
└── skill-name/
    ├── SKILL.md           # Required: Main instructions
    └── examples/          # Optional: Reference files, templates
```

### SKILL.md Format

Frontmatter (`name`, `description`) then `## When to Use` / `## When NOT to Use` / domain sections
/ `## Examples` / `## References`.

### Naming Conventions

- **Directory name**: `proj-<descriptive-name>` (lowercase, hyphens) — swap `proj-` for your own
  project prefix
- **Name in frontmatter**: Must match directory name exactly
- **Description**: Start with verb phrase, include "Use when..." trigger

## Registering a Skill

Skills are **auto-discovered** from `.github/skills/` when they contain a valid `SKILL.md` with
proper frontmatter, and the skill list (name + description) is **auto-injected into every
request** for both Copilot and Claude Code. Do **not** add a skill list or routing table to
copilot-instructions.md — the frontmatter `description` is the trigger.

After creating a skill:

### 1. Compound Tasks row (only if part of a multi-skill sequence)

If the skill belongs in a multi-skill build sequence, add or extend a row in the
copilot-instructions.md **Compound Tasks** table. Standalone skills need no L1 entry at all.

### 2. Sync to Claude Code

`.github/skills/` and `.github/agents/` are the canonical sources. Claude Code only discovers
skills/agents under `.claude/`, so after creating or editing a skill or agent, run:

```
npm run sync:claude-config
```

This mirrors `.github/skills/**` into `.claude/skills/**` as-is (frontmatter format is shared)
and regenerates `.claude/agents/*.md` from `.github/agents/*.agent.md` with Claude Code-compatible
frontmatter (`name`, `description`, mapped `tools`). **Never hand-edit files under
`.claude/skills/` or `.claude/agents/`** — they are generated and will be overwritten. Use
`npm run sync:claude-config:check` to verify the mirrors are up to date (e.g. in CI).

### 3. Meta-drift guard (CI)

`npm run check:meta-drift` (`scripts/check-meta-drift.mjs`) mechanically fails on structural drift
that the sync check can't catch:

- an `.github/agents/*.agent.md` body over 70 lines — agents are **shims**, content lives in
  skills;
- a `proj-*` name referenced in `.github/agents/*.agent.md` or `copilot-instructions.md` that
  resolves to **no** `.github/skills/<name>/SKILL.md` **or** `.github/agents/<name>.agent.md` (a
  dangling reference);
- a `SKILL.md` over the 400-line L2 cap below.

It is dumb by design (line counts + regex, no markdown parsing), so an illustrative `proj-*` token
written in prose must still resolve to a real skill/agent or be reworded.

## Writing Effective Skill Content

### Be Concise but Complete

- Include enough detail for autonomous task completion
- Avoid duplicating information available in linked docs
- Use code examples liberally — they're more precise than prose

### Include "Gotchas"

Document non-obvious issues discovered during debugging — real bugs, not hypothetical ones.

### Link Don't Duplicate

Summarize an L3 doc's relevant points and link to it, instead of pasting its content.

## Maintaining Skills

### When to Update

- After debugging sessions that reveal undocumented patterns
- When architecture changes invalidate existing patterns
- When users repeatedly make the same mistakes

### Avoiding Bloat

Skills should stay **focused and scannable**. If a skill exceeds ~400 lines:
1. Split into multiple focused skills
2. Move detailed examples to `examples/` subdirectory
3. Link to external documentation for stable reference material

### Review Checklist for Skill Changes

- [ ] Frontmatter `name` matches directory name
- [ ] Description includes "Use when..." trigger
- [ ] "When to Use" section has clear, specific criteria
- [ ] Code examples are tested and correct
- [ ] Cross-references to other skills/docs are valid
- [ ] `npm run sync:claude-config` run; Compound Tasks row added if the skill joins a sequence

## Documentation Tier Model (L1/L2/L3)

Skills are the L2 tier in the AI documentation cache hierarchy — see the canonical tier/budget
table in [proj-doc-tiers](../proj-doc-tiers/SKILL.md#artifacts-in-scope).

### Tier Routing Rules

1. **L1 → L2**: Skill descriptions are auto-injected every request; the Compound Tasks table in
   `copilot-instructions.md` maps multi-skill task sequences.
2. **L2 → L3**: Every skill's Cross References section links to relevant `docs/` pages.
3. **L1 → L3 (fallback)**: For topics without a matching skill, L1 points to `docs/Overview.md`
   and `docs/Architecture.md`.

### When to Promote Content Between Tiers

- **L3 → L2**: When a pattern is needed frequently enough to justify on-demand loading, extract
  it into a skill with concise guidance. Keep the detailed reference in L3 and link to it.
- **L2 → L1**: When a rule applies to nearly every request, promote a one-liner to L1. Never
  promote detailed patterns — keep L1 lean.
- **L1 → L2**: If an L1 section grows beyond a few bullet points, extract it into a skill and
  replace with a routing entry.

## References

- [GitHub Agent Skills Documentation](https://docs.github.com/en/copilot/concepts/agents/about-agent-skills)
- [copilot-instructions.md](../../copilot-instructions.md)
- [docs/Documentation.md](../../../docs/Documentation.md) — L3 content governance
