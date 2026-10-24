"""Build-order step 7: model registry schema, class resolution, and validate's cross-checks."""

import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_team.canonical import parse_file, validate_tree
from agent_team.routing import RoutingError, resolve_model
from agent_team.schemas import RegistrySpec

REPO = Path(__file__).resolve().parents[1]
CANONICAL = REPO / "canonical"
REGISTRY = CANONICAL / "routing" / "registry.yaml"


def registry() -> RegistrySpec:
    data, _ = parse_file(REGISTRY, "registry")
    return RegistrySpec.model_validate(data)


def raw() -> dict:
    data, _ = parse_file(REGISTRY, "registry")
    return data


# --- resolution -------------------------------------------------------------------


def test_locked_classes_resolve_to_the_pinned_models() -> None:
    implement = resolve_model("strong-coding", registry())
    assert (implement.model, implement.fallbacks, implement.source) == ("claude-sonnet-5-5", (), "registry")
    review = resolve_model("independent", registry())
    assert (review.model, review.fallbacks) == ("claude-opus-5-5", ("claude-sonnet-5-5",))
    assert review.note() == "model claude-opus-5-5 (class independent); fallback claude-sonnet-5-5"


def test_unknown_class_is_an_error_that_names_the_known_ones() -> None:
    with pytest.raises(RoutingError, match=r"'fast-cheap' is not in the registry \(known: independent, strong-coding\)"):
        resolve_model("fast-cheap", registry())


def test_override_keeps_the_chain_minus_itself() -> None:
    other = resolve_model("independent", registry(), override="claude-haiku-5-5")
    assert (other.model, other.fallbacks, other.source) == ("claude-haiku-5-5", ("claude-sonnet-5-5",), "override")
    assert other.note() == "model claude-haiku-5-5 (--model override, not in registry); fallback claude-sonnet-5-5"
    same = resolve_model("independent", registry(), override="claude-sonnet-5-5")
    assert same.fallbacks == (), "a model never falls back to itself"
    assert same.note() == "model claude-sonnet-5-5 (--model override); fallback none"


def test_override_does_not_excuse_an_unknown_class() -> None:
    with pytest.raises(RoutingError):
        resolve_model("fast-cheap", registry(), override="claude-opus-5-5")


# --- schema -----------------------------------------------------------------------


def test_cost_is_optional_for_a_local_model() -> None:
    data = raw()
    data["models"]["qwen3:8b"] = {"provider": "ollama", "privacy_class": "local-only"}
    spec = RegistrySpec.model_validate(data)
    assert spec.models["qwen3:8b"].cost is None


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["classes"].update({"strong-coding": "claude-sonnet-9"}), "has no entry under models"),
        (lambda d: d["models"]["claude-opus-5-5"].update(fallbacks=["claude-opus-5-5"]), "lists itself"),
        (lambda d: d["models"]["claude-opus-5-5"].update(fallbacks=["claude-haiku-5-5"]), "has no entry under models"),
        (lambda d: d["models"]["claude-opus-5-5"].update(fallbacks=["a", "b", "c"]), "at most 2"),
        (lambda d: d["classes"].update({"claude-review": "claude-opus-5-5"}), "names a vendor"),
        (lambda d: d["models"]["claude-opus-5-5"].update(privacy_class="public"), "vendor-cloud"),
        (lambda d: d["models"]["claude-opus-5-5"]["cost"].update(input_usd_per_mtok=-1), "greater than or equal"),
        (lambda d: d.update(classes={}), "at least 1"),
    ],
)
def test_bad_registry_is_rejected(mutate, message: str) -> None:
    data = raw()
    mutate(data)
    with pytest.raises(ValidationError, match=message):
        RegistrySpec.model_validate(data)


# --- validate ---------------------------------------------------------------------


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    target = tmp_path / "canonical"
    shutil.copytree(CANONICAL, target)
    return target


def test_agent_class_missing_from_the_registry_fails_validate(tree: Path) -> None:
    agent = tree / "agents" / "independent-reviewer.md"
    agent.write_text(agent.read_text().replace("model_class: independent", "model_class: fast-cheap", 1))
    report = validate_tree(tree)
    assert [(p.file.name, p.field) for p in report.problems] == [("independent-reviewer.md", "model_class")]


def test_registry_problem_names_the_file(tree: Path) -> None:
    path = tree / "routing" / "registry.yaml"
    path.write_text(path.read_text().replace("privacy_class: vendor-cloud", "privacy_class: public", 1))
    report = validate_tree(tree)
    assert report.problems and report.problems[0].file == path
    assert report.problems[0].field.startswith("models.")
