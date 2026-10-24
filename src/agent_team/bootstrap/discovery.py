"""`agent-team init`: read-only discovery of a target repository (build-order step 8).

Discovery reads files and asks git read-only questions (`config --get`,
`symbolic-ref`, `rev-parse`, `ls-files`); it never runs anything from the
repository and never touches the network. Detected commands are "verified by
dry run" in a narrow, non-executing sense: the tool the command needs resolves
(on PATH as an absolute path, or inside the repository's `.venv/` or
`node_modules/`) and the target or script it names exists. A ✓ says the command
can be found, not that it passes.

The only write is `.agent-team/proposal.yaml`, and only when git ignores `.agent-team/`.
"""

import json
import os
import re
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from agent_team.doctor import _which
from agent_team.runs.store import STATE_DIR, RunStoreError, _git, check_state_dir_ignored, repo_root
from agent_team.schemas.bootstrap import (
    COMMAND_KINDS,
    CiInfo,
    CommandKind,
    DetectedCommand,
    McpConfig,
    Proposal,
    SecretHint,
    VcsInfo,
)

PROPOSAL_NAME = "proposal.yaml"

Which = Callable[[str], str | None]


class BootstrapError(RuntimeError):
    """init or configure cannot proceed safely; the message says what to fix."""


# Remote hostname -> VCS host name.
VCS_HOSTS: tuple[tuple[str, str], ...] = (
    ("github.com", "github"),
    ("gitlab", "gitlab"),
    ("bitbucket.org", "bitbucket"),
    ("dev.azure.com", "azure-devops"),
    ("visualstudio.com", "azure-devops"),
)

# CI system -> file globs that mark it.
CI_SYSTEMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("github-actions", (".github/workflows/*.yml", ".github/workflows/*.yaml")),
    ("gitlab-ci", (".gitlab-ci.yml",)),
    ("circleci", (".circleci/config.yml",)),
    ("jenkins", ("Jenkinsfile",)),
    ("azure-pipelines", ("azure-pipelines.yml",)),
    ("bitbucket-pipelines", ("bitbucket-pipelines.yml",)),
    ("buildkite", (".buildkite/*.yml", ".buildkite/*.yaml")),
    ("travis", (".travis.yml",)),
)

INSTRUCTION_FILES: tuple[str, ...] = (
    "CLAUDE.md", ".claude/CLAUDE.md", "AGENTS.md", ".github/copilot-instructions.md", "GEMINI.md", ".cursorrules",
)  # fmt: skip
INSTRUCTION_DIRS: tuple[str, ...] = (".cursor/rules", ".github/instructions", ".github/agents", ".claude/agents")

MCP_FILES: tuple[str, ...] = (".mcp.json", ".vscode/mcp.json", ".cursor/mcp.json")
DEVCONTAINER_GLOBS: tuple[str, ...] = (".devcontainer.json", ".devcontainer/devcontainer.json", ".devcontainer/*/devcontainer.json")
CODEOWNERS_PATHS: tuple[str, ...] = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS", ".gitlab/CODEOWNERS")

# The secrets walk skips dependency, build and state directories and never follows symlinks.
WALK_SKIP: frozenset[str] = frozenset(
    {".git", "node_modules", ".venv", "venv", STATE_DIR, "__pycache__", "dist", "build", ".tox", "target", "vendor"}
)
WALK_MAX_DEPTH = 4
SCAN_MAX_BYTES = 512 * 1024

ENV_TEMPLATE_SUFFIXES: tuple[str, ...] = (".example", ".sample", ".template", ".dist")
KEY_SUFFIXES: tuple[str, ...] = (".pem", ".key", ".p12", ".pfx")

# References to a secret store inside config files. Only the kind is reported, never the match.
SECRET_REFS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("vault", re.compile(r"VAULT_ADDR|hashicorp/vault-action|\bvault (?:kv|read|login)\b")),
    ("1password", re.compile(r"op://|1password/load-secrets-action")),
    ("aws-secrets-manager", re.compile(r"secretsmanager|aws-secretsmanager-get-secrets", re.IGNORECASE)),
    ("gcp-secret-manager", re.compile(r"gcloud secrets|secretmanager\.googleapis")),
    ("azure-key-vault", re.compile(r"get-keyvault-secrets|vault\.azure\.net")),
    ("doppler", re.compile(r"doppler run|dopplerhq/")),
    ("sops", re.compile(r"\bsops\b")),
    ("github-actions-secrets", re.compile(r"\$\{\{\s*secrets\.")),
)
SCANNED_CONFIG_GLOBS: tuple[str, ...] = (
    "docker-compose*.yml", "docker-compose*.yaml", "compose*.yml", "compose*.yaml", "Makefile", "package.json",
)  # fmt: skip

_MAKE_TARGET = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)\s*:(?!=)", re.MULTILINE)
MAKE_TARGETS: dict[CommandKind, tuple[str, ...]] = {
    "build": ("build",),
    "test": ("test",),
    "lint": ("lint",),
    "typecheck": ("typecheck", "type-check", "check-types", "mypy"),
}
NPM_SCRIPTS: dict[CommandKind, tuple[str, ...]] = {
    "build": ("build",),
    "test": ("test",),
    "lint": ("lint",),
    "typecheck": ("typecheck", "type-check", "check-types", "tsc"),
}
NPM_PLACEHOLDER_TEST = "no test specified"

# Lockfile -> package manager, first match wins.
NODE_LOCKFILES: tuple[tuple[str, str], ...] = (
    ("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("bun.lockb", "bun"), ("bun.lock", "bun"), ("package-lock.json", "npm"),
)  # fmt: skip
PYTHON_LOCKFILES: tuple[tuple[str, str], ...] = (("uv.lock", "uv"), ("poetry.lock", "poetry"), ("pdm.lock", "pdm"))


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _inside(root: Path, path: Path) -> bool:
    """True when `path` resolves inside `root`, so a symlink such as `.claude -> ~/.claude` is never followed."""
    try:
        return path.resolve().is_relative_to(root)
    except OSError:
        return False


def _is_file(root: Path, path: Path) -> bool:
    return _inside(root, path) and path.is_file()


def _is_dir(root: Path, path: Path) -> bool:
    return _inside(root, path) and path.is_dir()


def _glob(root: Path, patterns: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    for pattern in patterns:
        for path in sorted(root.glob(pattern)):
            if _is_file(root, path) and _rel(root, path) not in found:
                found.append(_rel(root, path))
    return found


def _read_text(root: Path, path: Path) -> str | None:
    try:
        if not _is_file(root, path) or path.stat().st_size > SCAN_MAX_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _git_out(root: Path, *args: str) -> str | None:
    result = _git(root, *args)
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


# VCS


def sanitize_remote(url: str) -> tuple[str, str]:
    """(hostname, host/path) of a remote URL with userinfo, port, scheme and `.git` removed."""
    if "://" in url:
        parts = urlsplit(url)
        hostname = parts.hostname or ""
        path = parts.path
    elif ":" in url and not url.startswith("/"):  # scp-like: git@github.com:acme/widgets.git
        user_host, _, path = url.partition(":")
        hostname = user_host.rpartition("@")[2]
    else:  # a local path remote
        return "", "local"
    path = path.strip("/").removesuffix(".git")
    return hostname.lower(), f"{hostname.lower()}/{path}" if path else hostname.lower()


def discover_vcs(root: Path) -> VcsInfo:
    remotes = (_git_out(root, "remote") or "").split()
    name = "origin" if "origin" in remotes else (remotes[0] if remotes else None)
    host, remote = "none", None
    if name is not None:
        url = _git_out(root, "config", "--get", f"remote.{name}.url")
        if url:
            hostname, remote = sanitize_remote(url)
            host = next((h for marker, h in VCS_HOSTS if marker in hostname), "other")

    branch, branch_source = "unknown", "no remote HEAD, main/master branch or current branch"
    if name is not None and (head := _git_out(root, "symbolic-ref", "--quiet", "--short", f"refs/remotes/{name}/HEAD")):
        branch, branch_source = head.removeprefix(f"{name}/"), f"{name}/HEAD"
    elif local := next((b for b in ("main", "master") if _git_out(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{b}")), None):
        branch, branch_source = local, "local branch"
    elif current := _git_out(root, "symbolic-ref", "--quiet", "--short", "HEAD"):
        branch, branch_source = current, "current branch"

    protected, protected_source = _protected_branches(root)
    return VcsInfo(
        host=host,
        remote=remote,
        default_branch=branch,
        default_branch_source=branch_source,
        protected_branches=protected,
        protected_branches_source=protected_source,
    )


def _protected_branches(root: Path) -> tuple[list[str] | str, str]:
    """Branch protection from files in the repository; the hosting API needs auth, so it is never called."""
    settings = root / ".github" / "settings.yml"
    text = _read_text(root, settings)
    if text is not None:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError:
            data = None
        branches = data.get("branches") if isinstance(data, dict) else None
        if isinstance(branches, list):
            names = [
                str(b["name"]) for b in branches if isinstance(b, dict) and b.get("name") and b.get("protection") is not None
            ]
            return names, ".github/settings.yml"
    return "unknown", "not discoverable without an authenticated API call"


# CI


def discover_ci(root: Path) -> CiInfo:
    systems: list[str] = []
    files: list[str] = []
    for system, patterns in CI_SYSTEMS:
        found = _glob(root, patterns)
        if found:
            systems.append(system)
            files.extend(found)
    return CiInfo(systems=systems, files=files)


# Commands


class _Checker:
    """Non-executing dry-run checks: does the tool a command needs resolve here?"""

    def __init__(self, root: Path, which: Which) -> None:
        self.root = root
        self.which = which

    def on_path(self, tool: str) -> tuple[bool, str]:
        return (True, f"{tool} on PATH") if self.which(tool) else (False, f"{tool} not on PATH")

    def in_venv(self, tool: str) -> bool:
        venv = self.root / ".venv"
        return (venv / "bin" / tool).is_file() or (venv / "Scripts" / f"{tool}.exe").is_file()

    def python_tool(self, runner: str | None, tool: str) -> tuple[bool, str]:
        if runner is not None:
            ok, note = self.on_path(runner)
            if not ok:
                return ok, note
            if self.in_venv(tool):
                return True, f"{runner} on PATH; {tool} in .venv"
            return False, f"{tool} not in .venv (dependencies not installed here)"
        if self.in_venv(tool):
            return True, f"{tool} in .venv"
        return self.on_path(tool)

    def node(self, manager: str, what: str) -> tuple[bool, str]:
        ok, note = self.on_path(manager)
        if not ok:
            return ok, note
        if not (self.root / "node_modules").is_dir():
            return False, "node_modules missing (dependencies not installed)"
        return True, f"{manager} on PATH; {what}"


def _python_deps(data: dict[str, Any]) -> set[str]:
    """Lower-cased distribution names from every dependency list pyproject.toml can hold."""
    specs: list[str] = []
    project = data.get("project", {})
    specs += project.get("dependencies", [])
    for group in project.get("optional-dependencies", {}).values():
        specs += group
    for group in data.get("dependency-groups", {}).values():
        specs += [s for s in group if isinstance(s, str)]
    tool = data.get("tool", {})
    specs += tool.get("uv", {}).get("dev-dependencies", [])
    poetry = tool.get("poetry", {})
    names = set(poetry.get("dependencies", {})) | set(poetry.get("dev-dependencies", {}))
    for group in poetry.get("group", {}).values():
        names |= set(group.get("dependencies", {}))
    for spec in specs:
        if isinstance(spec, str) and (match := re.match(r"[A-Za-z0-9_.-]+", spec)):
            names.add(match.group(0))
    return {n.lower().replace("_", "-") for n in names}


def _python_commands(root: Path, check: _Checker) -> list[DetectedCommand]:
    text = _read_text(root, root / "pyproject.toml")
    try:
        data = tomllib.loads(text) if text is not None else None
    except tomllib.TOMLDecodeError:
        data = None
    if data is None:
        return []
    runner = next((tool for lock, tool in PYTHON_LOCKFILES if (root / lock).is_file()), None)
    prefix = f"{runner} run " if runner else ""
    deps = _python_deps(data)
    tool = data.get("tool", {})
    found: list[DetectedCommand] = []

    def add(kind: CommandKind, name: str, argv: str, why: str) -> None:
        ok, note = check.python_tool(runner, name)
        found.append(DetectedCommand(kind=kind, command=f"{prefix}{argv}", source=f"pyproject.toml ({why})", verified=ok, check=note))

    if runner is not None and "build-system" in data:
        ok, note = check.on_path(runner)
        found.append(DetectedCommand(kind="build", command=f"{runner} build", source="pyproject.toml ([build-system])", verified=ok, check=note))
    if "pytest" in tool or "pytest" in deps or (root / "pytest.ini").is_file() or (root / "conftest.py").is_file():
        add("test", "pytest", "pytest", "pytest configured or a dependency")
    if "ruff" in tool or "ruff" in deps:
        add("lint", "ruff", "ruff check .", "ruff configured or a dependency")
    elif "flake8" in deps or (root / ".flake8").is_file():
        add("lint", "flake8", "flake8", "flake8 configured or a dependency")
    if "mypy" in tool or "mypy" in deps or (root / "mypy.ini").is_file():
        add("typecheck", "mypy", "mypy .", "mypy configured or a dependency")
    elif "pyright" in tool or "pyright" in deps or (root / "pyrightconfig.json").is_file():
        add("typecheck", "pyright", "pyright", "pyright configured or a dependency")
    return found


def _node_commands(root: Path, check: _Checker) -> list[DetectedCommand]:
    text = _read_text(root, root / "package.json")
    try:
        data = json.loads(text) if text is not None else None
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        return []
    manager = next((pm for lock, pm in NODE_LOCKFILES if (root / lock).is_file()), "npm")
    scripts = data.get("scripts") if isinstance(data.get("scripts"), dict) else {}
    found: list[DetectedCommand] = []
    for kind in COMMAND_KINDS:
        script = next((s for s in NPM_SCRIPTS[kind] if isinstance(scripts.get(s), str)), None)
        if script is None or (script == "test" and NPM_PLACEHOLDER_TEST in scripts[script]):
            continue
        ok, note = check.node(manager, f"script {script!r} defined")
        found.append(
            DetectedCommand(kind=kind, command=f"{manager} run {script}", source=f"package.json script {script}", verified=ok, check=note)
        )
    if not any(c.kind == "typecheck" for c in found) and (root / "tsconfig.json").is_file():
        ok = (root / "node_modules" / ".bin" / "tsc").is_file()
        note = "tsc in node_modules/.bin" if ok else "tsc not in node_modules/.bin (dependencies not installed)"
        found.append(DetectedCommand(kind="typecheck", command="npx tsc --noEmit", source="tsconfig.json", verified=ok, check=note))
    return found


def _make_commands(root: Path, check: _Checker) -> list[DetectedCommand]:
    # `make -n` is not used: it still runs `$(shell ...)` and `+` recipe lines from the Makefile.
    text = _read_text(root, root / "Makefile")
    if text is None:
        return []
    targets = set(_MAKE_TARGET.findall(text))
    found: list[DetectedCommand] = []
    for kind in COMMAND_KINDS:
        target = next((t for t in MAKE_TARGETS[kind] if t in targets), None)
        if target is not None:
            ok, note = check.on_path("make")
            note = f"{note}; target {target!r} defined" if ok else note
            found.append(DetectedCommand(kind=kind, command=f"make {target}", source=f"Makefile target {target}", verified=ok, check=note))
    return found


def _toolchain_commands(root: Path, check: _Checker) -> list[DetectedCommand]:
    found: list[DetectedCommand] = []
    toolchains = (
        ("Cargo.toml", "cargo", (("build", "cargo build"), ("test", "cargo test"), ("lint", "cargo clippy"))),
        ("go.mod", "go", (("build", "go build ./..."), ("test", "go test ./..."), ("lint", "go vet ./..."))),
    )
    for marker, tool, commands in toolchains:
        if (root / marker).is_file():
            ok, note = check.on_path(tool)
            for kind, command in commands:
                found.append(DetectedCommand(kind=kind, command=command, source=marker, verified=ok, check=note))  # type: ignore[arg-type]
    return found


def discover_commands(root: Path, which: Which | None = None) -> list[DetectedCommand]:
    """Every build/test/lint/typecheck command found, Makefile first, grouped by kind; nothing is run."""
    check = _Checker(root, which or _which)
    found = [
        *_make_commands(root, check),
        *_node_commands(root, check),
        *_python_commands(root, check),
        *_toolchain_commands(root, check),
    ]
    unique: dict[tuple[str, str], DetectedCommand] = {}
    for command in found:
        unique.setdefault((command.kind, command.command), command)
    return sorted(unique.values(), key=lambda c: COMMAND_KINDS.index(c.kind))


# Instructions, MCP, devcontainer, codeowners


def discover_instruction_files(root: Path) -> list[str]:
    found = [p for p in INSTRUCTION_FILES if _is_file(root, root / p)]
    for directory in INSTRUCTION_DIRS:
        if _is_dir(root, root / directory):
            found.extend(_rel(root, p) for p in sorted((root / directory).rglob("*")) if _is_file(root, p))
    return found


def discover_mcp(root: Path) -> list[McpConfig]:
    configs: list[McpConfig] = []
    for rel in MCP_FILES:
        text = _read_text(root, root / rel)
        if text is None:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        servers = (data.get("mcpServers") or data.get("servers")) if isinstance(data, dict) else None
        configs.append(McpConfig(path=rel, servers=sorted(servers) if isinstance(servers, dict) else []))
    return configs


# Secrets: paths and kinds only


def _walk(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        depth = len(Path(dirpath).relative_to(root).parts)
        dirnames[:] = sorted(d for d in dirnames if d not in WALK_SKIP and depth < WALK_MAX_DEPTH)
        files.extend(Path(dirpath) / f for f in sorted(filenames))
    return files


def _file_secret_kind(name: str) -> str | None:
    if name == ".envrc":
        return "direnv"
    if name == ".sops.yaml":
        return "sops"
    if name == ".env" or name.startswith(".env.") or name.endswith(".env"):
        return "env-template" if name.endswith(ENV_TEMPLATE_SUFFIXES) else "env-file"
    if name.endswith(KEY_SUFFIXES):
        return "key-file"
    return None


def discover_secrets(root: Path, scanned: list[str]) -> list[SecretHint]:
    """Secret files by name and secret-store references inside `scanned` config files; no value is read out."""
    listing = _git(root, "ls-files", "-z")
    tracked = set(listing.stdout.split("\0")) if listing.returncode == 0 else set()
    hints: list[SecretHint] = []
    for path in _walk(root):
        if path.is_symlink():
            continue
        kind = _file_secret_kind(path.name)
        if kind is not None:
            rel = _rel(root, path)
            hints.append(SecretHint(path=rel, kind=kind, tracked=rel in tracked))
    for rel in scanned:
        text = _read_text(root, root / rel)
        if text is None:
            continue
        for kind, pattern in SECRET_REFS:
            if pattern.search(text):
                hints.append(SecretHint(path=rel, kind=kind, tracked=rel in tracked))
    return hints


# init


def discover(path: Path, which: Which | None = None) -> Proposal:
    """Build the proposal for the repository containing `path`. Reads only; writes nothing."""
    root = repo_root(path).resolve()
    ci = discover_ci(root)
    devcontainer = _glob(root, DEVCONTAINER_GLOBS)
    scanned = list(dict.fromkeys([*ci.files, *devcontainer, *_glob(root, SCANNED_CONFIG_GLOBS)]))
    return Proposal(
        vcs=discover_vcs(root),
        ci=ci,
        commands=discover_commands(root, which),
        instruction_files=discover_instruction_files(root),
        mcp_configs=discover_mcp(root),
        secrets=discover_secrets(root, scanned),
        devcontainer=devcontainer,
        codeowners=next((p for p in CODEOWNERS_PATHS if _is_file(root, root / p)), None),
    )


def check_state_file_ignored(root: Path, name: str) -> None:
    """Fail unless git ignores `.agent-team/` and the state file `name` inside it."""
    try:
        check_state_dir_ignored(root)
    except RunStoreError as exc:
        raise BootstrapError(str(exc).replace("the run record is", "agent-team state is")) from exc
    if _git(root, "check-ignore", "-q", "--no-index", f"{STATE_DIR}/{name}").returncode != 0:
        raise BootstrapError(f"{STATE_DIR}/{name} is not gitignored in {root}; add '{STATE_DIR}/' to .gitignore")


def dump_proposal(proposal: Proposal) -> str:
    header = (
        "# agent-team proposal: what `agent-team init` found, and the ask under `ask:`.\n"
        "# Edit anything, set ask.approved: true, then run `agent-team configure`.\n"
        "# verified: true (✓) means the command's tool and target were found; nothing was run.\n"
    )
    return header + yaml.safe_dump(proposal.model_dump(mode="json"), sort_keys=False, allow_unicode=True)


def write_proposal(path: Path, *, force: bool = False, which: Which | None = None) -> tuple[Proposal, Path]:
    """Discover, then write `.agent-team/proposal.yaml`; refuses unless `.agent-team/` is gitignored."""
    root = repo_root(path)
    check_state_file_ignored(root, PROPOSAL_NAME)
    if (root / STATE_DIR).is_symlink():
        raise BootstrapError(f"{STATE_DIR} in {root} is a symlink; agent-team state must be a real directory")
    target = root / STATE_DIR / PROPOSAL_NAME
    if target.exists() and not force:
        raise BootstrapError(f"{STATE_DIR}/{PROPOSAL_NAME} already exists in {root}; pass --force to replace it")
    proposal = discover(root, which)
    target.parent.mkdir(exist_ok=True)
    target.write_text(dump_proposal(proposal), encoding="utf-8")
    return proposal, target


def render_summary(proposal: Proposal) -> str:
    vcs = proposal.vcs
    protected = ", ".join(vcs.protected_branches) if isinstance(vcs.protected_branches, list) else "unknown"
    lines = [
        f"vcs           {vcs.host}" + (f"  {vcs.remote}" if vcs.remote else ""),
        f"default       {vcs.default_branch} ({vcs.default_branch_source})",
        f"protected     {protected or 'none'} ({vcs.protected_branches_source})",
        "ci            " + (", ".join(proposal.ci.systems) or "none") + (f"  {', '.join(proposal.ci.files)}" if proposal.ci.files else ""),
        "commands" + ("" if proposal.commands else "      none detected"),
    ]
    for c in proposal.commands:
        lines.append(f"  {'✓' if c.verified else '✗'} {c.kind:<10} {c.command:<24} {c.check}")
    lines += [
        "instructions  " + (", ".join(proposal.instruction_files) or "none"),
        "mcp           " + (", ".join(f"{m.path} ({', '.join(m.servers) or 'no servers'})" for m in proposal.mcp_configs) or "none"),
        "secrets       " + (", ".join(f"{s.path} ({s.kind}{', tracked' if s.tracked else ''})" for s in proposal.secrets) or "none found"),
        "devcontainer  " + (", ".join(proposal.devcontainer) or "none"),
        f"codeowners    {proposal.codeowners or 'none'}",
        f"ask           runtimes {', '.join(proposal.ask.runtimes)}; privacy {', '.join(proposal.ask.privacy_classes)}; "
        f"autonomy {proposal.ask.autonomy_ceiling}; draft PRs {'yes' if proposal.ask.draft_prs else 'no'}; "
        f"approved {'yes' if proposal.ask.approved else 'no'}",
    ]
    return "\n".join(lines)
