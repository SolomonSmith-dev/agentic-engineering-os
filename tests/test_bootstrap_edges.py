"""Edges of `init` and `configure` (build-order step 8): refusals, symlinks, diff-before-write, odd inputs.

Reuses the throwaway repositories and the faked PATH lookup from
test_init_configure.py. Nothing here runs a repository's tools.
"""

import shutil
from pathlib import Path

import pytest
import yaml
from test_init_configure import (  # noqa: F401  (fixtures are used by name)
    CANONICAL,
    approve,
    commit,
    configure,
    fake_path,
    git,
    init,
    new_repo,
    node_repo,
    python_repo,
    tree,
    write,
)

from agent_team.bootstrap import (
    BEGIN_MARKER,
    END_MARKER,
    BootstrapError,
    apply_plan,
    discover,
    plan_configure,
)
from agent_team.bootstrap import configure as configure_mod
from agent_team.bootstrap.configure import AGENT_MARKER, PlannedWrite, render_block
from agent_team.bootstrap.discovery import (
    SCAN_MAX_BYTES,
    discover_commands,
    discover_secrets,
)
from agent_team.schemas import RepoConfig

# --- configure: the proposal it reads -------------------------------------------------


def test_configure_names_an_unreadable_proposal(python_repo: Path) -> None:
    assert init(python_repo).exit_code == 0
    (python_repo / ".agent-team" / "proposal.yaml").write_text("ask: [unclosed\n", encoding="utf-8")
    before = tree(python_repo)
    result = configure(python_repo, "--yes")
    assert result.exit_code == 2 and "cannot read" in result.output and "proposal.yaml" in result.output
    assert tree(python_repo) == before


def test_configure_names_the_first_invalid_field(python_repo: Path) -> None:
    assert init(python_repo).exit_code == 0
    approve(python_repo, autonomy_ceiling="A5")
    result = configure(python_repo, "--yes")
    assert result.exit_code == 2
    assert ".agent-team/proposal.yaml is invalid: ask.autonomy_ceiling:" in result.output
    assert not (python_repo / ".agent-team" / "config.yaml").exists()


def test_configure_without_a_proposal_says_run_init(python_repo: Path) -> None:
    result = configure(python_repo, "--yes")
    assert result.exit_code == 2 and "run 'agent-team init' first" in result.output


def test_configure_with_a_bad_canonical_dir_writes_nothing(python_repo: Path, tmp_path: Path) -> None:
    assert init(python_repo).exit_code == 0
    approve(python_repo)
    before = tree(python_repo)
    empty = tmp_path / "not-canonical"
    empty.mkdir()
    result = configure(python_repo, "--yes", "--canonical", str(empty))
    assert result.exit_code == 2 and "pass --canonical" in result.output
    assert tree(python_repo) == before


# --- configure: what it compiles ----------------------------------------------------------


def test_a_privacy_class_no_model_has_compiles_no_agent(python_repo: Path) -> None:
    assert init(python_repo).exit_code == 0
    approve(python_repo, privacy_classes=["local-only"])
    result = configure(python_repo, "--yes")
    assert result.exit_code == 0, result.output
    assert "agent implementer not compiled: model 'claude-sonnet-5-5' is vendor-cloud; policy allows only local-only" in result.output
    assert not (python_repo / ".claude" / "agents").exists()
    config = RepoConfig.model_validate(yaml.safe_load((python_repo / ".agent-team" / "config.yaml").read_text(encoding="utf-8")))
    assert config.agents == [] and config.instruction_files == ["CLAUDE.md"]


def test_a_runtime_without_an_adapter_is_noted_and_compiles_nothing(python_repo: Path) -> None:
    (python_repo / "CLAUDE.md").unlink()
    write(python_repo, "AGENTS.md", "# Widgets\n")
    commit(python_repo)
    assert init(python_repo).exit_code == 0
    approve(python_repo, runtimes=["codex"])
    result = configure(python_repo, "--yes")
    assert result.exit_code == 0, result.output
    assert "note: runtime codex: no adapter yet; nothing compiled for it" in result.output
    assert not (python_repo / "CLAUDE.md").exists(), "CLAUDE.md is only for the claude runtime"
    assert BEGIN_MARKER in (python_repo / "AGENTS.md").read_text(encoding="utf-8")
    assert not (python_repo / ".claude").exists()


def test_copilot_agents_of_the_same_name_are_noted_and_left_alone(python_repo: Path) -> None:
    copilot = write(python_repo, ".github/agents/implementer.agent.md", "copilot's own\n")
    write(python_repo, ".github/agents/unrelated.md", "other\n")
    assert init(python_repo).exit_code == 0
    approve(python_repo)
    result = configure(python_repo, "--yes")
    assert result.exit_code == 0, result.output
    assert ".github/agents/implementer.agent.md defines the same agent for Copilot; left untouched" in result.output
    assert "unrelated.md" not in result.output
    assert copilot.read_text(encoding="utf-8") == "copilot's own\n"


def test_no_verified_command_says_so_in_the_block() -> None:
    config = RepoConfig(default_branch="main", protected_branches=["main", "release"], runtimes=["claude"],
                        privacy_classes=["vendor-cloud"], autonomy_ceiling="A2", draft_prs=True,
                        commands={}, unverified_commands={"test": ["make test"]})  # fmt: skip
    block = render_block(config)
    assert block.startswith(BEGIN_MARKER + "\n") and block.endswith(END_MARKER + "\n")
    assert "- No verified build, test, lint or type-check commands." in block
    assert "make test" not in block, "an unverified command never reaches an instruction file"
    assert "protected branches: `main`, `release`." in block and "Autonomy ceiling A2." in block


# --- configure: instruction files it must not touch -----------------------------------------


def test_a_malformed_block_refuses_and_writes_nothing(python_repo: Path) -> None:
    write(python_repo, "CLAUDE.md", f"# Widgets\n{BEGIN_MARKER}\nhalf a block\n")
    assert init(python_repo).exit_code == 0
    approve(python_repo)
    before = tree(python_repo)
    result = configure(python_repo, "--yes")
    assert result.exit_code == 2 and "leave exactly one pair, in order, or none" in result.output
    assert tree(python_repo) == before


def test_claude_md_linked_to_agents_md_gets_the_block_once(node_repo: Path) -> None:
    (node_repo / "CLAUDE.md").symlink_to("AGENTS.md")
    assert init(node_repo).exit_code == 0
    approve(node_repo)
    result = configure(node_repo, "--yes")
    assert result.exit_code == 0, result.output
    assert "CLAUDE.md reaches AGENTS.md; the block goes in AGENTS.md only" in result.output
    assert (node_repo / "CLAUDE.md").is_symlink()
    assert (node_repo / "AGENTS.md").read_text(encoding="utf-8").count(BEGIN_MARKER) == 1


def test_agents_md_linked_outside_the_repo_is_left_untouched(python_repo: Path, tmp_path: Path) -> None:
    outside = write(tmp_path, "elsewhere/AGENTS.md", "someone else's\n")
    (python_repo / "AGENTS.md").symlink_to(outside)
    assert init(python_repo).exit_code == 0
    approve(python_repo)
    result = configure(python_repo, "--yes")
    assert result.exit_code == 0, result.output
    assert "AGENTS.md is a symlink or sits under one; left untouched" in result.output
    assert outside.read_text(encoding="utf-8") == "someone else's\n"
    config = yaml.safe_load((python_repo / ".agent-team" / "config.yaml").read_text(encoding="utf-8"))
    assert config["instruction_files"] == ["CLAUDE.md"]


def test_a_symlinked_state_dir_refuses_before_writing(python_repo: Path, tmp_path: Path) -> None:
    assert init(python_repo).exit_code == 0
    approve(python_repo)
    elsewhere = tmp_path / "elsewhere-state"
    shutil.move(python_repo / ".agent-team", elsewhere)
    (python_repo / ".agent-team").symlink_to(elsewhere, target_is_directory=True)
    before_repo, before_outside = tree(python_repo), tree(elsewhere)
    result = configure(python_repo, "--yes")
    assert result.exit_code == 2 and ("symlink" in result.output or "symbolic link" in result.output), result.output
    assert tree(python_repo) == before_repo and tree(elsewhere) == before_outside


def test_init_refuses_a_symlinked_state_dir(python_repo: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere-state"
    elsewhere.mkdir()
    (python_repo / ".agent-team").symlink_to(elsewhere, target_is_directory=True)
    result = init(python_repo)
    assert result.exit_code == 2 and ("symlink" in result.output or "symbolic link" in result.output), result.output
    assert list(elsewhere.iterdir()) == []


# --- configure: diff before write ---------------------------------------------------------


def test_apply_refuses_when_a_file_changed_after_the_diff(python_repo: Path) -> None:
    assert init(python_repo).exit_code == 0
    approve(python_repo)
    plan = plan_configure(python_repo, CANONICAL)
    assert plan.writes[0].path == "CLAUDE.md"
    (python_repo / "CLAUDE.md").write_text("# Widgets\n\nedited after the diff\n", encoding="utf-8")
    before = tree(python_repo)
    with pytest.raises(BootstrapError, match="CLAUDE.md changed since the diff was shown"):
        apply_plan(plan)
    assert tree(python_repo) == before, "the check runs for every file before the first write"


def test_apply_refuses_a_path_that_became_a_symlink_after_the_diff(python_repo: Path, tmp_path: Path) -> None:
    assert init(python_repo).exit_code == 0
    approve(python_repo)
    plan = plan_configure(python_repo, CANONICAL)
    outside = tmp_path / "home" / ".claude"
    outside.mkdir(parents=True)
    (python_repo / ".claude").symlink_to(outside, target_is_directory=True)
    with pytest.raises(BootstrapError, match=r"\.claude/agents/.* changed since the diff was shown"):
        apply_plan(plan)
    assert list(outside.iterdir()) == []


def test_a_failed_write_leaves_no_temp_file(python_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert init(python_repo).exit_code == 0
    approve(python_repo)
    plan = plan_configure(python_repo, CANONICAL)
    original = (python_repo / "CLAUDE.md").read_text(encoding="utf-8")

    def fail(src: str, dst: str) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(configure_mod.os, "replace", fail)
    with pytest.raises(OSError, match="No space left"):
        apply_plan(plan)
    assert (python_repo / "CLAUDE.md").read_text(encoding="utf-8") == original
    assert not [p for p in python_repo.rglob("*.tmp")], "the temp file is removed"


def test_apply_keeps_an_existing_files_mode(python_repo: Path) -> None:
    (python_repo / "CLAUDE.md").chmod(0o600)
    assert init(python_repo).exit_code == 0
    approve(python_repo)
    assert configure(python_repo, "--yes").exit_code == 0
    assert (python_repo / "CLAUDE.md").stat().st_mode & 0o777 == 0o600
    assert (python_repo / ".claude" / "agents" / "implementer.md").stat().st_mode & 0o777 == 0o644


def test_planned_write_diff_marks_new_files_and_missing_newlines() -> None:
    new = PlannedWrite(path="AGENTS.md", before=None, after="one\n").diff()
    assert new.startswith("--- /dev/null\n+++ b/AGENTS.md\n")
    edited = PlannedWrite(path="x.md", before="a", after="a\nb").diff()
    assert "--- a/x.md\n+++ b/x.md\n" in edited and edited.count("\\ No newline at end of file") == 2


def test_an_agent_file_carries_the_managed_marker_after_its_frontmatter(python_repo: Path) -> None:
    assert init(python_repo).exit_code == 0
    approve(python_repo)
    assert configure(python_repo, "--yes").exit_code == 0
    text = (python_repo / ".claude" / "agents" / "independent-reviewer.md").read_text(encoding="utf-8")
    frontmatter, _, rest = text.partition("\n---\n")
    assert AGENT_MARKER not in frontmatter and rest.startswith("\n" + AGENT_MARKER + "\n")
    assert not (python_repo / ".claude" / "settings.json").exists() and not (python_repo / ".claude" / "policy.json").exists()


# --- discovery: never follow a link out, never read a value -------------------------------


def test_discovery_does_not_follow_symlinks_out_of_the_repo(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    write(outside, "package.json", '{"scripts": {"test": "vitest"}}\n')
    write(outside, "rules/secret-rule.md", "rule\n")
    write(outside, "settings.yml", "branches:\n  - name: main\n    protection: {}\n")
    root = new_repo(tmp_path / "linked")
    (root / "package.json").symlink_to(outside / "package.json")
    (root / ".cursor").mkdir()
    (root / ".cursor" / "rules").symlink_to(outside / "rules", target_is_directory=True)
    (root / ".github").mkdir()
    (root / ".github" / "settings.yml").symlink_to(outside / "settings.yml")
    (root / "id_rsa.pem").symlink_to(outside / "package.json")
    proposal = discover(root)
    assert proposal.commands == []
    assert proposal.instruction_files == []
    assert proposal.vcs.protected_branches == "unknown"
    assert [s.path for s in proposal.secrets] == [], "a symlinked key file is not reported or read"


def test_secret_files_are_reported_by_kind(tmp_path: Path) -> None:
    root = new_repo(tmp_path / "secrets", ".agent-team/\n")
    for rel in (".envrc", ".sops.yaml", "certs/server.pem", "deploy/prod.env", ".env.sample",
                "node_modules/pkg/.env", "a/b/c/d/e/deep.key"):  # fmt: skip
        write(root, rel, "VALUE-CANARY\n")
    commit(root)
    hints = {(h.path, h.kind) for h in discover_secrets(root, [])}
    assert hints == {(".envrc", "direnv"), (".sops.yaml", "sops"), ("certs/server.pem", "key-file"),
                     ("deploy/prod.env", "env-file"), (".env.sample", "env-template")}  # fmt: skip
    assert "VALUE-CANARY" not in repr(discover(root))


def test_an_oversized_config_file_is_not_read(tmp_path: Path) -> None:
    root = new_repo(tmp_path / "big")
    write(root, "Makefile", "test:\n\ttrue\n" + "#" * (SCAN_MAX_BYTES + 1))
    assert discover_commands(root, {"make": "/usr/bin/make"}.get) == []


def test_malformed_manifests_yield_no_commands(tmp_path: Path) -> None:
    root = new_repo(tmp_path / "broken")
    write(root, "pyproject.toml", "[project\nname = 'x'\n")
    write(root, "package.json", "{not json")
    write(root, ".mcp.json", "{not json")
    write(root, ".github/settings.yml", "branches: [unclosed\n")
    proposal = discover(root)
    assert proposal.commands == []
    assert [(m.path, m.servers) for m in proposal.mcp_configs] == [(".mcp.json", [])]
    assert proposal.vcs.protected_branches == "unknown"


def test_toolchains_and_less_common_python_tools(tmp_path: Path) -> None:
    root = new_repo(tmp_path / "poly")
    write(root, "Cargo.toml", "[package]\nname = 'x'\n")
    write(root, "go.mod", "module x\n")
    write(root, "pyproject.toml", "[tool.poetry.group.dev.dependencies]\npytest = '*'\nflake8 = '*'\npyright = '*'\n")
    write(root, "poetry.lock", "")
    write(root, "package.json", '{"scripts": {"test": "echo \\"Error: no test specified\\" && exit 1"}}\n')
    found = {(c.kind, c.command): (c.verified, c.check) for c in discover_commands(root, {"cargo": "/usr/bin/cargo"}.get)}
    assert found[("test", "cargo test")] == (True, "cargo on PATH")
    assert found[("lint", "go vet ./...")] == (False, "go not on PATH")
    assert found[("test", "poetry run pytest")] == (False, "poetry not on PATH")
    assert ("lint", "poetry run flake8") in found and ("typecheck", "poetry run pyright") in found
    assert not [c for c in found if c[1].startswith("npm")], "npm's placeholder test script is not a test command"


def test_default_branch_from_the_remote_head_then_the_current_branch(tmp_path: Path) -> None:
    root = new_repo(tmp_path / "branches")
    git(root, "checkout", "-q", "-b", "trunk")
    write(root, "README.md", "x\n")
    commit(root)
    git(root, "branch", "-q", "-m", "trunk", "develop")
    vcs = discover(root).vcs
    assert (vcs.default_branch, vcs.default_branch_source) == ("develop", "current branch")
    git(root, "remote", "add", "upstream", "https://example.invalid/acme/x.git")
    git(root, "update-ref", "refs/remotes/upstream/stable", "HEAD")
    git(root, "symbolic-ref", "refs/remotes/upstream/HEAD", "refs/remotes/upstream/stable")
    vcs = discover(root).vcs
    assert (vcs.default_branch, vcs.default_branch_source) == ("stable", "upstream/HEAD")
    assert (vcs.host, vcs.remote) == ("other", "example.invalid/acme/x")


def test_a_non_utf8_instruction_file_is_refused_cleanly(python_repo: Path) -> None:
    (python_repo / "CLAUDE.md").write_bytes("# Caf\xe9\n".encode("latin-1"))
    assert init(python_repo).exit_code == 0
    approve(python_repo)
    before = tree(python_repo)
    result = configure(python_repo, "--yes")
    assert tree(python_repo) == before
    assert result.exit_code == 2 and "CLAUDE.md is not UTF-8 text" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit), "a refusal, not a traceback"
