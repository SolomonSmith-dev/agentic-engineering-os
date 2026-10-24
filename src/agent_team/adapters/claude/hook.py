"""PreToolUse hook: block any tool call that names a protected path. Fails closed.

Claude Code runs this before every tool call with the call as JSON on stdin.
Exit 2 blocks the call and shows stderr to the agent; exit 0 lets it through.
Permission deny rules already cover the file tools; this also catches a Bash
command such as `cat .env` or `head .en''v`, which a `Read(...)` deny rule
never sees. Bash words are read the way the shell would (quotes and escapes
joined, globs matched against protected names); a command the hook cannot
parse is blocked. It is a tripwire, not a boundary: a process that assembles
a path at run time (`f=.e; cat ${f}nv`) gets past it, and only srt's own
denyRead rules hold against that.

It also confines the file-writing tools to the policy's `write_root` (the
run's worktree): a target that resolves outside it, symlinks followed, is
blocked, on top of the `Edit(./**)` allow rule.

    python -m agent_team.adapters.claude.hook <compiled>/policy.json
"""

import fnmatch
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any

BLOCK = 2
FILE_TOOLS = ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit")
WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
SEARCH_TOOLS = ("Glob", "Grep")
# Path-like words in a shell command: everything between shell punctuation and whitespace.
_WORDS = re.compile(r"[^\s\"'`;|&<>()=$,]+")
# $HOME and ${HOME} become ~ before splitting, so `cat "$HOME/.ssh/id"` matches `~/.ssh/**`.
_HOME_VAR = re.compile(r"\$\{HOME\}|\$HOME\b")


def _candidates(tool: str, tool_input: dict[str, Any]) -> list[str]:
    if tool in FILE_TOOLS:
        return [str(tool_input.get(k)) for k in ("file_path", "notebook_path") if tool_input.get(k)]
    if tool in SEARCH_TOOLS:
        return [str(tool_input.get(k)) for k in ("path", "pattern", "glob") if tool_input.get(k)]
    if tool == "Bash":
        return _shell_words(str(tool_input.get("command", "")))
    return []


class UnparsableCommand(ValueError):
    """The hook cannot split the command the way the shell would; the call is blocked."""


def _shell_words(command: str) -> list[str]:
    """Every word the shell could see, quotes and escapes removed, plus the raw path-like pieces."""
    command = _HOME_VAR.sub("~", command)
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";|&<>()")
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError as exc:
        raise UnparsableCommand(str(exc)) from exc
    words = dict.fromkeys(_WORDS.findall(command))
    for token in tokens:
        words[token] = None
        words.update(dict.fromkeys(_WORDS.findall(token)))
    return list(words)


_GLOB_CHARS = re.compile(r"[*?\[]")


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
        elif _GLOB_CHARS.search(absolute.name) and fnmatch.fnmatch(glob.replace("*", "").replace("?", "x"), absolute.name):
            return pattern  # the shell would expand `.en?` or `.e*` to a protected name
    return None


def check(event: dict[str, Any], policy: dict[str, Any]) -> str | None:
    """Why the call must be blocked, or None to allow it."""
    tool = str(event.get("tool_name", ""))
    tool_input = event.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return f"{tool}: tool input is not an object"
    roots = [Path(r) for r in policy["roots"]]
    home = Path(policy["home"])
    try:
        candidates = _candidates(tool, tool_input)
    except UnparsableCommand as exc:
        return f"blocked by agent-team policy: {tool} command could not be parsed ({exc}); quote it plainly"
    for candidate in candidates:
        pattern = protected_match(candidate, list(policy["protected_paths"]), roots, home)
        if pattern is not None:
            return f"blocked by agent-team policy: {tool} touches {candidate!r}, protected by {pattern!r}"
    write_root = policy.get("write_root")
    if tool in WRITE_TOOLS and write_root is not None:
        for candidate in candidates:
            if not inside(candidate, Path(write_root), home):
                return f"blocked by agent-team policy: {tool} writes {candidate!r}, outside the run's worktree"
            if ".git" in Path(candidate).parts or ".git" in resolved_parts(candidate, Path(write_root), home):
                # The worktree's .git file names its gitdir; repointed, the orchestrator's own git
                # (outside every sandbox) would read a config the agent wrote.
                return f"blocked by agent-team policy: {tool} writes {candidate!r}, inside git's metadata"
    return None


def resolved_parts(candidate: str, root: Path, home: Path) -> tuple[str, ...]:
    """The parts of `candidate` below `root` once symlinks are followed, so a link to `.git` is caught too."""
    path = _expand(candidate, home)
    target = Path(os.path.realpath(path if path.is_absolute() else root / path))
    real_root = Path(os.path.realpath(root))
    return target.relative_to(real_root).parts if target.is_relative_to(real_root) else ()


def inside(candidate: str, root: Path, home: Path) -> bool:
    """Whether `candidate` (relative paths against `root`) resolves inside `root`, symlinks followed."""
    path = _expand(candidate, home)
    target = Path(os.path.realpath(path if path.is_absolute() else root / path))
    return target.is_relative_to(Path(os.path.realpath(root)))


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
