"""Scope check: did the run write outside the task's declared paths?

Run on the diff between the run's base commit and the head the TEST step is about to test.
Three rules, each reported separately so the implementer's retry gets the exact file list:

    protected    a changed file under `.claude/` or `.agent-team/` (local tooling state is never committed)
    outside      a changed file within none of the task's paths (only when the task declares paths)
    dependency   a dependency-source change (lockfile, manifest source or index URL, .gitmodules) unless
                 the task sets `allow_dependency_changes: true` (only when the task declares paths)

A rename changes two paths and a delete changes one, so `git diff --no-renames` is used:
moving a file out of scope, or deleting an out-of-scope file, is a write outside scope.
A prompt run has no declared paths, so only the protected rule applies (`DEFAULT_SCOPE`).
"""

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_team.tasks.audit import AuditFinding, audit_dependency_changes
from agent_team.tasks.paths import normalize, within

PROTECTED_PREFIXES: tuple[str, ...] = (".claude", ".agent-team")
DEFAULT_SCOPE: dict[str, Any] = {"paths": None, "allow_dependency_changes": True}

GIT_TIMEOUT_S = 60


class ScopeError(RuntimeError):
    """git could not produce the changed-file list."""


def changed_paths(root: Path, base: str, head: str) -> list[str]:
    """Files changed from `base` to `head`, both sides of a rename, sorted."""
    for rev in (base, head):
        if not rev or rev.startswith("-"):
            raise ScopeError(f"revision {rev!r} is not a usable commit")
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "diff", "--name-only", "--no-renames", "-z", base, head, "--"],
            capture_output=True, check=False, timeout=GIT_TIMEOUT_S,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ScopeError(f"git diff failed: {type(exc).__name__}") from exc
    if done.returncode != 0:
        raise ScopeError(f"git diff {base[:12]}..{head[:12]} failed: {done.stderr.decode('utf-8', 'replace').strip()}")
    names = {n.decode("utf-8", "surrogateescape") for n in done.stdout.split(b"\0") if n}
    return sorted(names)


@dataclass(frozen=True)
class ScopeReport:
    outside: list[str]
    protected: list[str]
    dependency: list[AuditFinding]
    paths: tuple[str, ...] | None = field(default=None)

    @property
    def ok(self) -> bool:
        return not (self.outside or self.protected or self.dependency)

    def findings(self) -> list[dict[str, Any]]:
        """One blocker per violation, shaped like `ReviewFinding`, for the implementer's retry prompt."""
        listed = ", ".join(self.paths or ()) or "(none)"
        out: list[dict[str, Any]] = []

        def add(file: str, fix: str) -> None:
            out.append({"file": file, "line": None, "requirement_id": "SCOPE", "severity": "blocker", "fix": fix})

        for file in self.protected:
            add(file, f"revert the change to {file}: local tooling state is never committed")
        for file in self.outside:
            if file not in self.protected:
                add(file, f"revert the change to {file}: outside the task's paths {listed}")
        for finding in self.dependency:
            add(
                finding.where or "dependency source",
                f"revert the change to {finding.where}: {finding.render()}; dependency changes need allow_dependency_changes: true",
            )
        return out


def check_scope(root: Path, base: str, head: str, *, scope: Mapping[str, Any]) -> ScopeReport:
    """Apply the three scope rules to the diff `base..head`."""
    declared = scope.get("paths")
    paths = tuple(normalize(p) for p in declared) if declared is not None else None
    files = changed_paths(root, base, head)
    protected = [f for f in files if any(within(f, prefix) for prefix in PROTECTED_PREFIXES)]
    outside = [f for f in files if not any(within(f, p) for p in paths)] if paths is not None else []
    dependency: list[AuditFinding] = []
    if paths is not None and not scope.get("allow_dependency_changes", False):
        dependency = audit_dependency_changes(root, base, head)
    return ScopeReport(outside=outside, protected=protected, dependency=dependency, paths=paths)
