"""`agent-team init` and `configure` (build-order step 8) against throwaway git repos.

Discovery never runs a repository's tools, so the only host contact to fake is
the PATH lookup; every test replaces it, and output is deterministic. Regenerate
the proposal fixtures after an intended change with
`UPDATE_SNAPSHOTS=1 uv run pytest tests/test_init_configure.py`.
"""

import hashlib
import os
import subprocess
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from agent_team.bootstrap import (
    AGENT_MARKER,
    BEGIN_MARKER,
    END_MARKER,
    BootstrapError,
    discover,
    dump_proposal,
    extend_with_block,
)
from agent_team.bootstrap import discovery as discovery
from agent_team.bootstrap.discovery import sanitize_remote
from agent_team.cli import app
from agent_team.schemas import Proposal, RepoConfig

FIXTURES = Path(__file__).parent / "fixtures" / "bootstrap"
CANONICAL = Path(__file__).parents[1] / "canonical"
runner = CliRunner()

SECRET_REMOTE = "ghp_REMOTEURLLEAKCANARY0000"
SECRET_ENV = "sk-ant-api03-DOTENV-LEAK-CANARY-1111"
SECRET_MCP = "ghp_MCPCONFIGLEAKCANARY2222"
CANARIES = (SECRET_REMOTE, SECRET_ENV, SECRET_MCP)

TOOLS = {"uv": "/usr/bin/uv", "pnpm": "/usr/bin/pnpm", "make": "/usr/bin/make"}


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def write(root: Path, rel: str, text: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def new_repo(root: Path, gitignore: str = ".agent-team/\n") -> Path:
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    if gitignore:
        write(root, ".gitignore", gitignore)
    return root


def commit(root: Path) -> None:
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "fixture")


@pytest.fixture(autouse=True)
def fake_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discovery, "_which", TOOLS.get)


@pytest.fixture
def python_repo(tmp_path: Path) -> Path:
    root = new_repo(tmp_path / "widgets", ".agent-team/\n.env\n.venv/\n")
    write(
        root,
        "pyproject.toml",
        '[project]\nname = "widgets"\nversion = "0.1.0"\ndependencies = ["requests>=2"]\n\n'
        '[dependency-groups]\ndev = ["pytest>=8", "ruff", "mypy>=1"]\n\n'
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n\n'
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
    )
    write(root, "uv.lock", "version = 1\n")
    write(root, ".venv/bin/pytest")  # installed; ruff and mypy are not, so they fail their check
    write(
        root,
        ".github/workflows/ci.yml",
        "on: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - run: uv run pytest\n        env:\n          PYPI_TOKEN: ${{ secrets.PYPI_TOKEN }}\n",
    )
    write(root, "CLAUDE.md", "# Widgets\n\nUse uv for everything.\n")
    write(root, ".mcp.json", '{"mcpServers": {"github": {"command": "gh-mcp", "env": {"GITHUB_TOKEN": "%s"}}}}\n' % SECRET_MCP)
    write(root, ".env", f"ANTHROPIC_API_KEY={SECRET_ENV}\n")
    write(root, ".env.example", "ANTHROPIC_API_KEY=\n")
    write(root, ".github/CODEOWNERS", "* @acme/widgets\n")
    write(root, ".devcontainer/devcontainer.json", '{"image": "python:3.12"}\n')
    git(root, "remote", "add", "origin", f"https://deploy:{SECRET_REMOTE}@github.com/acme/widgets.git")
    commit(root)
    return root


@pytest.fixture
def node_repo(tmp_path: Path) -> Path:
    root = new_repo(tmp_path / "web", ".agent-team/\nnode_modules/\n")
    write(
        root,
        "package.json",
        '{"name": "web", "scripts": {"build": "tsc -p .", "test": "vitest run", "lint": "eslint .", "typecheck": "tsc --noEmit"}}\n',
    )
    write(root, "pnpm-lock.yaml", "lockfileVersion: '9.0'\n")
    write(root, "tsconfig.json", "{}\n")
    write(root, "node_modules/.bin/tsc")
    write(root, "Makefile", "VAR := $(shell echo never-run)\n\ntest:\n\tpnpm run test\n\ndeploy:\n\t./deploy.sh\n")
    write(root, ".gitlab-ci.yml", "test:\n  script:\n    - export VAULT_ADDR=https://vault.example.invalid\n    - make test\n")
    write(root, "AGENTS.md", "# Web\n\nRun pnpm, never npm.\n")
    write(
        root,
        ".github/settings.yml",
        "branches:\n  - name: main\n    protection:\n      required_pull_request_reviews: {}\n  - name: scratch\n    protection: null\n",
    )
    git(root, "remote", "add", "origin", "git@gitlab.com:acme/web.git")
    commit(root)
    return root


def tree(root: Path) -> dict[str, str]:
    """Every file under root, .git included, with a content hash."""
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            out[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def check_snapshot(name: str, actual: str) -> None:
    path = FIXTURES / name
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8")
    assert actual == path.read_text(encoding="utf-8")


def approve(root: Path, **ask: object) -> None:
    path = root / ".agent-team" / "proposal.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["ask"].update({"approved": True, **ask})
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def init(root: Path, *args: str):
    return runner.invoke(app, ["init", str(root), *args])


def configure(root: Path, *args: str, input: str | None = None):
    return runner.invoke(app, ["configure", str(root), "--canonical", str(CANONICAL), *args], input=input)


# Discovery


def test_python_repo_proposal_matches_snapshot(python_repo: Path) -> None:
    proposal = discover(python_repo)
    check_snapshot("python.yaml", dump_proposal(proposal))
    commands = {(c.kind, c.command): c.verified for c in proposal.commands}
    assert commands == {
        ("build", "uv build"): True,
        ("test", "uv run pytest"): True,
        ("lint", "uv run ruff check ."): False,
        ("typecheck", "uv run mypy ."): False,
    }
    assert proposal.vcs.host == "github" and proposal.vcs.remote == "github.com/acme/widgets"
    assert proposal.vcs.protected_branches == "unknown"
    assert {(s.path, s.kind, s.tracked) for s in proposal.secrets} == {
        (".env", "env-file", False),
        (".env.example", "env-template", True),
        (".github/workflows/ci.yml", "github-actions-secrets", True),
    }
    assert [m.servers for m in proposal.mcp_configs] == [["github"]]


def test_node_repo_proposal_matches_snapshot(node_repo: Path) -> None:
    proposal = discover(node_repo)
    check_snapshot("node.yaml", dump_proposal(proposal))
    assert [(c.kind, c.command, c.verified) for c in proposal.commands] == [
        ("build", "pnpm run build", True),
        ("test", "make test", True),
        ("test", "pnpm run test", True),
        ("lint", "pnpm run lint", True),
        ("typecheck", "pnpm run typecheck", True),
    ]
    assert proposal.vcs.host == "gitlab" and proposal.vcs.remote == "gitlab.com/acme/web"
    assert proposal.vcs.protected_branches == ["main"]
    assert proposal.ci.systems == ["gitlab-ci"]
    assert [(s.path, s.kind) for s in proposal.secrets] == [(".gitlab-ci.yml", "vault")]


def test_missing_tools_and_dependencies_fail_their_check(node_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discovery, "_which", {"make": "/usr/bin/make"}.get)
    proposal = discover(node_repo)
    by_command = {c.command: c for c in proposal.commands}
    assert by_command["make test"].verified
    assert not by_command["pnpm run test"].verified and by_command["pnpm run test"].check == "pnpm not on PATH"


def test_no_secret_value_reaches_the_proposal_or_summary(python_repo: Path) -> None:
    result = init(python_repo)
    assert result.exit_code == 0, result.output
    written = (python_repo / ".agent-team" / "proposal.yaml").read_text(encoding="utf-8")
    for canary in CANARIES:
        assert canary not in written
        assert canary not in result.output


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://user:tok@github.com/acme/widgets.git", ("github.com", "github.com/acme/widgets")),
        ("git@gitlab.com:acme/web.git", ("gitlab.com", "gitlab.com/acme/web")),
        ("ssh://git@bitbucket.org:22/acme/api.git", ("bitbucket.org", "bitbucket.org/acme/api")),
        ("/srv/git/repo.git", ("", "local")),
    ],
)
def test_remote_urls_lose_their_credentials(url: str, expected: tuple[str, str]) -> None:
    assert sanitize_remote(url) == expected


# init


def test_init_writes_only_the_proposal(python_repo: Path) -> None:
    before = tree(python_repo)
    result = init(python_repo)
    assert result.exit_code == 0, result.output
    after = tree(python_repo)
    assert set(after) - set(before) == {".agent-team/proposal.yaml"}
    assert {k: v for k, v in after.items() if k in before} == before
    assert "✓ test       uv run pytest" in result.output
    assert "✗ lint       uv run ruff check ." in result.output
    Proposal.model_validate(yaml.safe_load((python_repo / ".agent-team" / "proposal.yaml").read_text(encoding="utf-8")))


def test_init_refuses_without_gitignore(tmp_path: Path) -> None:
    root = new_repo(tmp_path / "bare", gitignore="")
    result = init(root)
    assert result.exit_code == 2
    assert ".agent-team/ is not gitignored" in result.output
    assert not (root / ".agent-team").exists()


def test_init_keeps_an_existing_proposal_without_force(python_repo: Path) -> None:
    assert init(python_repo).exit_code == 0
    approve(python_repo)
    result = init(python_repo)
    assert result.exit_code == 2 and "--force" in result.output
    assert "approved: true" in (python_repo / ".agent-team" / "proposal.yaml").read_text(encoding="utf-8")
    assert init(python_repo, "--force").exit_code == 0


def test_proposal_rejects_autonomy_above_a3_and_unknown_keys(python_repo: Path) -> None:
    data = discover(python_repo).model_dump(mode="json")
    data["ask"]["autonomy_ceiling"] = "A4"
    with pytest.raises(ValidationError, match="above A3"):
        Proposal.model_validate(data)
    data["ask"]["autonomy_ceiling"] = "A3"
    data["surprise"] = True
    with pytest.raises(ValidationError):
        Proposal.model_validate(data)


# configure


def test_configure_requires_an_approved_proposal(python_repo: Path) -> None:
    assert init(python_repo).exit_code == 0
    before = tree(python_repo)
    result = configure(python_repo, "--yes")
    assert result.exit_code == 2 and "not approved" in result.output
    assert tree(python_repo) == before


def test_configure_refuses_without_gitignore(python_repo: Path) -> None:
    assert init(python_repo).exit_code == 0
    approve(python_repo)
    (python_repo / ".gitignore").write_text(".env\n", encoding="utf-8")
    result = configure(python_repo, "--yes")
    assert result.exit_code == 2 and "not gitignored" in result.output


def test_dry_run_shows_the_diff_and_writes_nothing(python_repo: Path) -> None:
    assert init(python_repo).exit_code == 0
    before = tree(python_repo)
    result = configure(python_repo, "--dry-run")  # no approval needed to look
    assert result.exit_code == 0, result.output
    assert tree(python_repo) == before
    assert "--- a/CLAUDE.md" in result.output and "+++ b/CLAUDE.md" in result.output
    assert "+++ b/.claude/agents/independent-reviewer.md" in result.output
    assert "+++ b/.agent-team/config.yaml" in result.output
    assert "dry run: nothing written" in result.output


def test_declining_confirmation_writes_nothing(python_repo: Path) -> None:
    assert init(python_repo).exit_code == 0
    approve(python_repo)
    before = tree(python_repo)
    result = configure(python_repo, input="n\n")
    assert result.exit_code == 1 and "nothing written" in result.output
    assert tree(python_repo) == before


def test_configure_extends_claude_md_and_writes_only_the_diffed_files(python_repo: Path) -> None:
    assert init(python_repo).exit_code == 0
    approve(python_repo)
    original = (python_repo / "CLAUDE.md").read_text(encoding="utf-8")
    result = configure(python_repo, input="y\n")
    assert result.exit_code == 0, result.output

    claude_md = (python_repo / "CLAUDE.md").read_text(encoding="utf-8")
    assert claude_md.startswith(original)
    assert claude_md.count(BEGIN_MARKER) == 1 and claude_md.count(END_MARKER) == 1
    assert "- test: `uv run pytest`" in claude_md and "ruff" not in claude_md

    agent = (python_repo / ".claude" / "agents" / "independent-reviewer.md").read_text(encoding="utf-8")
    assert agent.startswith("---\nname: independent-reviewer\n") and AGENT_MARKER in agent
    assert "Bash(uv run pytest)" in agent

    config = RepoConfig.model_validate(yaml.safe_load((python_repo / ".agent-team" / "config.yaml").read_text(encoding="utf-8")))
    assert config.commands == {"build": ["uv build"], "test": ["uv run pytest"]}
    assert config.unverified_commands == {"lint": ["uv run ruff check ."], "typecheck": ["uv run mypy ."]}
    agents = [".claude/agents/implementer.md", ".claude/agents/independent-reviewer.md",
              ".claude/agents/repository-archaeologist.md"]  # fmt: skip
    assert config.instruction_files == ["CLAUDE.md"] and config.agents == agents

    implementer = (python_repo / agents[0]).read_text(encoding="utf-8")
    assert "- `Edit(./**)`" in implementer and "- `Bash`\n" not in implementer, "srt does not wrap these: no unconfined Bash"
    archaeologist = (python_repo / ".claude" / "agents" / "repository-archaeologist.md").read_text(encoding="utf-8")
    assert "Bash(uv run pytest)" not in archaeologist, "A0: no test.run, whatever the proposal's ceiling"

    status = git(python_repo, "status", "--porcelain", "--untracked-files=all").splitlines()
    assert sorted(status) == [" M CLAUDE.md", *(f"?? {a}" for a in agents)]


def test_configure_is_idempotent_and_updates_the_block_in_place(python_repo: Path) -> None:
    assert init(python_repo).exit_code == 0
    approve(python_repo)
    assert configure(python_repo, "--yes").exit_code == 0
    first = tree(python_repo)

    again = configure(python_repo, "--yes")
    assert again.exit_code == 0 and "nothing to change" in again.output
    assert tree(python_repo) == first

    claude_md = python_repo / "CLAUDE.md"
    claude_md.write_text(claude_md.read_text(encoding="utf-8") + "\n## Notes added later\n", encoding="utf-8")
    approve(python_repo, draft_prs=False)
    assert configure(python_repo, "--yes").exit_code == 0
    text = claude_md.read_text(encoding="utf-8")
    assert text.count(BEGIN_MARKER) == 1
    assert "do not open pull requests" in text and "open a draft pull request" not in text
    assert text.startswith("# Widgets\n\nUse uv for everything.\n") and text.endswith("## Notes added later\n")


def test_claude_md_that_imports_agents_md_is_left_alone(node_repo: Path) -> None:
    write(node_repo, "CLAUDE.md", "@AGENTS.md\n")
    assert init(node_repo).exit_code == 0
    approve(node_repo)
    result = configure(node_repo, "--yes")
    assert result.exit_code == 0, result.output
    assert (node_repo / "CLAUDE.md").read_text(encoding="utf-8") == "@AGENTS.md\n"
    agents_md = (node_repo / "AGENTS.md").read_text(encoding="utf-8")
    assert agents_md.startswith("# Web\n\nRun pnpm, never npm.\n") and BEGIN_MARKER in agents_md
    assert "- test: `make test`" in agents_md and "- test: `pnpm run test`" in agents_md


def test_unmanaged_agent_file_and_symlinked_claude_dir_are_untouched(python_repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "home" / ".claude"
    write(outside, "agents/keep.md", "user config\n")
    (python_repo / ".claude").symlink_to(outside, target_is_directory=True)
    assert init(python_repo).exit_code == 0
    assert "keep.md" not in (python_repo / ".agent-team" / "proposal.yaml").read_text(encoding="utf-8")
    approve(python_repo)
    before = tree(outside)
    result = configure(python_repo, "--yes")
    assert result.exit_code == 0, result.output
    assert tree(outside) == before
    assert ".claude/agents/independent-reviewer.md is a symlink or sits under one; left untouched" in result.output

    (python_repo / ".claude").unlink()
    write(python_repo, ".claude/agents/independent-reviewer.md", "---\nname: independent-reviewer\n---\nmine\n")
    result = configure(python_repo, "--yes")
    assert result.exit_code == 0, result.output
    assert "was not written by agent-team; left untouched" in result.output
    assert (python_repo / ".claude/agents/independent-reviewer.md").read_text(encoding="utf-8").endswith("mine\n")


def test_claude_md_linked_outside_the_repo_is_never_read_or_written(python_repo: Path, tmp_path: Path) -> None:
    outside = write(tmp_path, "home/.claude/CLAUDE.md", f"{BEGIN_MARKER}\nunclosed, would fail if parsed\n")
    (python_repo / "CLAUDE.md").unlink()
    (python_repo / "CLAUDE.md").symlink_to(outside)
    assert init(python_repo).exit_code == 0
    approve(python_repo)
    result = configure(python_repo, "--yes")
    assert result.exit_code == 0, result.output
    assert "CLAUDE.md is a symlink or sits under one; left untouched" in result.output
    assert outside.read_text(encoding="utf-8") == f"{BEGIN_MARKER}\nunclosed, would fail if parsed\n"


def test_extend_with_block() -> None:
    block = f"{BEGIN_MARKER}\nnew\n{END_MARKER}\n"
    assert extend_with_block(None, block) == block
    assert extend_with_block("# Title", block) == "# Title\n\n" + block
    assert extend_with_block("# Title\n", block) == "# Title\n\n" + block
    existing = f"top\n{BEGIN_MARKER}\nold\n{END_MARKER}\nbottom\n"
    assert extend_with_block(existing, block) == f"top\n{BEGIN_MARKER}\nnew\n{END_MARKER}\nbottom\n"
    for broken in (f"{BEGIN_MARKER}\n", f"{END_MARKER}\n{BEGIN_MARKER}\n", existing + existing):
        with pytest.raises(BootstrapError, match="exactly one pair"):
            extend_with_block(broken, block)
