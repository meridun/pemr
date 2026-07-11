---
name: proj-doc-tiers
description: Harvest session learnings into the documentation tier system and audit/reorganize the tiers (L1 copilot-instructions, L2 skills, L3 docs, agents, memory). Use when a session ends with discoverable information ("harvest this session", "update the doc tiers", "capture what we learned"), or when auditing, rebalancing, or reorganizing the documentation system.
---

# proj-doc-tiers

Harvest session learnings into the documentation tier system, and audit/reorganize the tiers.
This skill owns the tier **system** — placement, routing, budgets, freshness, and the
memory-vs-docs boundary. Per-artifact authoring conventions stay in
[proj-agent-skill](../proj-agent-skill/SKILL.md) — load it on demand when applying changes; do
not duplicate its content here.

## When to Use

- End of a session that surfaced reusable knowledge: new patterns, gotchas, architectural facts,
  workflow corrections — "harvest this", "capture what we learned", "update the doc tiers"
- Explicit audit or reorganization of the tier system: "audit the doc tiers", "is this in the
  right tier?", "the skills feel bloated"
- Deciding where a piece of knowledge belongs (which tier, or memory instead)

## When NOT to Use

- Authoring or editing a single L3 doc for a feature → do it directly, following
  `docs/Documentation.md`
- Creating/updating one skill or agent with known placement → `proj-agent-skill`
- Recording something only relevant to the current conversation → nowhere; let it go

## Artifacts in Scope

| Tier / artifact | Location | When Loaded | Budget |
|---|---|---|---|
| L1 | `.github/copilot-instructions.md` (+ `CLAUDE.md` @-include) | Every request | ≤100 lines |
| L2 skills | `.github/skills/*/SKILL.md` | On demand (description match) | ≤400 lines |
| L3 docs | `docs/*.md` | Explicitly read | Unlimited, governed by `docs/Documentation.md` |
| Agents | `.github/agents/*.agent.md` | Per spawn | ≤70 lines — shims, content lives in skills |
| SDLC prompts | `prompts/sdlc/*.md` (if adopted) | Per lane pass | Reference skills/docs, never inline copies |
| Claude memory | `~/.claude/projects/<project>/memory/` | Index every session | One fact per file |

## Core Principle: Every Token Must Earn Its Tier

Expected cost of a piece of content = **size × load frequency of its tier**. An L1 line is paid on
every request; an L2 line only when its skill fires; an L3 line only on explicit read. Evaluate
every finding through four lenses, in order:

1. **Freshness** — is it still true? Verify referenced paths/names against the codebase. Stale
   content is negative-value at any tier: fix or delete before considering placement.
2. **Placement** — is it at the *cheapest* tier that still gets discovered in time? Push down
   (L1→L2→L3) by default; promote up only when needed on nearly every request (one-liner to L1)
   or frequently enough to justify a skill (L3→L2 summary + link).
3. **Routing** — placement's dual: pushing content down is only safe if something reaches it.
   Check skill descriptions trigger correctly and L2→L3 links are valid.
4. **Budget** — measure against the caps above (`wc -l`, tokens ≈ chars/4). Rank findings by
   token impact: L1 savings outrank L2, which outrank L3.

## Mode 1: Harvest (default)

Invoked at session end. Extract what the session learned and route each piece to its home.

1. **Extract** — sweep the session for candidate learnings: patterns discovered, gotchas debugged,
   architecture facts established, corrections the user made, decisions with rationale, stale docs
   noticed in passing. Discard anything conversation-local.
2. **Classify** each candidate:
   - Fresh, uncertain, or user/workflow-specific → **memory**
   - Verified + broadly useful → repo tier by expected access frequency (lens 2 above)
   - Invalidates existing content → **staleness fix** at whatever tier it lives
   - Also check existing memories touched this session: verified and broadly useful now →
     **promote to L3** and shrink the memory to a pointer
3. **Report** — table: finding → target artifact → action (create/update/delete/promote) →
   rationale (one line). Get approval before editing.
4. **Apply** — load `proj-agent-skill` for skill/agent edits. Order: L3 first, L2 next, L1 last
   and only if routing changed.
5. **Verify** — `npm run sync:claude-config && npm run check:meta-drift` (if adopted).

## Mode 2: Audit (explicit request)

Full-system sweep, run occasionally or scoped on request ("audit just the skills").

1. **Inventory** — sizes and budgets for every artifact in scope (table above).
2. **Apply the four lenses** per artifact. Cross-cutting checks: content duplicated across tiers
   (keep one home, link the rest); agents embedding knowledge that belongs in skills; skills
   reproducing L3 content instead of linking; L1 sections grown past a few bullets.
3. **Report** — findings ranked by token impact, each with proposed action and estimated
   savings. Get approval.
4. **Persist** — approved findings become tracked work items (issue/ticket per your tracker).
   Small approved fixes may apply in-session.

## Rules

- **Report → approve → apply.** Never restructure tiers unprompted.
- Never hand-edit `.claude/skills/` or `.claude/agents/` — edit `.github/` sources and sync.
- Link, don't duplicate: a summary line + link beats a copied section, always.
- No git operations unless explicitly requested.

## Cross References

- [proj-agent-skill](../proj-agent-skill/SKILL.md) — skill/agent authoring, promotion rules detail
- [docs/Documentation.md](../../../docs/Documentation.md) — L3 content governance
