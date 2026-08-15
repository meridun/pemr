"""PreToolUse hook: stop an agent publishing a real identity to GitHub.

The repo-side hooks (.githooks/) cover commits and pushes, and CI covers the
tree and the PR body. None of them see an agent calling `gh pr create` or
`gh issue comment` straight from a shell — which is exactly how the identities
this guard exists for got republished three times in one session, each time in
the act of *describing* the leak.

Blocks a Bash command when the command text itself carries a patient identity.
Delegates every pattern decision to scripts/pii-scan.mjs so there is one source
of truth, not a Python reimplementation that drifts.

Fails open on any internal error: a broken hook must not wedge the session.
"""
import json
import os
import re
import shutil
import subprocess
import sys

SCANNER = os.path.join("scripts", "pii-scan.mjs")

# Only gate commands that actually publish. Reads (`gh pr view`, `git log`)
# legitimately surface the old content and must stay usable for remediation.
PUBLISHING = re.compile(
    r"""\b(
        gh\s+(issue|pr)\s+(create|comment|edit|review)
      | gh\s+api\b(?=.*-X\s*(POST|PATCH|PUT))
      | git\s+commit\b
      | git\s+tag\b(?!.*-l)
    )""",
    re.VERBOSE | re.IGNORECASE,
)


def allow():
    sys.exit(0)


def deny(reason):
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    sys.exit(0)


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        allow()

    if payload.get("tool_name") != "Bash":
        allow()

    command = (payload.get("tool_input") or {}).get("command") or ""
    if not command or not PUBLISHING.search(command):
        allow()

    cwd = payload.get("cwd") or os.getcwd()
    scanner = os.path.join(cwd, SCANNER)
    if not os.path.exists(scanner):
        allow()

    node = shutil.which("node")
    if not node:
        allow()

    try:
        proc = subprocess.run(
            [node, scanner, "--stdin", "this command"],
            input=command,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=15,
        )
    except Exception:
        allow()

    if proc.returncode == 0:
        allow()

    detail = (proc.stderr or proc.stdout or "").strip()
    deny(
        "Blocked: this command would publish a real identity to a public repo.\n\n"
        f"{detail}\n\n"
        "AGENTS.md §\"Privacy posture\": describe the shape, never the value. "
        "Rewrite the message/body without the identifier, or use a roster persona."
    )


if __name__ == "__main__":
    main()
