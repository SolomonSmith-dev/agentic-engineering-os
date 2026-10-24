"""Model registry and class resolution (build-order step 7)."""

import shutil
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from agent_team.canonical import validate_tree
from agent_team.routing import RegistryError, load_registry, resolve_model
from agent_team.schemas import AgentSpec, RegistrySpec

REPO = Path(__file__).resolve().parents[1]
REGISTRY = REPO / "routing" / "registry.yaml"


def registry() -> RegistrySpec:
    return load_registry(REGISTRY)


def raw() -> dict:
    return yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))


def test_shipped_registry_resolves_the_locked_classes() -> None:
    reg = registry()
    assert reg.classes["strong-coding"] == "claude-sonnet-5-5", "Sonnet implements"
    assert reg.classes["independent"] == "claude-opus-5-5", "Opus reviews"
    resolved = resolve_model(reg, "independent", runtime="claude")
    assert resolved.model == "claude-opus-5-5"
    assert resolved.fallbacks == ("claude-sonnet-5-5",), "one fallback step, Opus to Sonnet"
    assert resolve_model(reg, "strong-coding", runtime="claude").fallbacks == ()


def test_every_canonical_agent_class_is_in_the_registry() -> None:
    report = validate_tree(REPO / "canonical")
    assert report.ok, [p.render() for p in report.problems]
    agents = [d.spec for d in report.documents if d.kind == "agent"]
    assert agents and all(a.model_class in registry().classes for a in agents)


def test_unknown_class_and_wrong_runtime_are_refused() -> None:
    with pytest.raises(RegistryError, match="not in the model registry"):
        resolve_model(registry(), "nonexistent", runtime="claude")
    with pytest.raises(RegistryError, match="runs on claude, not codex"):
        resolve_model(registry(), "independent", runtime="codex")


def test_override_must_name_a_registry_model() -> None:
    resolved = resolve_model(registry(), "independent", runtime="claude", override="claude-haiku-5-5")
    assert resolved.model == "claude-haiku-5-5" and resolved.override
    with pytest.raises(RegistryError, match="has no entry"):
        resolve_model(registry(), "independent", runtime="claude", override="sonnet")


def test_policy_privacy_pin_refuses_the_model_and_drops_fallbacks() -> None:
    data = raw()
    data["models"]["claude-sonnet-5-5"]["privacy_class"] = "vendor-zdr"
    reg = RegistrySpec.model_validate(data)
    with pytest.raises(RegistryError, match="policy allows only vendor-zdr"):
        resolve_model(reg, "independent", runtime="claude", allowed_privacy=["vendor-zdr"])
    resolved = resolve_model(reg, "independent", runtime="claude", allowed_privacy=["vendor-cloud"])
    assert resolved.fallbacks == ()
    assert resolved.dropped == ("claude-sonnet-5-5 (vendor-zdr not allowed by policy)",)
    assert "dropped fallback" in resolved.note()


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["classes"].update({"independent": "claude-missing-1"}), "has no entry under models"),
        (lambda d: d["models"]["claude-opus-5-5"].update({"fallbacks": ["claude-opus-5-5"]}), "lists itself"),
        (lambda d: d["models"]["claude-opus-5-5"].update({"fallbacks": ["gone"]}), "falls back to 'gone'"),
        (lambda d: d["classes"].update({"opus-tier": "claude-opus-5-5"}), "names a vendor or model"),
        (lambda d: d["models"]["claude-haiku-5-5"].update({"privacy_class": "public"}), "privacy_class"),
    ],
)
def test_invalid_registries_are_rejected(mutate, message: str) -> None:
    data = raw()
    mutate(data)
    with pytest.raises(ValidationError, match=message):
        RegistrySpec.model_validate(data)


def test_fallback_must_share_the_runtime() -> None:
    data = raw()
    data["models"]["gpt-5.1-codex"] = {"provider": "openai", "runtime": "codex", "privacy_class": "vendor-cloud"}
    data["models"]["claude-opus-5-5"]["fallbacks"] = ["gpt-5.1-codex"]
    with pytest.raises(ValidationError, match="same runtime"):
        RegistrySpec.model_validate(data)


def test_validate_flags_an_agent_class_missing_from_the_registry(tmp_path: Path) -> None:
    shutil.copytree(REPO / "canonical", tmp_path / "canonical")
    (tmp_path / "routing").mkdir()
    data = raw()
    del data["classes"]["independent"]
    (tmp_path / "routing" / "registry.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    report = validate_tree(tmp_path / "canonical")
    assert [(p.file.name, p.field) for p in report.problems] == [("independent-reviewer.md", "model_class")]


def test_load_registry_reports_missing_and_invalid_files(tmp_path: Path) -> None:
    with pytest.raises(RegistryError, match="no model registry at"):
        load_registry(tmp_path / "registry.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("kind: registry\nname: default\nmodels: {}\nclasses: {}\n", encoding="utf-8")
    with pytest.raises(RegistryError, match="is invalid: models"):
        load_registry(bad)


