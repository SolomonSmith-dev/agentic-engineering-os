"""PR wall-clock boundaries, with no remote subprocesses or model calls."""
import json
import subprocess

import pytest
from test_implementer import git, repo, srt, world  # noqa: F401
from test_orchestrator_edges import RUN, at_pr, executor

from agent_team.budget import BudgetMeter, Limits
from agent_team.orchestrator import pr as pr_mod


@pytest.mark.parametrize('stop', ['entry', 'auth', 'remote', 'list', 'recovered', 'push', 'create', None])
@pytest.mark.parametrize('times_out', [False, True])
def test_pr_budget_boundaries(repo, srt, monkeypatch, stop, times_out):
    now = [0.0]
    meter = BudgetMeter(Limits(tokens=1000, minutes=1, usd=1), {}, clock=lambda: now[0])
    now[0] = 59.25
    ex = executor(repo, gh='fake-gh', meter=meter)
    given = at_pr(git(repo, 'rev-parse', 'HEAD'))
    ledger_path = ex._pr_ledger_path(RUN)
    ex._write_pr_ledger(RUN, {'status': 'attempting', 'pushed': False})
    calls = []
    expected = ['auth', 'remote', 'list', 'push', 'create']

    def run(argv, **kwargs):
        if argv[:2] == ['fake-gh', 'auth']:
            step = 'auth'
        elif argv[:3] == ['fake-gh', 'pr', 'list']:
            step = 'list'
        elif argv[:3] == ['fake-gh', 'pr', 'create']:
            step = 'create'
        elif 'push' in argv:
            step = 'push'
        elif 'get-url' in argv:
            step = 'remote'
        else:
            return original_run(argv, **kwargs)  # local worktree setup only
        calls.append(step)
        assert kwargs.get('timeout') == pytest.approx(60 - now[0])
        now[0] += 0.1
        if step == stop or (step == 'list' and stop == 'recovered'):
            now[0] = 60
            if times_out:
                raise subprocess.TimeoutExpired(argv, kwargs['timeout'])
        found = [{'state': 'OPEN', 'isCrossRepository': False, 'headRefOid': given.inputs['diff']['head'],
                  'url': 'https://github.com/example/r/pull/1'}] if stop == 'recovered' else []
        return subprocess.CompletedProcess(argv, 0, json.dumps(found) if step == 'list' else 'https://github.com/example/r/pull/1\n', '')

    original_run = subprocess.run
    monkeypatch.setattr(pr_mod.subprocess, 'run', run)
    if stop == 'entry':
        now[0] = 60
    result = ex._pr(given)
    if stop is None:
        assert result.evidence['pr']['status'] == 'opened'
        assert calls == expected
    else:
        assert result.halted == 'minutes'
        assert calls == ([] if stop == 'entry' else expected[:expected.index('list' if stop == 'recovered' else stop) + 1])
        ledger = json.loads(ledger_path.read_text())
        if stop == 'push' and not times_out:
            assert ledger['pushed'] is True
        if stop in ('create', 'recovered') and not times_out:
            assert ledger['status'] == 'opened'
            # A restart reuses the successful effect even though the prior node halted.
            ex.meter = None
            assert ex._pr(given).evidence['pr']['recovered'] == 'pr ledger'


@pytest.mark.parametrize('helper', ['gh_authenticated', 'has_remote', 'find_existing', 'push', 'create'])
def test_pr_helpers_keep_default_timeout(tmp_path, monkeypatch, helper):
    calls = []
    def run(argv, **kwargs):
        calls.append(kwargs['timeout'])
        return subprocess.CompletedProcess(argv, 0, '[]', '')
    monkeypatch.setattr(pr_mod.subprocess, 'run', run)
    args = {
        'gh_authenticated': ('gh', tmp_path), 'has_remote': (tmp_path,),
        'find_existing': ('gh', tmp_path, 'agent-team/r'),
        'push': (tmp_path, 'a' * 40, 'agent-team/r'),
        'create': (['gh', 'pr', 'create'], tmp_path, 'agent-team/r', 'title', tmp_path / 'body'),
    }
    getattr(pr_mod, helper)(*args[helper])
    assert calls == [120]
