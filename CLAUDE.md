@.github/copilot-instructions.md

# Claude Code Notes

The instructions above are shared with VS Code Copilot via `.github/copilot-instructions.md`.

## Skills and Agents

- The `proj-*` skills and subagents referenced above live as the canonical source in
  `.github/skills/` and `.github/agents/*.agent.md`, and are mirrored into `.claude/skills/` and
  `.claude/agents/*.md` (with Claude Code-compatible frontmatter) so Claude Code can discover them.
- **Do not hand-edit `.claude/skills/` or `.claude/agents/`** — edit the `.github/` source and run
  `npm run sync:claude-config` (or `npm run sync:claude-config:check` to verify they're already in
  sync). CI enforces this.
- Invoke subagents via the `Agent` tool with `subagent_type` set to the agent name.
- The role agents (`scout`, `Explore`, `mech-executor`, `executor`, `verifier`,
  `security-executor`) carry `model`/`effort` frontmatter so delegated work runs on the cheapest
  adequate tier — see `docs/Development_ModelRouting.md` and the `## Orchestration` policy in the
  shared instructions above.

## graphify

The `## graphify` section in `.github/copilot-instructions.md` (included above) covers the
query-first workflow shared with Copilot. Claude Code additionally gets a `.claude/settings.json`
`PreToolUse` hook that nudges toward `graphify query`/`explain`/`path` before grep or raw file
reads once `graphify-out/graph.json` exists.

## Token wrappers

If you wire up `vtk` (see `docs/Development_TokenTools.md`), document the routing rule here so
Claude Code bash sessions know which commands are wrapped and which to prefix explicitly.
