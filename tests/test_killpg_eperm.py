"""Held-back verifier for the killpg-EPERM card. Deterministic: os.killpg is patched to raise EPERM."""
import errno
import importlib
import os

import pytest


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_sweep_tolerates_eperm_and_still_halts(tmp_path, monkeypatch, runtime):
    invoke = importlib.import_module(f"agent_team.adapters.{runtime}.invoke")
    real = os.killpg
    calls = []

    def flaky_killpg(pgid, sig):
        calls.append(pgid)
        if len(calls) >= 2:  # the sweep after proc.wait(): macOS answers EPERM for an all-zombie group
            raise PermissionError(errno.EPERM, "Operation not permitted")
        return real(pgid, sig)

    monkeypatch.setattr(invoke.os, "killpg", flaky_killpg)
    script = "echo '{\"type\": \"assistant\", \"message\": {\"id\": \"m1\", \"model\": \"x\", \"usage\": {\"input_tokens\": 1}, \"content\": []}}'; echo '{\"type\": \"turn.completed\", \"usage\": {\"input_tokens\": 1}}'; sleep 30"
    mode = "login" if runtime == "claude" else "api_key"
    inv = invoke.Invocation(argv=["sh", "-c", script], env=dict(os.environ), mode=mode)
    result, _ = invoke.execute(inv, prompt="", cwd=tmp_path, stderr_path=tmp_path / "err", timeout_s=20,
                               watch=lambda e: "budget halt: usd 6 > 5")
    assert result.status == "budget_exceeded" and result.reason == "budget halt: usd 6 > 5"
    assert len(calls) >= 2, "the post-wait sweep ran"


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_watch_exception_still_propagates_when_sweep_gets_eperm(tmp_path, monkeypatch, runtime):
    invoke = importlib.import_module(f"agent_team.adapters.{runtime}.invoke")
    real = os.killpg
    calls = []

    def flaky_killpg(pgid, sig):
        calls.append(pgid)
        if len(calls) >= 2:
            raise PermissionError(errno.EPERM, "Operation not permitted")
        return real(pgid, sig)

    monkeypatch.setattr(invoke.os, "killpg", flaky_killpg)
    inv = invoke.Invocation(argv=["sh", "-c", "echo '{\"type\": \"x\"}'; sleep 30"], env=dict(os.environ),
                            mode="login" if runtime == "claude" else "api_key")

    def watch(event):
        raise ValueError("price missing")

    with pytest.raises(ValueError, match="price missing"):  # the watch's error, not the sweep's EPERM
        invoke.execute(inv, prompt="", cwd=tmp_path, stderr_path=tmp_path / "err", timeout_s=20, watch=watch)


def test_a_real_permission_error_on_the_first_kill_is_not_swallowed_silently(tmp_path, monkeypatch):
    """EPERM on the FIRST kill means we could not stop a live worker: that must surface, not pass."""
    invoke = importlib.import_module("agent_team.adapters.claude.invoke")

    def deny(pgid, sig):
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(invoke.os, "killpg", deny)
    inv = invoke.Invocation(argv=["sh", "-c", "echo '{\"type\": \"x\"}'; exit 0"], env=dict(os.environ), mode="login")
    with pytest.raises(PermissionError):
        invoke.execute(inv, prompt="", cwd=tmp_path, stderr_path=tmp_path / "err", timeout_s=20,
                       watch=lambda e: "budget halt: usd 6 > 5")
