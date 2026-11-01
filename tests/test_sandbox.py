"""srt wrapper (build-order step 5): settings, autonomy cap, and real sandbox behavior.

Tests marked `srt` run commands under the real sandbox. They skip when srt is
not installed, unless AGENT_TEAM_REQUIRE_SRT=1 (set in CI), which turns a
missing srt into a failure so CI can never pass by skipping the sandbox.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_team.environments import UNSANDBOXED_CEILING, create, effective_ceiling, srt_path
from agent_team.environments.sandbox import DENY_READ_HOME, build_settings, extend_settings, srt_deny_read
from agent_team.schemas import EnvironmentSpec

ALLOWED = "github.com"
DENIED = "example.com"


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


def spec(allowed: list[str]) -> EnvironmentSpec:
    return EnvironmentSpec.model_validate(
        {
            "kind": "environment",
            "name": "test",
            "description": "test environment",
            "tier": "srt",
            "network": {"allowed_domains": allowed},
        }
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "target"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "test")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "commit.gpgsign", "false")  # host git config may sign through a service the sandbox blocks
    (root / ".gitignore").write_text(".agent-team/\n", encoding="utf-8")
    (root / "app.py").write_text("print('hello')\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "init")
    return root.resolve()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A fake home with a credential the sandbox must not read."""
    fake = (tmp_path / "home").resolve()
    (fake / ".ssh").mkdir(parents=True)
    (fake / ".ssh" / "id_test").write_text("SANDBOX-READ-CANARY\n", encoding="utf-8")
    return fake


# --- pure ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("requested", "sandboxed", "expected"),
    [("A0", False, "A0"), ("A2", False, "A2"), ("A3", False, "A2"), ("A4", False, "A2"), ("A3", True, "A3"), ("A4", True, "A4")],
)
def test_effective_ceiling(requested: str, sandboxed: bool, expected: str) -> None:
    assert effective_ceiling(requested, sandboxed) == expected  # type: ignore[arg-type]
    assert UNSANDBOXED_CEILING == "A2"


def test_settings_allow_writes_only_where_a_run_needs_them(tmp_path: Path) -> None:
    wt, git_dir, tmp, home = (tmp_path / p for p in ("wt", ".git", "tmp", "home"))
    settings = build_settings(spec([ALLOWED]), run_id="run-1", worktree=wt, git_dir=git_dir, tmp_dir=tmp, home=home)
    assert settings["network"] == {"allowedDomains": [ALLOWED], "deniedDomains": []}
    fs = settings["filesystem"]
    assert fs["allowWrite"] == [
        str(wt),
        str(tmp),
        str(git_dir / "objects"),
        str(git_dir / "worktrees" / "run-1"),
        str(git_dir / "refs" / "heads" / "agent-team"),
        str(git_dir / "logs" / "refs" / "heads" / "agent-team"),
    ]
    assert str(git_dir) not in fs["allowWrite"], "never the whole .git: hooks, config and main stay read-only"
    assert fs["denyWrite"] == [str(wt / ".git")]
    assert fs["denyRead"] == [str(home / n) for n in DENY_READ_HOME]
    assert DENY_READ_HOME[:5] == (".ssh", ".aws", ".config/gh", ".git-credentials", ".netrc")


def test_empty_allowlist_means_no_network(tmp_path: Path) -> None:
    settings = build_settings(spec([]), run_id="r", worktree=tmp_path, git_dir=tmp_path, tmp_dir=tmp_path, home=tmp_path)
    assert settings["network"]["allowedDomains"] == []


@pytest.mark.parametrize("bad", ["http://github.com", "github", "GitHub.com", "*github.com", "a..b", "x.com:999999"])
def test_bad_domains_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        spec([bad])


# --- real srt -----------------------------------------------------------------


@pytest.fixture
def srt() -> str:
    path = srt_path()
    if path is None:
        if os.environ.get("AGENT_TEAM_REQUIRE_SRT") == "1":
            pytest.fail("AGENT_TEAM_REQUIRE_SRT=1 but srt is not on PATH")
        pytest.skip("srt not installed")
    return path


def run_in(env, argv: list[str]) -> subprocess.CompletedProcess[str]:  # noqa: ANN001
    return subprocess.run(
        env.sandbox.wrap(argv),
        cwd=env.path,
        env=env.sandbox.env(dict(os.environ)),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.srt
def test_srt_allows_writes_in_worktree_and_tmpdir(repo: Path, home: Path, srt: str) -> None:
    env = create(repo, "run-1", spec([]), home=home)
    assert env.sandbox.sandboxed and env.autonomy_ceiling == "A3"
    result = run_in(env, ["sh", "-c", 'echo ok > inside.txt && echo ok > "$TMPDIR/t.txt"'])
    assert result.returncode == 0, result.stderr
    assert (env.path / "inside.txt").read_text() == "ok\n"


@pytest.mark.srt
def test_srt_blocks_writes_outside_worktree(repo: Path, home: Path, srt: str) -> None:
    env = create(repo, "run-1", spec([]), home=home)
    for target in (repo / "app.py", repo / ".agent-team" / "sandbox" / "run-1.json", home / "planted.txt"):
        before = target.read_bytes() if target.exists() else None
        result = run_in(env, ["sh", "-c", f"echo pwned > '{target}'"])
        assert result.returncode != 0, f"wrote {target}"
        assert (target.read_bytes() if target.exists() else None) == before


@pytest.mark.srt
def test_srt_blocks_reading_denied_credentials(repo: Path, home: Path, srt: str) -> None:
    env = create(repo, "run-1", spec([]), home=home)
    result = run_in(env, ["cat", str(home / ".ssh" / "id_test")])
    assert result.returncode != 0
    assert "SANDBOX-READ-CANARY" not in result.stdout


@pytest.mark.srt
def test_srt_lets_git_commit_but_not_touch_main_hooks_or_config(repo: Path, home: Path, srt: str) -> None:
    env = create(repo, "run-1", spec([]), home=home)
    commit = run_in(env, ["sh", "-c", "echo fix > app.py && git commit -qam fix"])
    assert commit.returncode == 0, commit.stderr
    assert git(repo, "log", "-1", "--format=%s", "agent-team/run-1") == "fix"

    hook = repo / ".git" / "hooks" / "pre-commit"
    assert run_in(env, ["sh", "-c", f"echo 'exit 0' > '{hook}'"]).returncode != 0
    assert not hook.exists()
    assert run_in(env, ["git", "config", "core.hooksPath", "/tmp"]).returncode != 0
    assert subprocess.run(["git", "-C", str(repo), "config", "--get", "core.hooksPath"], capture_output=True).returncode == 1

    main_before = git(repo, "rev-parse", "main")
    moved = run_in(env, ["git", "update-ref", "refs/heads/main", "HEAD"])
    assert moved.returncode != 0
    assert git(repo, "rev-parse", "main") == main_before, "the sandbox must never move main"


@pytest.mark.srt
def test_srt_denies_domains_off_the_allowlist(repo: Path, home: Path, srt: str) -> None:
    env = create(repo, "run-1", spec([ALLOWED]), home=home)
    result = run_in(env, ["curl", "-sS", "-m", "15", "-o", "/dev/null", f"https://{DENIED}"])
    assert result.returncode != 0


@pytest.mark.srt
def test_srt_allows_domains_on_the_allowlist(repo: Path, home: Path, srt: str) -> None:
    env = create(repo, "run-1", spec([ALLOWED]), home=home)
    result = run_in(env, ["curl", "-sS", "-m", "15", "-o", "/dev/null", f"https://{ALLOWED}"])
    assert result.returncode == 0, result.stderr


def test_every_sandboxed_run_denies_home_logins(tmp_path: Path) -> None:
    """The base settings every runtime starts from, not only inspect's and Codex's extras."""
    home = tmp_path / "home"
    settings = build_settings(spec([]), run_id="r", worktree=tmp_path, git_dir=tmp_path, tmp_dir=tmp_path, home=home)
    denied = set(settings["filesystem"]["denyRead"])  # type: ignore[index]
    for name in (".docker", ".kube", ".npmrc", ".pypirc", ".azure", ".vault-token", ".cache/huggingface/token", ".codex"):
        assert str(home / name) in denied, name
    assert str(home / ".gnupg") not in denied, "a worker's own signed commit keeps working"


def test_tracked_env_templates_are_readable_but_a_real_env_is_not(repo: Path, tmp_path: Path) -> None:
    from agent_team.environments.sandbox import env_template_files

    (repo / ".env.example").write_text("API_KEY=replace-me\n", encoding="utf-8")
    (repo / "cfg").mkdir()
    (repo / "cfg" / ".env.sample").write_text("X=1\n", encoding="utf-8")
    (repo / ".env").write_text("API_KEY=real\n", encoding="utf-8")  # untracked: never committed
    (repo / ".env.local").write_text("X=1\n", encoding="utf-8")
    git(repo, "add", "-f", ".env.example", "cfg/.env.sample", ".env.local")  # a global gitignore may hide .env*
    git(repo, "commit", "-q", "-m", "templates")
    assert env_template_files(repo) == [repo / ".env.example", repo / "cfg" / ".env.sample"]
    settings = build_settings(spec([]), run_id="r", worktree=repo, git_dir=repo / ".git", tmp_dir=tmp_path, home=tmp_path)
    assert settings["filesystem"]["allowRead"] == [str(repo / ".env.example"), str(repo / "cfg" / ".env.sample")]


@pytest.mark.srt
def test_srt_lets_a_test_runner_stat_env_example_and_still_hides_env(repo: Path, home: Path, srt: str) -> None:
    """pytest stats every rootdir entry while collecting; a denied stat on .env.example killed every run on ARDA."""
    (repo / ".env.example").write_text("API_KEY=replace-me\n", encoding="utf-8")
    git(repo, "add", "-f", ".env.example")
    git(repo, "commit", "-q", "-m", "template")
    env = create(repo, "run-1", spec([]), home=home)
    # The policy's `.env*` protection arrives through the adapter, after prepare(): same order as a run.
    extend_settings(env.sandbox, deny_read=srt_deny_read([".env*"], [env.path, repo], home, [env.path]))
    probe = "import os,sys; os.stat(sys.argv[1]); print('stat-ok')"
    ok = run_in(env, [sys.executable, "-c", probe, str(env.path / ".env.example")])
    assert "stat-ok" in ok.stdout, ok.stdout + ok.stderr
    (env.path / ".env").write_text("API_KEY=SANDBOX-READ-CANARY\n", encoding="utf-8")
    denied = run_in(env, ["cat", str(env.path / ".env")])
    assert "SANDBOX-READ-CANARY" not in denied.stdout
