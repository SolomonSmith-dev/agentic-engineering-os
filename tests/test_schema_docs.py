"""Step 14: docs/spec/ pages are generated from the JSON Schemas and must not drift from them."""

from pathlib import Path

from typer.testing import CliRunner

from agent_team.cli import app
from agent_team.schemas import SCHEMAS, json_schema
from agent_team.schemas.docs import INDEX, drift, render_all, render_kind

runner = CliRunner()

SPEC = Path(__file__).resolve().parent.parent / "docs" / "spec"


def test_committed_spec_pages_match_the_schemas() -> None:
    """Fails when a model changed without `agent-team schema docs --out docs/spec` being re-run."""
    assert drift(SPEC) == []


def test_one_page_per_kind_plus_an_index() -> None:
    assert sorted(render_all()) == sorted([INDEX, *(f"{kind}.md" for kind in SCHEMAS)])


def test_every_field_and_definition_appears_on_its_page() -> None:
    for kind in SCHEMAS:
        schema = json_schema(kind)
        page = render_kind(kind)
        for name in schema["properties"]:
            assert f"| `{name}` |" in page, (kind, name)
        for definition_name, definition in schema.get("$defs", {}).items():
            assert f"## {definition_name}\n" in page, (kind, definition_name)
            for name in definition.get("properties", {}):
                assert f"| `{name}` |" in page, (kind, definition_name, name)


def test_table_cells_escape_pipes() -> None:
    # The trigger pattern holds `|`; unescaped it would split the Markdown table row.
    row = next(line for line in render_kind("agent").splitlines() if line.startswith("| `triggers` |"))
    assert "state\\|policy\\|event\\|manual" in row
    assert row.replace("\\|", "").count("|") == 7


def test_schema_docs_writes_then_check_passes(tmp_path: Path) -> None:
    result = runner.invoke(app, ["schema", "docs", "--out", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(render_all())
    result = runner.invoke(app, ["schema", "docs", "--out", str(tmp_path), "--check"])
    assert result.exit_code == 0, result.output
    assert "matches the schemas" in result.output


def test_check_reports_stale_missing_and_stray_pages(tmp_path: Path) -> None:
    runner.invoke(app, ["schema", "docs", "--out", str(tmp_path)])
    (tmp_path / "agent.md").write_text("hand edited\n", encoding="utf-8")
    (tmp_path / "tool.md").unlink()
    (tmp_path / "extra.md").write_text("# not generated\n", encoding="utf-8")
    result = runner.invoke(app, ["schema", "docs", "--out", str(tmp_path), "--check"])
    assert result.exit_code == 1
    assert "agent.md: out of date" in result.output
    assert "tool.md: missing" in result.output
    assert "extra.md: not generated from a schema" in result.output
    assert (tmp_path / "agent.md").read_text(encoding="utf-8") == "hand edited\n"  # --check writes nothing
