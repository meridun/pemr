# Documentation.md — L3 content governance

L3 is `docs/*.md`: unlimited length, read explicitly (not auto-loaded). Governs architecture,
deep reference, decisions with lasting rationale.

## Conventions

- **Hub-and-spoke**: `Overview.md` and `Architecture.md` are entry points that link out to
  focused sub-docs (`Architecture_<Subsystem>.md`), rather than one giant file.
- **One concept per file.** If a doc mixes two unrelated subsystems, split it.
- **Link, don't duplicate** across L1/L2/L3 — a summary + link beats a copy that goes stale.
- **Freshness**: when code changes invalidate a doc, fix it in the same PR, or file a follow-up.

## L3 = current state; GitHub Issues = history

- Docs describe **current** behaviour and cite the issue that established it. No per-issue
  headings, no "revised / superseding" narrative — that history already lives in the issues, and
  replaying it in a doc is how L3 files bloat past usefulness.
- GitHub Issues are the canonical record for closed work **unless the issue was superseded**. When
  a later issue replaces an earlier one's design, apply the `superseded` label to the old issue
  and close it with the comment `Superseded by #N` (exact phrase — the guard parses it):
  `gh issue edit <old> --add-label superseded` then
  `gh issue close <old> --comment "Superseded by #<new>"`.
  Then grep `docs/` for `#<old>` and cite `#<new>` alongside it in the same PR.
- A doc citing a superseded issue must also cite its **terminal** superseder (chains are
  followed). `npm run check:doc-issue-refs` (`scripts/check-doc-issue-refs.mjs`, needs `gh`)
  enforces this in CI and also flags a `superseded` label with no `Superseded by #N` pointer.
  `--docs-dir <dir>` scans a directory other than `docs/`.

## Placement decision

New knowledge goes here (L3) when it's: broadly useful, verified (not a hunch), and either too
detailed for a skill (L2) or not needed often enough to justify auto-loading. See
[pemr-doc-tiers](../.github/skills/pemr-doc-tiers/SKILL.md) for the full four-lens placement
process.

## Suggested starting entry points

- `Overview.md` — what the system is, for a newcomer
- `Architecture.md` — hub linking to subsystem docs
- `Development.md` — local setup, test commands, branch model
- `Development_AgenticSDLC.md` — the Agentic SDLC model and invariants (ported from upstream);
  `Development_SdlcAdoption.md` (copy the tree, pick the binding, fill the profile), `Development_SdlcComposability.md`
  (9-stage canonical spine), `Development_SdlcProfileExample.md` (worked `gh-issue` profile);
  pemr's own profile is `sdlc/PROFILE.md` and the label taxonomy lives in `sdlc/bindings/gh-issue/labels.md`
- `Development_TokenTools.md` — vtk/graphify setup notes, if adopted
