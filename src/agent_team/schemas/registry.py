"""routing/registry.yaml: model entries and model class -> model resolution (build-order step 7).

An agent names a model class; the registry maps each class to one model entry,
and each entry lists the fallbacks its runtime may switch to when the model is
overloaded or unavailable. `runtime_classes` maps a class to a model on another
runtime, used only when a run selects that runtime (the reviewer on Codex, step
12). Routing is a lookup, not a learned policy.
"""

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from agent_team.schemas.common import CanonicalModel, KebabName, SnakeName, reject_vendor_names

# Model ids as the runtime takes them: claude-opus-5-5, gpt-5.1-codex.
ModelId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]*$", max_length=128)]

# Runtimes with an adapter, or one on the build order (codex, step 12).
Runtime = Literal["claude", "codex"]

# Where the code a model sees ends up. vendor-cloud: the vendor's API under its standard terms.
# vendor-zdr: the vendor's API under a zero-data-retention agreement. local-only: never leaves the host.
PrivacyClass = Literal["vendor-cloud", "vendor-zdr", "local-only"]


class ModelCost(CanonicalModel):
    input_per_mtok_usd: float = Field(ge=0)
    output_per_mtok_usd: float = Field(ge=0)


class ModelEntry(CanonicalModel):
    provider: KebabName
    runtime: Runtime
    privacy_class: PrivacyClass
    capabilities: list[SnakeName] = Field(default_factory=list, description="e.g. tool_use, structured_output.")
    cost: ModelCost | None = Field(default=None, description="List price; the run record uses the runtime's report.")
    fallbacks: list[ModelId] = Field(
        default_factory=list, description="Models tried in order when this one is overloaded or unavailable."
    )


class RegistrySpec(CanonicalModel):
    kind: Literal["registry"]
    name: KebabName
    models: dict[ModelId, ModelEntry] = Field(min_length=1)
    classes: dict[KebabName, ModelId] = Field(min_length=1, description="Model class -> model id in `models`.")
    runtime_classes: dict[Runtime, dict[KebabName, ModelId]] = Field(
        default_factory=dict,
        description="Runtime -> model class -> model id on that runtime, for runs that select the runtime.",
    )

    @field_validator("classes")
    @classmethod
    def _classes_are_not_vendor_names(cls, value: dict[str, str]) -> dict[str, str]:
        for name in value:
            reject_vendor_names(name)
        return value

    @field_validator("runtime_classes")
    @classmethod
    def _runtime_classes_are_not_vendor_names(cls, value: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
        for classes in value.values():
            for name in classes:
                reject_vendor_names(name)
        return value

    @model_validator(mode="after")
    def _references_resolve(self) -> "RegistrySpec":
        for name, model in self.classes.items():
            if model not in self.models:
                raise ValueError(f"class {name!r} maps to {model!r}, which has no entry under models")
        for runtime, classes in self.runtime_classes.items():
            for name, model in classes.items():
                if model not in self.models:
                    raise ValueError(f"{runtime} class {name!r} maps to {model!r}, which has no entry under models")
                if self.models[model].runtime != runtime:
                    raise ValueError(
                        f"{runtime} class {name!r} maps to {model!r}, which runs on {self.models[model].runtime}"
                    )
        for model, entry in self.models.items():
            for fallback in entry.fallbacks:
                if fallback == model:
                    raise ValueError(f"model {model!r} lists itself as a fallback")
                if fallback not in self.models:
                    raise ValueError(f"model {model!r} falls back to {fallback!r}, which has no entry under models")
                if self.models[fallback].runtime != entry.runtime:
                    raise ValueError(
                        f"model {model!r} ({entry.runtime}) falls back to {fallback!r} "
                        f"({self.models[fallback].runtime}); a fallback must run on the same runtime"
                    )
        return self
