"""The `pr` step's draft pull request: its body, the push, and `gh` (build-order step 11).

The orchestrator holds `github.pr.create`; no agent ever does. It runs `git
push` and `gh` itself, outside every sandbox, with its own environment, so the
GitHub credential (`GH_TOKEN`, or gh's own login) never reaches a worker,
whose environment is scrubbed and whose sandbox cannot read `~/.config/gh`.

    push   <reviewed commit>:refs/heads/agent-team/<run-id> to origin; never --force, never another branch
    open   gh pr create --draft --head agent-team/<run-id> --title ... --body-file <artifact>

The body carries the evidence summary, the verdict, the unresolved risks and
the minor findings as follow-ups. An escalated run's draft is titled
`[blocked]` and lists every finding.
"""

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_team.orchestrator.workspace import SAFE_GIT, short
from agent_team.runs.redact import redact_text

TITLE_CHARS = 72
BLOCKED_PREFIX = "[blocked] "
REMOTE = "origin"
GH_TIMEOUT_S = 120


class PrError(RuntimeError):
    """The push or `gh` failed; the message is safe to record (redacted, cut short)."""


@dataclass(frozen=True)
class PrInputs:
    """Everything the body is written from: evidence only, never a transcript."""

    run_id: str
    task: str
    plan: Mapping[str, Any]
    reproduction_command: str | None
    reproduction_check: Mapping[str, Any]
    diff: Mapping[str, Any]
    claimed_changes: Mapping[str, Any]  # the last attempt's report: changes, and the files it did not name
    test_output: Mapping[str, Any]
    verdict: str
    findings: Sequence[Mapping[str, Any]]
    blocked: bool
    blocked_reasons: Sequence[str]


def title(task: str, blocked: bool) -> str:
    first = next((line.strip() for line in task.splitlines() if line.strip()), "bug fix")
    text = f"agent-team: {first}"
    if len(text) > TITLE_CHARS:
        text = text[: TITLE_CHARS - 3].rstrip() + "..."
    return BLOCKED_PREFIX + text if blocked else text


def _finding(f: Mapping[str, Any]) -> str:
    where = f"{f.get('file')}:{f['line']}" if f.get("line") else str(f.get("file"))
    return f"`{where}` ({f.get('requirement_id')}, {f.get('severity')}): {f.get('fix')}"


def risks(given: PrInputs) -> list[str]:
    out = list(given.blocked_reasons)
    if not given.reproduction_check.get("verified"):
        reason = given.reproduction_check.get("reason", "it was not run by the orchestrator")
        out.append(f"The reproduction was not verified before the fix: {reason}.")
    for skipped in given.test_output.get("skipped", []):
        out.append(f"Gate skipped: {skipped}.")
    unclaimed = given.claimed_changes.get("unclaimed") or []
    if unclaimed:
        out.append(
            "Changed in the last attempt but not named in the implementer's report: "
            + ", ".join(f"`{f}`" for f in unclaimed) + "."
        )  # fmt: skip
    return out


def body(given: PrInputs, redaction: Sequence[str] = ()) -> str:
    """The PR body, redacted line by line (the task and the findings are free text)."""
    diff, tests, check = given.diff, given.test_output, given.reproduction_check
    lines = ["## Summary", "", given.task.strip(), ""]
    if given.blocked:
        lines += ["> **Blocked: this draft needs a human decision before anyone builds on it.** "
                  "The reasons are under Unresolved risks.", ""]  # fmt: skip

    lines += ["## Evidence", ""]
    if check.get("verified"):
        lines.append(
            f"- Reproduction `{given.reproduction_command}` failed before the fix "
            f"(exit {check.get('exit_code')} at `{short(check.get('head'))}`, run by the orchestrator in the sandbox)."
        )
    else:
        lines.append(f"- Reproduction `{given.reproduction_command}`: not verified before the fix.")
    files = diff.get("files", [])
    lines.append(
        f"- Diff `{short(diff.get('base'))}..{short(diff.get('head'))}`: {len(files)} file(s), "
        f"+{diff.get('insertions', 0)} -{diff.get('deletions', 0)}."
    )
    lines += [f"  - `{f}`" for f in files]
    where = "in the srt sandbox" if tests.get("sandboxed") else "not run (no sandbox)"
    lines.append(f"- Checks run by the orchestrator at `{short(tests.get('head'))}`, {where}:")
    for r in tests.get("results", []):
        code = r.get("exit_code")
        status = "passed" if code == 0 else ("timed out" if code is None else f"failed (exit {code})")
        lines.append(f"  - {r.get('kind')} `{r.get('command')}`: {status}")
    for criterion in given.plan.get("acceptance_criteria", []):
        lines.append(f"- Acceptance criterion {criterion.get('id')}: {criterion.get('text')}")
    lines.append("")

    lines += ["## Review", ""]
    lines.append(
        f"Verdict: **{given.verdict}**, from the independent reviewer in a fresh process that saw the plan, "
        "the diff and the orchestrator's check output, never the implementer's transcript."
    )
    blocking = [f for f in given.findings if f.get("severity") in ("blocker", "major")]
    if blocking:
        lines += ["", "Blocking findings:", *(f"- {_finding(f)}" for f in blocking)]
    lines.append("")

    lines += ["## Unresolved risks", ""]
    lines += [f"- {r}" for r in risks(given)] or ["- None recorded."]
    lines.append("")

    minor = [f for f in given.findings if f.get("severity") == "minor"]
    lines += ["## Follow-ups", ""]
    lines += [f"- [ ] {_finding(f)}" for f in minor] or ["- None."]
    lines += ["", "---", "", f"Draft opened by agent-team run `{given.run_id}`. Trace: `agent-team trace {given.run_id}`."]
    extra = tuple(redaction)
    return "\n".join(redact_text(line, extra) for line in lines) + "\n"


def _fail(what: str, proc: subprocess.CompletedProcess[str], redaction: Sequence[str]) -> PrError:
    detail = redact_text((proc.stderr or proc.stdout or "").strip()[-400:], tuple(redaction))
    return PrError(f"{what} exited {proc.returncode}: {detail}" if detail else f"{what} exited {proc.returncode}")


def gh_env() -> dict[str, str]:
    """The orchestrator's own environment (the credential lives here), with gh's prompts off."""
    return {**os.environ, "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"}


def gh_authenticated(gh: str, root: Path) -> bool:
    proc = subprocess.run([gh, "auth", "status"], cwd=root, env=gh_env(), capture_output=True, text=True,
                          timeout=GH_TIMEOUT_S, check=False)  # fmt: skip
    return proc.returncode == 0  # its output names the account; it is never read or stored


def has_remote(root: Path) -> bool:
    return subprocess.run(["git", "-C", str(root), "remote", "get-url", REMOTE], capture_output=True, check=False).returncode == 0


def push(root: Path, head: str, branch: str, redaction: Sequence[str] = ()) -> None:
    """Push exactly `head` to the run branch on origin. No force: a branch that moved elsewhere refuses."""
    if not branch.startswith("agent-team/") or head.startswith("+"):
        raise PrError(f"refusing to push {head} to {branch}: only the run's agent-team/ branch is pushed")
    proc = subprocess.run(
        ["git", "-C", str(root), *SAFE_GIT, "push", "--no-verify", "--porcelain", REMOTE, f"{head}:refs/heads/{branch}"],
        capture_output=True, text=True, timeout=GH_TIMEOUT_S, check=False,
    )  # fmt: skip
    if proc.returncode != 0:
        raise _fail(f"git push {REMOTE} {branch}", proc, redaction)


def create(gh_argv: Sequence[str], root: Path, branch: str, pr_title: str, body_file: Path, redaction: Sequence[str] = ()) -> str:
    """`gh pr create --draft` for the run branch; returns the PR URL gh prints."""
    proc = subprocess.run(
        [*gh_argv, "--head", branch, "--title", pr_title, "--body-file", str(body_file)],
        cwd=root, env=gh_env(), capture_output=True, text=True, timeout=GH_TIMEOUT_S, check=False,
    )  # fmt: skip
    if proc.returncode != 0:
        raise _fail("gh pr create", proc, redaction)
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return lines[-1] if lines else "(gh printed no URL)"
