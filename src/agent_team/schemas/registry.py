"""canonical/routing/registry.yaml: model entries and class -> model resolution (section 6 of docs/ARCHITECTURE.md).

The registry is the one canonical file allowed to name vendors and model ids.
Agents name a class (`strong-coding`, `independent`); the registry maps each
class to a model, and each model carries its own one-step fallback chain.
"""

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from agent_team.schemas.common import CanonicalModel, KebabName, reject_vendor_names

# Model ids as runtimes spell them: claude-opus-5-5, anthropic.claude-opus-5-5, qwen3:8b.
ModelId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@-]*$", max_length=128)]

# vendor-cloud: the provider's hosted API sees the code. local-only: inference never leaves the host.
PrivacyClass = Literal["vendor-cloud", "local-only"]

# Claude Code keeps at most three models in a chain (primary included), so two fallbacks.
MAX_FALLBACKS = 2


class Cost(CanonicalModel):
    """List price in USD per million tokens, for estimates; the runtime's own report is the record."""

    input_usd_per_mtok: float = Field(ge=0)
    output_usd_per_mtok: float = Field(ge=0)


class ModelEntry(CanonicalModel):
    provider: KebabName
    capabilities: list[KebabName] = Field(default_factory=list)
    context_tokens: int | None = Field(default=None, ge=1)
    cost: Cost | None = None
    privacy_class: PrivacyClass
    fallbacks: list[ModelId] = Field(
        default_factory=list,
        max_length=MAX_FALLBACKS,
        description="Tried in order when this model is overloaded or unavailable; never transitive.",
    )

    @field_validator("fallbacks")
    @classmethod
    def _no_duplicate_fallbacks(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError(f"fallbacks repeat a model: {value}")
        return value


class RegistrySpec(CanonicalModel):
    kind: Literal["registry"]
    models: dict[ModelId, ModelEntry] = Field(min_length=1)
    classes: dict[KebabName, ModelId] = Field(min_length=1, description="Model class -> model id.")

    @field_validator("classes")
    @classmethod
    def _class_names_are_classes(cls, value: dict[str, str]) -> dict[str, str]:
        for name in value:
            reject_vendor_names(name)
        return value

    @model_validator(mode="after")
    def _references_resolve(self) -> "RegistrySpec":
        for name, model in self.classes.items():
            if model not in self.models:
                raise ValueError(f"class {name!r} resolves to {model!r}, which has no entry under models")
        for model, entry in self.models.items():
            for fallback in entry.fallbacks:
                if fallback == model:
                    raise ValueError(f"model {model!r} lists itself as a fallback")
                if fallback not in self.models:
                    raise ValueError(f"model {model!r} falls back to {fallback!r}, which has no entry under models")
        return self
