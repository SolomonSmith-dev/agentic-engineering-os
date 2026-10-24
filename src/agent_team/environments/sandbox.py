"""The `srt` wrapper: deny-all network plus an allowlist, writes only where a run needs them.

The settings file lives in `.agent-team/sandbox/`, outside the worktree, so the
sandboxed process cannot rewrite its own rules. Writes are allowed only in the
worktree, the run's temp directory, and the parts of `.git` a commit on the
run's branch touches: the object store, the worktree's own admin directory, and
the `agent-team/` branch namespace. `main` and every other ref stay read-only,
and so do the git hooks and config, which would let a sandboxed commit run code
outside the sandbox.
"""

import json
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from agent_team.schemas.common import AUTONOMY_ORDER, Autonomy
from agent_team.schemas.environment import EnvironmentSpec

# Ceiling for any run that is not wrapped in srt (worktree-only isolation).
UNSANDBOXED_CEILING: Autonomy = "A2"

# Read-denied for every sandboxed process: credentials that never belong in a run. The GitHub
# ones are the orchestrator's (it pushes and runs gh outside the sandbox), never a worker's.
DENY_READ_HOME: tuple[str, ...] = (".ssh", ".aws", ".config/gh", ".git-credentials", ".netrc")


def srt_path() -> str | None:
    return shutil.which("srt")


def effective_ceiling(requested: Autonomy, sandboxed: bool) -> Autonomy:
    """The autonomy a run may use: `requested`, capped at A2 without srt."""
    if sandboxed or AUTONOMY_ORDER[requested] <= AUTONOMY_ORDER[UNSANDBOXED_CEILING]:
        return requested
    return UNSANDBOXED_CEILING


# Branch namespace the allocator creates run branches in (refs/heads/agent-team/<run_id>).
BRANCH_NAMESPACE = "agent-team"


def git_write_paths(git_dir: Path, run_id: str) -> list[Path]:
    """The parts of the shared `.git` a commit on the run's branch writes to."""
    return [
        git_dir / "objects",
        git_dir / "worktrees" / run_id,
        git_dir / "refs" / "heads" / BRANCH_NAMESPACE,
        git_dir / "logs" / "refs" / "heads" / BRANCH_NAMESPACE,
    ]


def build_settings(
    spec: EnvironmentSpec, *, run_id: str, worktree: Path, git_dir: Path, tmp_dir: Path, home: Path
) -> dict[str, object]:
    """srt settings for one run. Every path is absolute."""
    extra = [str(home / p[2:]) if p.startswith("~/") else p for p in spec.deny_read]
    return {
        "network": {
            "allowedDomains": list(spec.network.allowed_domains),
            "deniedDomains": [],
        },
        "filesystem": {
            "denyRead": [*(str(home / name) for name in DENY_READ_HOME), *extra],
            "allowWrite": [str(worktree), str(tmp_dir), *(str(p) for p in git_write_paths(git_dir, run_id))],
            "denyWrite": [str(worktree / ".git")],
        },
    }


def settings_path(state_dir: Path, run_id: str) -> Path:
    return state_dir / "sandbox" / f"{run_id}.json"


@dataclass(frozen=True)
class Sandbox:
    """How to run a command for one run: through srt, or bare when srt is absent or not asked for."""

    srt: str | None
    settings_path: Path | None
    tmp_dir: Path
    port_offset: int

    @property
    def sandboxed(self) -> bool:
        return self.srt is not None

    def wrap(self, argv: list[str]) -> list[str]:
        if self.srt is None or self.settings_path is None:
            return list(argv)
        # TMPDIR is set inside the sandbox, not for srt itself: srt puts its proxy bridge
        # sockets in TMPDIR, and a deep per-run path overflows the unix socket path limit.
        return [self.srt, "--settings", str(self.settings_path), "--", "env", f"TMPDIR={self.tmp_dir}", *argv]

    def env(self, base: dict[str, str]) -> dict[str, str]:
        """`base` plus the run's port offset, and its temp directory when `wrap` does not set it."""
        env = dict(base)
        if not self.sandboxed:
            env["TMPDIR"] = str(self.tmp_dir)
        env["AGENT_TEAM_PORT_OFFSET"] = str(self.port_offset)
        return env


def extend_settings(
    sandbox: Sandbox, *, deny_read: Sequence[object] = (), allowed_domains: Sequence[str] = ()
) -> None:
    """Add read denies and allowed domains to a prepared run's srt settings: a runtime adapter's extras.

    It only narrows reads or names the runtime's own API hosts; writes stay as `prepare` set them.
    """
    if sandbox.settings_path is None:
        raise ValueError("the run is not sandboxed; there are no srt settings to extend")
    settings = json.loads(sandbox.settings_path.read_text(encoding="utf-8"))
    denied = settings["filesystem"]["denyRead"]
    denied.extend(e for e in deny_read if e not in denied)
    domains = settings["network"]["allowedDomains"]
    domains.extend(d for d in allowed_domains if d not in domains)
    sandbox.settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def prepare(
    spec: EnvironmentSpec,
    *,
    run_id: str,
    state_dir: Path,
    worktree: Path,
    git_dir: Path,
    port_offset: int,
    home: Path | None = None,
    srt: str | None = None,
) -> Sandbox:
    """Write the run's srt settings (when srt applies) and return its Sandbox."""
    tmp_dir = state_dir / "tmp" / run_id
    tmp_dir.mkdir(parents=True, exist_ok=True)
    binary = (srt or srt_path()) if spec.tier == "srt" else None
    if binary is None:
        return Sandbox(srt=None, settings_path=None, tmp_dir=tmp_dir, port_offset=port_offset)
    for path in git_write_paths(git_dir, run_id):
        path.mkdir(parents=True, exist_ok=True)  # srt can only allow writes under paths that exist
    settings = build_settings(
        spec, run_id=run_id, worktree=worktree, git_dir=git_dir, tmp_dir=tmp_dir, home=home or Path.home()
    )
    path = settings_path(state_dir, run_id)
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return Sandbox(srt=binary, settings_path=path, tmp_dir=tmp_dir, port_offset=port_offset)
