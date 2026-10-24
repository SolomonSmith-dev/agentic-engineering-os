"""PreToolUse hook: block any tool call that names a protected path. Fails closed.

Claude Code runs this before every tool call with the call as JSON on stdin.
Exit 2 blocks the call and shows stderr to the agent; exit 0 lets it through.
Permission deny rules already cover the file tools; this also catches a Bash
command such as `cat .env`, which a `Read(...)` deny rule never sees.

    python -m agent_team.adapters.claude.hook <compiled>/policy.json
"""

import fnmatch
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

BLOCK = 2
FILE_TOOLS = ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit")
SEARCH_TOOLS = ("Glob", "Grep")
# Path-like words in a shell command: everything between shell punctuation and whitespace.
_WORDS = re.compile(r"[^\s\"'`;|&<>()=$,]+")


def _candidates(tool: str, tool_input: dict[str, Any]) -> list[str]:
    if tool in FILE_TOOLS:
        return [str(tool_input.get(k)) for k in ("file_path", "notebook_path") if tool_input.get(k)]
    if tool in SEARCH_TOOLS:
        return [str(tool_input.get(k)) for k in ("path", "pattern", "glob") if tool_input.get(k)]
    if tool == "Bash":
        return _WORDS.findall(str(tool_input.get("command", "")))
    return []


def _expand(candidate: str, home: Path) -> Path:
    if candidate == "~" or candidate.startswith("~/"):
        return home / candidate[2:]
    return Path(candidate)


def protected_match(candidate: str, patterns: list[str], roots: list[Path], home: Path) -> str | None:
    """The pattern `candidate` falls under, or None.

    Patterns follow gitignore shape: `~/...` is under home, a pattern with a
    slash is relative to the innermost root (worktree, then target repository),
    and a bare name such as `.env` or `*.pem` matches that file name anywhere.
    """
    path = _expand(candidate, home)
    absolute = Path(os.path.normpath(path if path.is_absolute() else roots[0] / path))
    # Relative to the first root that holds the path: the worktree lives inside the
    # target repo's .agent-team/, and its own files must not match `.agent-team/**`.
    rels = []
    for root in roots:
        if absolute.is_relative_to(root):
            rels.append(absolute.relative_to(root).as_posix())
            break
    for pattern in patterns:
        glob = pattern.replace("**", "*")
        if glob.startswith("~/"):
            target = home / glob[2:]
            if fnmatch.fnmatch(str(absolute), str(target)) or str(absolute) == str(target).rstrip("/*"):
                return pattern
        elif "/" in pattern.rstrip("/"):
            bare = glob.rstrip("/*")
            if any(fnmatch.fnmatch(rel, glob) or rel == bare or rel.startswith(bare + "/") for rel in rels):
                return pattern
        elif fnmatch.fnmatch(absolute.name, glob):
            return pattern
    return None


def check(event: dict[str, Any], policy: dict[str, Any]) -> str | None:
    """Why the call must be blocked, or None to allow it."""
    tool = str(event.get("tool_name", ""))
    tool_input = event.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return f"{tool}: tool input is not an object"
    roots = [Path(r) for r in policy["roots"]]
    home = Path(policy["home"])
    for candidate in _candidates(tool, tool_input):
        pattern = protected_match(candidate, list(policy["protected_paths"]), roots, home)
        if pattern is not None:
            return f"blocked by agent-team policy: {tool} touches {candidate!r}, protected by {pattern!r}"
    return None


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        policy = json.loads(Path(args[0]).read_text(encoding="utf-8"))
        event = json.loads(sys.stdin.read())
        reason = check(event, policy)
    except Exception as exc:  # noqa: BLE001 - any failure blocks: the hook fails closed
        print(f"blocked by agent-team policy: hook could not evaluate the call ({type(exc).__name__})", file=sys.stderr)
        return BLOCK
    if reason is not None:
        print(reason, file=sys.stderr)
        return BLOCK
    return 0


if __name__ == "__main__":
    sys.exit(main())
