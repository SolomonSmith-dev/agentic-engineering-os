import shutil
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from agent_team.canonical import split_frontmatter, validate_tree
from agent_team.cli import app
from agent_team.lint import cap_matches, lint_tree

runner = CliRunner()
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def tmp_canonical(tmp_path):
    shutil.copytree(REPO / "canonical", tmp_path / "canonical")
    shutil.copytree(REPO / "routing", tmp_path / "routing")
    return tmp_path / "canonical"


def _edit_frontmatter(path: Path, edit) -> None:
    raw, body = split_frontmatter(path.read_text(encoding="utf-8"))
    data = yaml.safe_load(raw)
    edit(data)
    path.write_text(f"---\n{yaml.safe_dump(data, sort_keys=False)}---\n{body}", encoding="utf-8")


def dup_key(key):
    def mutate(path: Path) -> None:
        raw, body = split_frontmatter(path.read_text(encoding="utf-8"))
        value = yaml.safe_load(raw)[key]
        path.write_text(f"---\n{raw}{key}: {value}\n---\n{body}", encoding="utf-8")

    return mutate


def set_caps(allow, deny):
    def mutate(path: Path) -> None:
        _edit_frontmatter(path, lambda d: d.__setitem__("capabilities", {"allow": allow, "deny": deny}))

    return mutate


def set_body(body):
    def mutate(path: Path) -> None:
        raw, _ = split_frontmatter(path.read_text(encoding="utf-8"))
        path.write_text(f"---\n{raw}---\n\n{body}\n", encoding="utf-8")

    return mutate


def test_shipped_canonical_is_lint_clean():
    result = runner.invoke(app, ["validate", "--lint"])
    assert result.exit_code == 0, result.output
    assert "lint clean" in result.output


def test_validate_without_lint_has_no_lint_suffix():
    result = runner.invoke(app, ["validate"])
    assert result.exit_code == 0 and "lint clean" not in result.output


@pytest.mark.parametrize(
    "mutation,field,needle",
    [
        (dup_key("name"), "<file>", "duplicate key 'name'"),
        (set_caps(allow=["fs.read", "git.commit"], deny=["git.commit"]), "capabilities", "git.commit"),
        (
            set_caps(allow=["fs.read", "fs.write.workspace"], deny=["fs.write.*", "git.commit"]),
            "capabilities",
            "fs.write.workspace",
        ),
        (set_body("Review the diff."), "body", "You are"),
    ],
)
def test_reviewer_mutations_are_caught(tmp_canonical, mutation, field, needle):
    mutation(tmp_canonical / "agents" / "independent-reviewer.md")
    problems = lint_tree(validate_tree(tmp_canonical))
    assert any(p.field == field and needle in p.message for p in problems), problems


def test_duplicate_key_reports_line(tmp_canonical):
    dup_key("name")(tmp_canonical / "agents" / "independent-reviewer.md")
    [p] = [p for p in lint_tree(validate_tree(tmp_canonical)) if "duplicate" in p.message]
    assert "(line " in p.message


def test_reviewer_must_deny_writes(tmp_canonical):
    set_caps(allow=["fs.read"], deny=["github.pr.*"])(tmp_canonical / "agents" / "independent-reviewer.md")
    problems = [p for p in lint_tree(validate_tree(tmp_canonical)) if p.field == "capabilities.allow"]
    assert {"fs.write.*", "git.commit"} <= {m for p in problems for m in ("fs.write.*", "git.commit") if m in p.message}


def test_read_only_mirror_allows_no_writes(tmp_canonical):
    set_caps(allow=["fs.read", "deploy.prod"], deny=["fs.write.*", "git.commit"])(
        tmp_canonical / "agents" / "independent-reviewer.md"
    )
    assert [p for p in lint_tree(validate_tree(tmp_canonical)) if "read_only_mirror" in p.message]


def test_a0_agent_may_only_read(tmp_canonical):
    set_caps(allow=["fs.read", "test.run"], deny=[])(tmp_canonical / "agents" / "repository-archaeologist.md")
    assert [p for p in lint_tree(validate_tree(tmp_canonical)) if "test.run" in p.message]


def test_cli_lint_failure_exits_1(tmp_canonical):
    set_body("Review the diff.")(tmp_canonical / "agents" / "independent-reviewer.md")
    result = runner.invoke(app, ["validate", str(tmp_canonical), "--lint"])
    assert result.exit_code == 1 and "body" in result.output


@pytest.mark.parametrize(
    "cap,pattern,hit",
    [("fs.write.x", "fs.write.*", True), ("fs.write.*", "fs.write.x", True), ("fs.read", "fs.write.*", False)],
)
def test_cap_matches(cap, pattern, hit):
    assert cap_matches(cap, pattern) is hit
