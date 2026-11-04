"""Scope check (engine plan M2 Task 8): writes outside the task's paths, tooling state, dependency sources."""

import os
import subprocess
from pathlib import Path

import pytest

from agent_team.orchestrator.scope import (
    DEFAULT_SCOPE,
    PROTECTED_PREFIXES,
    ScopeError,
    ScopeReport,
    changed_paths,
    check_scope,
)
from agent_team.schemas.adapter import ReviewFinding

SCOPE = {"paths": ["src/ui"], "allow_dependency_changes": False}
ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, check=True, env={**os.environ, **ENV},
    )  # fmt: skip
    return out.stdout.strip()


def commit(
    repo: Path, files: dict[str, str] | None = None, *, mv: dict[str, str] | None = None, rm: list[str] | None = None
) -> str:
    for rel, text in (files or {}).items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    for src, dst in (mv or {}).items():
        (repo / dst).parent.mkdir(parents=True, exist_ok=True)
        git(repo, "mv", src, dst)
    for rel in rm or []:
        git(repo, "rm", "-q", rel)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "change")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    for rel in ("src/ui/x.py", "src/core/c.py", "docs/old.md", "uv.lock", "README.md"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("base\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    return repo


@pytest.fixture
def base(git_repo: Path) -> str:
    return git(git_repo, "rev-parse", "HEAD")


def test_constants() -> None:
    assert PROTECTED_PREFIXES == (".claude", ".agent-team")
    assert DEFAULT_SCOPE == {"paths": None, "allow_dependency_changes": True}


def test_in_scope_passes_and_out_of_scope_fails(git_repo: Path, base: str) -> None:
    head = commit(git_repo, {"src/ui/a.py": "x", "src/core/b.py": "y"})
    report = check_scope(git_repo, base, head, scope=SCOPE)
    assert report.outside == ["src/core/b.py"] and not report.ok
    ok_head = commit(git_repo, {"src/ui/more.py": "z"})
    assert check_scope(git_repo, head, ok_head, scope={**SCOPE, "allow_dependency_changes": True}).ok


def test_a_sibling_prefix_is_outside(git_repo: Path, base: str) -> None:
    head = commit(git_repo, {"src/uix/a.py": "x"})
    assert check_scope(git_repo, base, head, scope=SCOPE).outside == ["src/uix/a.py"]


def test_local_tooling_state_always_fails(git_repo: Path, base: str) -> None:
    head = commit(git_repo, {".claude/settings.json": "{}", ".agent-team/x": "y", ".claudex/ok.txt": "fine"})
    report = check_scope(git_repo, base, head, scope=DEFAULT_SCOPE)
    assert report.protected == [".agent-team/x", ".claude/settings.json"]
    assert report.outside == [] and not report.ok


def test_prompt_runs_have_no_path_rule(git_repo: Path, base: str) -> None:
    head = commit(git_repo, {"anywhere/at/all.py": "x", "uv.lock": "changed"})
    assert check_scope(git_repo, base, head, scope=DEFAULT_SCOPE).ok


def test_whole_repo_scope_allows_everything_but_tooling(git_repo: Path, base: str) -> None:
    head = commit(git_repo, {"anywhere/x.py": "x", ".claude/s.json": "{}"})
    report = check_scope(git_repo, base, head, scope={"paths": ["."], "allow_dependency_changes": True})
    assert report.outside == [] and report.protected == [".claude/s.json"]


def test_rename_and_delete_across_scope(git_repo: Path, base: str) -> None:  # Review Focus 5
    head = commit(git_repo, mv={"src/ui/x.py": "src/core/x.py"}, rm=["docs/old.md"])
    assert sorted(check_scope(git_repo, base, head, scope=SCOPE).outside) == ["docs/old.md", "src/core/x.py"]
    assert changed_paths(git_repo, base, head) == ["docs/old.md", "src/core/x.py", "src/ui/x.py"]


def test_rename_into_scope_from_outside_is_still_a_write_outside(git_repo: Path, base: str) -> None:
    head = commit(git_repo, mv={"src/core/c.py": "src/ui/c.py"})
    assert check_scope(git_repo, base, head, scope=SCOPE).outside == ["src/core/c.py"]


def test_lockfile_change_needs_the_flag(git_repo: Path, base: str) -> None:
    head = commit(git_repo, {"src/ui/a.py": "x", "uv.lock": "changed"})
    report = check_scope(git_repo, base, head, scope=SCOPE)
    assert report.dependency[0].rule == "dep-lockfile"
    allowed = {"paths": ["src/ui", "uv.lock"], "allow_dependency_changes": True}
    assert check_scope(git_repo, base, head, scope=allowed).ok


def test_lockfile_inside_paths_still_fails_without_the_flag(git_repo: Path, base: str) -> None:
    head = commit(git_repo, {"uv.lock": "changed"})
    report = check_scope(git_repo, base, head, scope={"paths": ["uv.lock"], "allow_dependency_changes": False})
    assert report.outside == [] and [f.rule for f in report.dependency] == ["dep-lockfile"] and not report.ok


def test_findings_match_the_review_finding_schema(git_repo: Path, base: str) -> None:
    head = commit(git_repo, {"src/core/b.py": "y", ".claude/s.json": "{}", "uv.lock": "changed"})
    findings = check_scope(git_repo, base, head, scope=SCOPE).findings()
    assert findings
    for f in findings:
        ReviewFinding.model_validate(f)
        assert f["requirement_id"] == "SCOPE" and f["severity"] == "blocker" and f["line"] is None
    by_file = {f["file"]: f["fix"] for f in findings}
    assert by_file["src/core/b.py"] == "revert the change to src/core/b.py: outside the task's paths src/ui"
    assert by_file[".claude/s.json"] == "revert the change to .claude/s.json: local tooling state is never committed"
    assert by_file["uv.lock"].startswith("revert the change to uv.lock: dep-lockfile")
    assert by_file["uv.lock"].endswith("dependency changes need allow_dependency_changes: true")
    assert len([f for f in findings if f["file"] == ".claude/s.json"]) == 1, "a protected file is reported once"


def test_no_changes_is_ok(git_repo: Path, base: str) -> None:
    report = check_scope(git_repo, base, base, scope=SCOPE)
    assert report == ScopeReport([], [], [], ("src/ui",)) and report.ok and report.findings() == []


def test_unusable_revisions_raise(git_repo: Path, base: str) -> None:
    with pytest.raises(ScopeError):
        changed_paths(git_repo, "--output=x", base)
    with pytest.raises(ScopeError):
        changed_paths(git_repo, base, "0" * 40)
