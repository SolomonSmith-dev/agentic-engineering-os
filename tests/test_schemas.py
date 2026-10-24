"""Build-order step 2: canonical schemas, JSON Schema export, agent-team validate."""

import json
import shutil
from pathlib import Path

import jsonschema
import pytest
from typer.testing import CliRunner

from agent_team.canonical import parse_file, validate_tree
from agent_team.cli import app
from agent_team.schemas import SCHEMAS, AgentSpec, json_schema

REPO = Path(__file__).resolve().parents[1]
CANONICAL = REPO / "canonical"
REVIEWER = CANONICAL / "agents" / "independent-reviewer.md"

runner = CliRunner()


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A writable copy of the repo's canonical/ directory."""
    target = tmp_path / "canonical"
    shutil.copytree(CANONICAL, target)
    return target


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, f"{old!r} not in {path}"
    path.write_text(text.replace(old, new, 1))


# --- valid fixtures -----------------------------------------------------------


def test_repo_canonical_tree_validates() -> None:
    report = validate_tree(CANONICAL)
    assert report.ok, [p.render(CANONICAL) for p in report.problems]
    assert {d.kind for d in report.documents} == set(SCHEMAS)


def test_validate_command_passes_on_repo_tree() -> None:
    result = runner.invoke(app, ["validate", str(CANONICAL)])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("ok:")


def test_reviewer_frontmatter_matches_architecture_doc() -> None:
    data, body = parse_file(REVIEWER, "agent")
    spec = AgentSpec.model_validate(data)
    assert spec.model_class == "independent"
    assert spec.max_autonomy == "A2"
    assert "review-checklist" in spec.skills
    assert body.strip(), "the body is the prompt"


# --- bad fields fail with file and field named --------------------------------


@pytest.mark.parametrize(
    ("old", "new", "field"),
    [
        ("max_autonomy: A2", "max_autonomy: A9", "max_autonomy"),
        ("model_class: independent", "model_class: claude-opus-5-5", "model_class"),
        ("workspace: read_only_mirror", "workspace: anywhere", "workspace"),
        ("skills: [review-checklist, requirement-tracing]", "skills: [review-checklist, nope]", "skills"),
        ("max_autonomy: A2", "max_autonomy: A2\nmodel: gpt-5", "model"),
    ],
)
def test_bad_agent_field_names_file_and_field(tree: Path, old: str, new: str, field: str) -> None:
    agent = tree / "agents" / "independent-reviewer.md"
    _edit(agent, old, new)
    result = runner.invoke(app, ["validate", str(tree)])
    assert result.exit_code == 1
    assert "independent-reviewer.md" in result.output
    assert f": {field}: " in result.output


def test_empty_agent_body_fails(tree: Path) -> None:
    agent = tree / "agents" / "independent-reviewer.md"
    text = agent.read_text()
    fm_end = text.index("\n---\n", 4)
    agent.write_text(text[: fm_end + 5])
    report = validate_tree(tree)
    assert [(p.file.name, p.field) for p in report.problems] == [("independent-reviewer.md", "body")]


def test_bad_workflow_transition_fails(tree: Path) -> None:
    _edit(tree / "workflows" / "bug-fix.yaml", "to: HUMAN_DECISION }", "to: SHIPPED }")
    report = validate_tree(tree)
    assert any(p.file.name == "bug-fix.yaml" and "SHIPPED" in p.message for p in report.problems)


def test_tool_credential_must_be_a_handle(tree: Path) -> None:
    _edit(tree / "tools" / "gh-pr-create.yaml", "credential: GH_TOKEN", "credential: ghp_abc123")
    report = validate_tree(tree)
    assert [(p.file.name, p.field) for p in report.problems] == [("gh-pr-create.yaml", "credential")]


def test_missing_frontmatter_is_reported(tree: Path) -> None:
    skill = tree / "skills" / "review-checklist" / "SKILL.md"
    skill.write_text("# no frontmatter\n")
    report = validate_tree(tree)
    assert any(p.file == skill and p.field == "<file>" for p in report.problems)


def test_missing_directory_fails() -> None:
    result = runner.invoke(app, ["validate", "/nonexistent/canonical"])
    assert result.exit_code == 1
    assert "not found" in result.output


# --- JSON Schema export round-trips --------------------------------------------


def test_schema_export_writes_every_kind(tmp_path: Path) -> None:
    result = runner.invoke(app, ["schema", "export", "--out", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(f"{k}.schema.json" for k in SCHEMAS)


def test_exported_schema_round_trips_and_validates_fixtures(tmp_path: Path) -> None:
    """Export -> file -> load -> a standalone validator accepts the fixtures and rejects a bad one."""
    runner.invoke(app, ["schema", "export", "--out", str(tmp_path)])
    report = validate_tree(CANONICAL)
    assert report.ok

    for doc in report.documents:
        loaded = json.loads((tmp_path / f"{doc.kind}.schema.json").read_text())
        assert loaded == json_schema(doc.kind)
        assert loaded["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        jsonschema.Draft202012Validator.check_schema(loaded)
        jsonschema.validate(doc.data, loaded)  # the exported schema is usable without pydantic

    agent_schema = json.loads((tmp_path / "agent.schema.json").read_text())
    data, _ = parse_file(REVIEWER, "agent")
    data["max_autonomy"] = "A9"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(data, agent_schema)


def test_schema_export_single_kind_to_stdout() -> None:
    result = runner.invoke(app, ["schema", "export", "skill"])
    assert result.exit_code == 0
    assert json.loads(result.output)["title"] == "SkillSpec"


def test_schema_export_unknown_kind() -> None:
    result = runner.invoke(app, ["schema", "export", "nope"])
    assert result.exit_code == 2
    assert "unknown kind" in result.output
