"""Metered runtime regressions using local fake Codex output and a fake clock."""
import importlib
import json
import sys

import pytest
import yaml
from test_orchestrator import CANONICAL, repo  # noqa: F401

from agent_team.adapters.codex.invoke import Invocation
from agent_team.budget import BudgetMeter, Limits
from agent_team.orchestrator import default_executor_factory, run_workflow
from agent_team.schemas.registry import ModelCost

run_mod = importlib.import_module('agent_team.run_agent')
MODEL = 'gpt-6.1-sol'
PRICE = ModelCost(input_per_mtok_usd=10, output_per_mtok_usd=20,
                  cache_read_per_mtok_usd=1, cache_write_per_mtok_usd=10)


@pytest.mark.parametrize('limit', [None, 'under', 'usd', 'tokens', 'minutes'])
def test_codex_cached_usage_uses_meter_price_without_changing_unmetered_calls(repo, tmp_path, monkeypatch, limit):
    now = [0.0]
    meter = None if limit is None else BudgetMeter(
        Limits(tokens=100 if limit == 'tokens' else 2_000_000, minutes=1,
               usd=0.5 if limit == 'usd' else 5), {MODEL: PRICE}, clock=lambda: now[0])
    registry = yaml.safe_load((CANONICAL.parent / 'routing' / 'registry.yaml').read_text())
    registry['models'][MODEL]['cost'] = PRICE.model_dump()
    registry_path = tmp_path / 'registry.yaml'
    registry_path.write_text(yaml.safe_dump(registry))
    events = [
        {'type': 'thread.started', 'thread_id': 'fake'},
        {'type': 'item.completed', 'item': {'id': 'answer', 'type': 'agent_message',
                                           'text': json.dumps({'verdict': 'approve', 'findings': []})}},
        {'type': 'turn.completed', 'usage': {'input_tokens': 1_000_100 if limit == 'tokens' else 1_000_000,
                                            'cached_input_tokens': 1_000_000, 'output_tokens': 0}},
    ]
    monkeypatch.setattr(run_mod.codex_adapter, 'check_flags', lambda *a: None)
    monkeypatch.setattr(run_mod, 'srt_path', lambda: '/fake/srt')
    monkeypatch.setattr(run_mod, 'codex_auth_mode', lambda _: 'api_key')

    def invocation(*args, **kwargs):
        if limit == 'minutes':
            now[0] = 60
        # Real parser/normalize/execute, but only a Python process printing fixture events.
        script = 'print(' + repr('\n'.join(json.dumps(e) for e in events)) + ')'
        return Invocation(argv=[sys.executable, '-c', script], env={}, mode='api_key')

    monkeypatch.setattr(run_mod.codex_adapter, 'build_invocation', invocation)
    _, result = run_mod.run_agent(repo, CANONICAL, 'independent-reviewer', 'Review fixture',
                                  runtime='codex', codex='fake-codex', registry_path=registry_path,
                                  max_budget_usd=5, timeout_s=10, meter=meter, echo=lambda _: None)
    if limit is None:
        assert result.status == 'budget_exceeded'
        assert result.cost_usd == pytest.approx(10)
    elif limit == 'under':
        assert result.status == 'succeeded'
        assert result.structured_output == {'verdict': 'approve', 'findings': []}
        assert result.cost_usd == pytest.approx(1)
        assert meter.check() is None
    else:
        assert result.status == 'budget_exceeded'
        assert meter.check().limit == limit


def test_setup_expiry_reaches_workflow_as_halted_budget(repo, monkeypatch):
    now = [0.0]
    meter = BudgetMeter(Limits(tokens=1000, minutes=1, usd=5), {}, clock=lambda: now[0])
    checks = []

    def setup(_):
        assert meter.check() is None  # The executor's initial check has passed.
        checks.append('setup')
        now[0] = 60

    monkeypatch.setattr(run_mod, 'check_version', setup)
    monkeypatch.setattr(run_mod, 'build_invocation', lambda *a, **kw: pytest.fail('worker must not start'))
    factory = default_executor_factory(repo=repo, canonical_dir=CANONICAL, claude='fake-claude',
                                       max_budget_usd=5, timeout_s=10, meter=meter, echo=lambda _: None)
    record = run_workflow(repo, CANONICAL, 'bug-fix', 'Fix fixture', executor_factory=factory,
                          meter=meter, echo=lambda _: None)
    assert checks == ['setup']
    assert meter.halted.limit == 'minutes'
    assert record.outcome == 'halted_budget'


@pytest.mark.parametrize('metered', [False, True])
def test_setup_errors_without_budget_expiry_still_block(repo, monkeypatch, metered):
    meter = BudgetMeter(Limits(tokens=1000, minutes=1, usd=5), {}, clock=lambda: 0) if metered else None

    def setup(_):
        raise run_mod.RunAgentError('fixture setup failure')

    monkeypatch.setattr(run_mod, 'check_version', setup)
    factory = default_executor_factory(repo=repo, canonical_dir=CANONICAL, claude='fake-claude',
                                       max_budget_usd=5, timeout_s=10, meter=meter, echo=lambda _: None)
    record = run_workflow(repo, CANONICAL, 'bug-fix', 'Fix fixture', executor_factory=factory,
                          meter=meter, echo=lambda _: None)
    assert record.outcome == 'blocked'
    assert meter is None or meter.halted is None
