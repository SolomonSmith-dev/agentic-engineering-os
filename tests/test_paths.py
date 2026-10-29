from pathlib import Path

import pytest

from agent_team import paths


def test_cwd_canonical_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "canonical").mkdir()
    monkeypatch.chdir(tmp_path)
    assert paths.default_canonical_dir() == tmp_path / "canonical"


def test_outside_a_checkout_the_install_copy_is_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A target repository has no canonical/; the tool must still find agents, workflows and the registry."""
    monkeypatch.chdir(tmp_path)
    found = paths.default_canonical_dir()
    assert found.is_dir() and (found / "agents").is_dir(), found
    assert paths.default_registry_for(found).is_file()


def test_bundled_copy_is_absent_or_complete() -> None:
    root = paths.bundled_root()
    if root is None:
        pytest.skip("source checkout: nothing bundled")
    assert (root / "canonical" / "workflows").is_dir() and (root / "routing" / "registry.yaml").is_file()
