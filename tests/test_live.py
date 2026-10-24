"""Opt-in live check: the real `claude` runs the reviewer inside the real `srt` (build-order step 6).

Never runs in CI and never runs by default: it calls a model and spends money.
Run it on a machine with Claude Code, srt and a login after anything that
touches the adapter, the sandbox or the run path:

    AGENT_TEAM_LIVE=1 uv run pytest -q -m live

AGENT_TEAM_LIVE_MODEL picks the model (default claude-haiku-5-5); each run is
capped at $0.10. The four live runs that shipped 0.1.0 each found a bug the
fake-claude tests could not see; this test keeps that path covered.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_team.run_agent import run_agent
from agent_team.runs import RunStore
from agent_team.schemas.adapter import ReviewVerdict

CANONICAL = Path(__file__).resolve().parents[1] / "canonical"
MODEL = os.environ.get("AGENT_TEAM_LIVE_MODEL", "claude-haiku-5-5")
BUDGET_USD = 0.10

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("AGENT_TEAM_LIVE") != "1", reason="live: set AGENT_TEAM_LIVE=1 (calls a model)"),
]


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "target"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "live-test")
    git(root, "config", "user.email", "live-test@example.invalid")
    git(root, "config", "commit.gpgsign", "false")
    (root / ".gitignore").write_text(".agent-team/\n", encoding="utf-8")
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "add calc.add")
    return root.resolve()


def test_reviewer_runs_bash_inside_srt_and_returns_a_verdict(repo: Path) -> None:
    claude, srt = shutil.which("claude"), shutil.which("srt")
    if claude is None or srt is None:
        pytest.fail(f"live test needs claude and srt on PATH (claude={claude}, srt={srt})")

    lines: list[str] = []
    record, result = run_agent(
        repo,
        CANONICAL,
        "independent-reviewer",
        "Run `git show HEAD` first, then review that commit. Acceptance criterion AC-1: add(2, 3) returns 5.",
        model=MODEL,
        claude=claude,
        max_budget_usd=BUDGET_USD,
        timeout_s=300,
        echo=lines.append,
    )
    detail = "\n".join([*lines, result.result or "", result.stderr_tail or ""])

    assert result.status == "succeeded", detail
    assert record.outcome == "succeeded", detail
    verdict = ReviewVerdict.model_validate(result.structured_output)
    assert verdict.verdict in {"changes", "escalate"}, "add() subtracts; approve would be wrong"

    store = RunStore.open(repo, create=False)
    assert store is not None
    with store:
        calls = store.tool_calls(record.run_id)
        transitions = store.transitions(record.run_id)
    assert "srt sandbox" in (transitions[1].note or ""), "the run must be sandboxed"
    bash_ok = [c for c in calls if c.tool_name == "Bash" and c.result_status == "ok"]
    assert bash_ok, f"no Bash call succeeded inside srt: {[(c.tool_name, c.result_status) for c in calls]}"
    assert 0 < record.cost_usd <= BUDGET_USD * 1.5, "cost is recorded and the budget cap held"
