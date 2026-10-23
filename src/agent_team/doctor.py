"""Host, runtime, auth, egress and managed-settings probes for `agent-team doctor`.

Read-only by construction: nothing here writes to disk or opens anything under
~/.claude. Each probe goes through one of the small seams below (`_which`,
`_run`, `_head`, `_exists`, `_host_facts`, `_env`) so tests can replace every
contact with the real host.
"""

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping

from agent_team.schemas.doctor import (
    AuthReport,
    DoctorReport,
    EgressReport,
    HostReport,
    ManagedSettingsReport,
    RuntimeReport,
)

RUNTIMES: tuple[str, ...] = ("claude", "srt", "gh", "git")
EGRESS_URL = "https://api.anthropic.com"
EGRESS_TIMEOUT_S = 3.0
PROBE_TIMEOUT_S = 10.0

# Where Claude Code reads enterprise managed settings, by sys.platform prefix.
MANAGED_SETTINGS_PATHS: dict[str, str] = {
    "darwin": "/Library/Application Support/ClaudeCode/managed-settings.json",
    "linux": "/etc/claude-code/managed-settings.json",
    "win32": r"C:\Program Files\ClaudeCode\managed-settings.json",
}

_VERSION = re.compile(r"\d+\.\d+(?:\.\d+)?")

# Env vars a `--version` probe never needs; dropped so a tool cannot see them.
_CREDENTIAL_ENV = re.compile(r"TOKEN|KEY|SECRET|PASSWORD|CREDENTIAL", re.IGNORECASE)


def _which(name: str) -> str | None:
    """Absolute path of `name` on PATH, or None.

    A relative hit (a `.` or empty PATH entry, or Windows' current-directory
    lookup) is refused so a binary planted in the repo under inspection never runs.
    """
    path = shutil.which(name)
    if path is None or not os.path.isabs(path):
        return None
    return path


def _run(argv: list[str], env: Mapping[str, str] | None = None) -> tuple[int, str]:
    """Run a probe command; stdout only, stderr discarded, never raises."""
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
            check=False,
            stdin=subprocess.DEVNULL,
            env=dict(env) if env is not None else None,
        )
    except (OSError, subprocess.SubprocessError):
        return 127, ""
    return proc.returncode, proc.stdout


def _head(url: str, timeout: float) -> bool:
    """True when the host answers HTTPS at all; any HTTP status counts as reachable."""
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _exists(path: str) -> bool:
    return os.path.isfile(path)


def _host_facts() -> dict[str, str]:
    os_version = platform.mac_ver()[0] if sys.platform == "darwin" else platform.release()
    return {
        "os": platform.system(),
        "os_version": os_version or "unknown",
        "arch": platform.machine(),
        "python": platform.python_version(),
        "platform": sys.platform,
    }


def _env() -> Mapping[str, str]:
    return os.environ


def extract_version(text: str) -> str | None:
    """First dotted version number in `text`; the rest of the output is discarded."""
    match = _VERSION.search(text)
    return match.group(0) if match else None


def _scrubbed_env() -> dict[str, str]:
    return {k: v for k, v in _env().items() if not _CREDENTIAL_ENV.search(k)}


def probe_version(name: str) -> str | None:
    path = _which(name)
    if path is None:
        return None
    code, out = _run([path, "--version"], env=_scrubbed_env())
    return extract_version(out) if code == 0 else None


def probe_host() -> HostReport:
    facts = _host_facts()
    return HostReport(
        os=facts["os"],
        os_version=facts["os_version"],
        arch=facts["arch"],
        python=facts["python"],
        uv=probe_version("uv"),
    )


def probe_runtimes() -> list[RuntimeReport]:
    reports = []
    for name in RUNTIMES:
        present = _which(name) is not None
        reports.append(RuntimeReport(name=name, present=present, version=probe_version(name) if present else None))
    return reports


def _claude_auth() -> bool:
    env = _env()
    if env.get("ANTHROPIC_API_KEY") or env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return True
    path = _which("claude")
    if path is None:
        return False
    code, out = _run([path, "auth", "status", "--json"])
    if code != 0:
        return False
    try:
        status = json.loads(out)
    except ValueError:
        return False
    # Only the boolean is read; email, org and every other field are dropped here.
    return isinstance(status, dict) and status.get("loggedIn") is True


def _gh_auth() -> bool:
    env = _env()
    if env.get("GH_TOKEN") or env.get("GITHUB_TOKEN"):
        return True
    path = _which("gh")
    if path is None:
        return False
    code, _ = _run([path, "auth", "status", "--hostname", "github.com"])
    return code == 0


def probe_auth() -> list[AuthReport]:
    return [
        AuthReport(name="claude", state="present" if _claude_auth() else "absent"),
        AuthReport(name="gh", state="present" if _gh_auth() else "absent"),
    ]


def probe_egress() -> EgressReport:
    return EgressReport(url=EGRESS_URL, reachable=_head(EGRESS_URL, EGRESS_TIMEOUT_S))


def managed_settings_path(platform_name: str) -> str | None:
    for prefix, path in MANAGED_SETTINGS_PATHS.items():
        if platform_name.startswith(prefix):
            return path
    return None


def probe_managed_settings() -> ManagedSettingsReport:
    path = managed_settings_path(_host_facts()["platform"])
    return ManagedSettingsReport(path=path, present=path is not None and _exists(path))


def build_report() -> DoctorReport:
    return DoctorReport(
        host=probe_host(),
        runtimes=probe_runtimes(),
        auth=probe_auth(),
        egress=probe_egress(),
        managed_settings=probe_managed_settings(),
    )


def render_table(report: DoctorReport) -> str:
    """Human-readable report: one section per probe, two aligned columns."""
    host = report.host
    sections: list[tuple[str, list[tuple[str, str]]]] = [
        (
            "host",
            [
                ("os", f"{host.os} {host.os_version}"),
                ("arch", host.arch),
                ("python", host.python),
                ("uv", host.uv or "not found"),
            ],
        ),
        (
            "runtimes",
            [(r.name, (r.version or "version unknown") if r.present else "not found") for r in report.runtimes],
        ),
        ("auth", [(a.name, a.state) for a in report.auth]),
        (
            "egress",
            [
                ("url", report.egress.url),
                ("result", "reachable" if report.egress.reachable else "unreachable"),
            ],
        ),
        (
            "managed settings",
            [
                ("state", "present" if report.managed_settings.present else "absent"),
                ("path", report.managed_settings.path or "no known path on this platform"),
            ],
        ),
    ]
    width = max(len(key) for _, rows in sections for key, _ in rows)
    lines: list[str] = []
    for title, rows in sections:
        if lines:
            lines.append("")
        lines.append(title)
        lines.extend(f"  {key.ljust(width)}  {value}" for key, value in rows)
    return "\n".join(lines)
