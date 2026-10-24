"""`.worktreeinclude`: gitignored files to copy into each new worktree.

Same convention as Claude Code: the file uses gitignore syntax, and a path is
copied only when it matches a pattern AND git ignores it, so tracked files and
anything under `.agent-team/` never move. Symlinks are skipped, never followed.
Only paths are returned for logging, never contents.
"""

import shutil
import subprocess
from pathlib import Path

INCLUDE_FILE = ".worktreeinclude"


def _ls(root: Path, *args: str) -> set[str]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--others", "--ignored", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return {p for p in result.stdout.split("\0") if p}


def matching_paths(root: Path, state_dir: str) -> list[str]:
    """Repo-relative paths of ignored files matched by `.worktreeinclude`, sorted."""
    include = root / INCLUDE_FILE
    if not include.is_file():
        return []
    wanted = _ls(root, f"--exclude-from={include}")
    ignored = _ls(root, "--exclude-standard")
    return sorted(p for p in wanted & ignored if not p.startswith(f"{state_dir}/"))


def copy_includes(root: Path, worktree: Path, state_dir: str) -> tuple[list[str], list[str]]:
    """Copy matching files into `worktree`. Returns (copied, skipped) repo-relative paths."""
    copied: list[str] = []
    skipped: list[str] = []
    resolved_root = root.resolve()
    for rel in matching_paths(root, state_dir):
        source = root / rel
        if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(resolved_root):
            skipped.append(rel)
            continue
        target = worktree / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(rel)
    return copied, skipped
