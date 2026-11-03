"""The PR step's restart ledger and `find_existing`: only one OPEN, same-repository PR on the
run branch at the reviewed commit counts as recovery; anything else proceeds or escalates.

`gh pr list` answers from a JSON file the test writes; every other gh call goes to the
fake gh from test_implementer.py. No test opens a real PR.
"""

import json
import stat
import sys
from pathlib import Path

import pytest
from test_implementer import World, git, repo, srt, world  # noqa: F401  (fixtures are used by name)
from test_orchestrator_edges import RUN, at_pr, executor, pr_reason

from agent_team.orchestrator import load_dag
from agent_team.orchestrator import pr as pr_mod

BRANCH = f"agent-team/{RUN}"
OTHER_URL = "https://github.com/example/target/pull/3"

LIST_GH = r'''#!{python}
import json, os, sys
args = sys.argv[1:]
if args[:2] == ["pr", "list"]:
    with open({log!r}, "a") as f:
        f.write(json.dumps(args) + "\n")
    answer = json.load(open({answer!r}))
    if answer.get("fail"):
        print("HTTP 502", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(answer["prs"]))
    sys.exit(0)
os.execv({fallback!r}, [{fallback!r}, *args])
'''


class ListGh:
    def __init__(self, tmp_path: Path, fallback: Path) -> None:
        self.answer = tmp_path / "pr-list.json"
        self.log = tmp_path / "pr-list.log"
        self.path = tmp_path / "list-bin" / "gh"
        self.path.parent.mkdir()
        self.path.write_text(LIST_GH.format(python=sys.executable, log=str(self.log), answer=str(self.answer),
                                            fallback=str(fallback)), encoding="utf-8")  # fmt: skip
        self.path.chmod(self.path.stat().st_mode | stat.S_IEXEC)
        self.prs([])

    def prs(self, prs: list[dict]) -> None:
        self.answer.write_text(json.dumps({"prs": prs}), encoding="utf-8")

    def fail(self) -> None:
        self.answer.write_text(json.dumps({"fail": True}), encoding="utf-8")

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []


def pr(head: str | None, *, state: str = "OPEN", cross: bool | None = False, url: str = OTHER_URL) -> dict:
    found: dict = {"url": url, "number": 3, "isDraft": True, "state": state,
                   "headRepositoryOwner": {"login": "example"}}  # fmt: skip
    if head is not None:
        found["headRefOid"] = head
    if cross is not None:
        found["isCrossRepository"] = cross
    return found


@pytest.fixture
def gh(tmp_path: Path, world: World) -> ListGh:
    return ListGh(tmp_path, world.bin / "gh")


# --- find_existing ---------------------------------------------------------------------------


def test_find_existing_asks_for_open_prs_with_their_head_repository(tmp_path: Path, gh: ListGh) -> None:
    head = "a" * 40
    gh.prs([pr(head)])
    assert pr_mod.find_existing(str(gh.path), tmp_path, BRANCH)["url"] == OTHER_URL
    [args] = gh.calls()
    assert args[args.index("--state") + 1] == "open"
    fields = args[args.index("--json") + 1].split(",")
    assert {"state", "headRefOid", "headRepositoryOwner", "isCrossRepository"} <= set(fields)


@pytest.mark.parametrize("state", ["CLOSED", "MERGED"])
def test_find_existing_ignores_closed_and_merged_prs(tmp_path: Path, gh: ListGh, state: str) -> None:
    gh.prs([pr("a" * 40, state=state)])
    assert pr_mod.find_existing(str(gh.path), tmp_path, BRANCH) is None


@pytest.mark.parametrize("cross", [True, None])
def test_find_existing_ignores_a_fork_head_or_one_it_cannot_place(tmp_path: Path, gh: ListGh, cross: bool | None) -> None:
    gh.prs([pr("a" * 40, cross=cross)])
    assert pr_mod.find_existing(str(gh.path), tmp_path, BRANCH) is None


def test_find_existing_fails_closed_on_more_than_one_candidate(tmp_path: Path, gh: ListGh) -> None:
    gh.prs([pr("a" * 40), pr("a" * 40, url="https://github.com/example/target/pull/4")])
    with pytest.raises(pr_mod.PrError, match="more than one open PR"):
        pr_mod.find_existing(str(gh.path), tmp_path, BRANCH)


def test_find_existing_raises_when_gh_cannot_answer(tmp_path: Path, gh: ListGh) -> None:
    gh.fail()
    with pytest.raises(pr_mod.PrError):
        pr_mod.find_existing(str(gh.path), tmp_path, BRANCH)


# --- the ledger states ------------------------------------------------------------------------


def ledger_path(repo: Path) -> Path:
    return repo / ".agent-team" / "artifacts" / RUN / "pr-ledger.json"


def write_ledger(repo: Path, entry: dict | str) -> None:
    path = ledger_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(entry if isinstance(entry, str) else json.dumps(entry), encoding="utf-8")


def run_pr(repo: Path, gh: ListGh):  # noqa: ANN201
    ex = executor(repo, gh=str(gh.path))
    return ex(load_dag(ex.canonical, "bug-fix", "default").node("PR"), at_pr(git(repo, "rev-parse", "HEAD")))


def test_an_opened_ledger_at_this_head_is_recovered_without_gh(repo: Path, world: World, gh: ListGh) -> None:
    head = git(repo, "rev-parse", "HEAD")
    write_ledger(repo, {"status": "opened", "url": OTHER_URL, "branch": BRANCH, "head": head, "draft": True})
    result = run_pr(repo, gh)
    assert result.evidence["pr"]["recovered"] == "pr ledger" and result.evidence["pr"]["url"] == OTHER_URL
    assert world.gh_calls() == [] and gh.calls() == []


def test_an_opened_ledger_at_another_head_escalates(repo: Path, world: World, gh: ListGh) -> None:
    write_ledger(repo, {"status": "opened", "url": OTHER_URL, "branch": BRANCH, "head": "b" * 40})
    assert "a human must decide" in pr_reason(run_pr(repo, gh))
    assert world.pr_creates() == []


@pytest.mark.parametrize("ledger", [{"status": "attempting", "branch": BRANCH, "pushed": True}, "{torn"])
def test_an_unfinished_attempt_recovers_one_open_pr_at_this_head(repo: Path, world: World, gh: ListGh, srt: str,
                                                                 ledger: dict | str) -> None:  # fmt: skip
    head = git(repo, "rev-parse", "HEAD")
    write_ledger(repo, ledger)
    gh.prs([pr(head)])
    result = run_pr(repo, gh)
    assert result.evidence["pr"]["recovered"] == "gh pr list" and result.evidence["pr"]["url"] == OTHER_URL
    assert world.pr_creates() == []
    assert json.loads(ledger_path(repo).read_text())["status"] == "opened"


@pytest.mark.parametrize("state", ["CLOSED", "MERGED"])
def test_a_closed_or_merged_pr_is_not_recovered_as_opened(repo: Path, world: World, gh: ListGh, srt: str, state: str) -> None:
    head = git(repo, "rev-parse", "HEAD")
    write_ledger(repo, {"status": "attempting", "branch": BRANCH, "head": head, "pushed": True})
    gh.prs([pr(head, state=state)])
    result = run_pr(repo, gh)
    assert "recovered" not in result.evidence["pr"]
    assert result.evidence["pr"]["status"] == "opened" and len(world.pr_creates()) == 1  # a fresh draft


@pytest.mark.parametrize("found_head", ["b" * 40, None])
def test_an_open_pr_at_another_or_unknown_head_escalates(repo: Path, world: World, gh: ListGh, srt: str,
                                                         found_head: str | None) -> None:  # fmt: skip
    write_ledger(repo, {"status": "attempting", "branch": BRANCH, "pushed": True})
    gh.prs([pr(found_head)])
    assert "a human must decide" in pr_reason(run_pr(repo, gh))
    assert world.pr_creates() == []


def test_a_fork_pr_on_the_run_branch_is_not_recovered(repo: Path, world: World, gh: ListGh, srt: str) -> None:
    head = git(repo, "rev-parse", "HEAD")
    write_ledger(repo, {"status": "attempting", "branch": BRANCH, "pushed": True})
    gh.prs([pr(head, cross=True, url="https://github.com/fork/target/pull/9")])
    result = run_pr(repo, gh)
    assert "recovered" not in result.evidence["pr"] and result.evidence["pr"].get("url") != "https://github.com/fork/target/pull/9"


def test_two_candidates_escalate_instead_of_taking_the_first(repo: Path, world: World, gh: ListGh, srt: str) -> None:
    head = git(repo, "rev-parse", "HEAD")
    write_ledger(repo, {"status": "attempting", "branch": BRANCH, "pushed": True})
    gh.prs([pr(head), pr(head, url="https://github.com/example/target/pull/4")])
    reason = pr_reason(run_pr(repo, gh))
    assert "gh could not say" in reason and "more than one open PR" in reason
    assert world.pr_creates() == []


def test_an_unreadable_ledger_and_a_silent_gh_open_nothing(repo: Path, world: World, gh: ListGh, srt: str) -> None:
    write_ledger(repo, "{torn")
    gh.fail()
    assert "gh could not say" in pr_reason(run_pr(repo, gh))
    assert world.pr_creates() == []
