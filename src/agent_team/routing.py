"""Resolve an agent's model class to a model and its fallback chain (build-order step 7).

The registry lives at `routing/registry.yaml`, next to `canonical/`. Resolution
is a lookup: class -> model entry -> that entry's fallbacks, filtered to the
runtime that will run the agent and the privacy classes the policy allows. A
class listed under `runtime_classes` for the run's runtime resolves there first.
"""

from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from agent_team.schemas.registry import RegistrySpec

REGISTRY_PATH = Path("routing") / "registry.yaml"


class RegistryError(RuntimeError):
    """The registry is missing, invalid, or cannot resolve the request. The message says what to fix."""


@dataclass(frozen=True)
class ResolvedModel:
    model_class: str
    model: str
    fallbacks: tuple[str, ...]
    privacy_class: str
    override: bool = False
    dropped: tuple[str, ...] = ()  # fallbacks the policy or runtime ruled out, with the reason

    def note(self) -> str:
        source = "--model override" if self.override else f"class {self.model_class}"
        parts = [f"model {self.model} ({source}, {self.privacy_class})"]
        parts.append("fallback " + ", ".join(self.fallbacks) if self.fallbacks else "no fallback")
        if self.dropped:
            parts.append("dropped fallback " + "; ".join(self.dropped))
        return ", ".join(parts)


def default_registry_path(canonical_dir: Path) -> Path:
    return canonical_dir.parent / REGISTRY_PATH


def load_registry(path: Path) -> RegistrySpec:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RegistryError(f"no model registry at {path}; pass --registry") from exc
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise RegistryError(f"cannot read model registry {path}: {str(exc).splitlines()[0]}") from exc
    try:
        return RegistrySpec.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(p) for p in first["loc"]) or "<root>"
        raise RegistryError(f"model registry {path} is invalid: {field}: {first['msg']}") from exc


def resolve_model(
    registry: RegistrySpec,
    model_class: str,
    *,
    runtime: str,
    allowed_privacy: list[str] | None = None,
    override: str | None = None,
) -> ResolvedModel:
    """The model for `model_class` (or `override`) and the fallbacks `runtime` may switch to."""
    if override is not None:
        if override not in registry.models:
            raise RegistryError(f"--model {override!r} has no entry in the model registry; known: {', '.join(registry.models)}")
        model = override
    elif model_class in registry.runtime_classes.get(runtime, {}):
        model = registry.runtime_classes[runtime][model_class]
    elif model_class not in registry.classes:
        raise RegistryError(f"model class {model_class!r} is not in the model registry; known: {', '.join(registry.classes)}")
    else:
        model = registry.classes[model_class]

    entry = registry.models[model]
    if entry.runtime != runtime:
        raise RegistryError(f"model {model!r} runs on {entry.runtime}, not {runtime}")
    if allowed_privacy is not None and entry.privacy_class not in allowed_privacy:
        raise RegistryError(
            f"model {model!r} is {entry.privacy_class}; policy allows only {', '.join(allowed_privacy) or 'none'}"
        )

    fallbacks: list[str] = []
    dropped: list[str] = []
    for fallback in entry.fallbacks:
        candidate = registry.models[fallback]
        if allowed_privacy is not None and candidate.privacy_class not in allowed_privacy:
            dropped.append(f"{fallback} ({candidate.privacy_class} not allowed by policy)")
        elif fallback not in fallbacks:
            fallbacks.append(fallback)
    return ResolvedModel(
        model_class=model_class,
        model=model,
        fallbacks=tuple(fallbacks),
        privacy_class=entry.privacy_class,
        override=override is not None,
        dropped=tuple(dropped),
    )
