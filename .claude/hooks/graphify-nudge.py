"""PreToolUse hook: nudge toward graphify before raw source exploration.

Fires for Bash (grep/find-style commands) and Read/Glob/Grep on source files,
but only when graphify-out/graph.json exists, only for paths inside this repo,
and at most once per 30 minutes (marker file TTL) — after the first nudge the
instruction is already in context; repeats are pure token cost.
"""
import json
import os
import re
import sys
import time

MARKER = "graphify-out/.nudge-marker"
TTL_SECONDS = 1800
SOURCE_EXTS = (
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb",
    ".c", ".h", ".cpp", ".hpp", ".cc", ".cs", ".kt", ".swift", ".php",
    ".scala", ".lua", ".sh", ".md", ".rst", ".txt", ".mdx",
)
MESSAGE = (
    "MANDATORY: graphify-out/graph.json exists. Run `graphify query \"<question>\"` "
    "(or `graphify explain` / `graphify path`) before exploring raw source files. "
    "Only read/grep raw files after graphify has oriented you, or to modify/debug "
    "specific lines. This rule applies to subagents too — include it in every "
    "subagent prompt involving code exploration."
)


def is_hit(data):
    tool = data.get("tool_name", "")
    tool_input = data.get("tool_input") or {}
    if tool == "Bash":
        cmd = str(tool_input.get("command", ""))
        return bool(re.search(r"\b(grep|rg|ripgrep|find|fd|ack|ag)\b", cmd))
    # Read / Glob / Grep: only source-file extensions (endswith — '.json' must
    # NOT match '.js'), only paths inside this repo.
    cwd = os.getcwd().lower().replace("\\", "/")
    for field in ("file_path", "path", "pattern"):
        value = tool_input.get(field)
        if not value:
            continue
        s = str(value).lower().replace("\\", "/")
        if "graphify-out/" in s:
            continue
        # Absolute path outside the repo (settings, scheduled tasks, other
        # projects) → not our business.
        if (":" in s[:3] or s.startswith("/")) and not s.startswith(cwd):
            continue
        if s.rstrip("$/*").endswith(SOURCE_EXTS):
            return True
    return False


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    if not os.path.isfile("graphify-out/graph.json"):
        return
    try:
        if time.time() - os.path.getmtime(MARKER) < TTL_SECONDS:
            return  # nudged recently; stay silent
    except OSError:
        pass
    if not is_hit(data):
        return
    try:
        with open(MARKER, "w"):
            pass
    except OSError:
        pass
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": MESSAGE,
        }
    }))


if __name__ == "__main__":
    main()
