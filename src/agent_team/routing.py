"""Model class resolution against canonical/routing/registry.yaml (build-order step 7).

An agent names a model class; the registry maps the class to a model id and
that model's fallback chain. `--model` overrides the model but keeps the
chain, minus the override itself. Routing is a lookup, not a learned policy.
"""

from dataclasses import dataclass
from typing import Literal

from agent_team.schemas.registry import RegistrySpec


class RoutingError(ValueError):
    """The model class has no entry in the registry."""


@dataclass(frozen=True)
class ResolvedModel:
    model_class: str
    model: str
    fallbacks: tuple[str, ...]
    source: Literal["registry", "override"]
    in_registry: bool = True

    def note(self) -> str:
        """One line for the run record: which model runs, why, and what it falls back to."""
        origin = f"class {self.model_class}" if self.source == "registry" else "--model override"
        if not self.in_registry:
            origin += ", not in registry"
        chain = ", ".join(self.fallbacks) if self.fallbacks else "none"
        return f"model {self.model} ({origin}); fallback {chain}"


def resolve_model(model_class: str, registry: RegistrySpec, override: str | None = None) -> ResolvedModel:
    """Resolve `model_class` to a model and its fallbacks; `override` replaces the model only."""
    if model_class not in registry.classes:
        known = ", ".join(sorted(registry.classes))
        raise RoutingError(f"model class {model_class!r} is not in the registry (known: {known})")
    primary = registry.classes[model_class]
    chain = tuple(registry.models[primary].fallbacks)
    if override is None:
        return ResolvedModel(model_class, primary, chain, "registry")
    return ResolvedModel(
        model_class,
        override,
        tuple(m for m in chain if m != override),
        "override",
        in_registry=override in registry.models,
    )
