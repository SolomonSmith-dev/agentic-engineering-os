"""Shared field types and validators for every canonical schema."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# kebab-case identifiers: agent names, skill names, workflow names.
KebabName = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$", min_length=1, max_length=64),
]

# snake_case identifiers: inputs, outputs, state names.
SnakeName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")]

# Dotted capability names with an optional trailing wildcard segment: fs.read, github.pr.*
Capability = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*(\.\*)?$"),
]

# Trigger strings: <namespace>:<expression>, e.g. state:CODE_REVIEW, policy:autonomy>=A3
Trigger = Annotated[str, StringConstraints(pattern=r"^(state|policy|event|manual):\S+$")]

# Autonomy levels A0 (read-only) through A4 (unattended merge).
Autonomy = Literal["A0", "A1", "A2", "A3", "A4"]

AUTONOMY_ORDER: dict[str, int] = {"A0": 0, "A1": 1, "A2": 2, "A3": 3, "A4": 4}

# Tokens that mark a vendor or a model id. An agent names a model class, never one of these.
VENDOR_TOKENS: tuple[str, ...] = (
    "anthropic",
    "claude",
    "opus",
    "sonnet",
    "haiku",
    "openai",
    "gpt",
    "o1",
    "o3",
    "codex",
    "gemini",
    "google",
    "llama",
    "mistral",
)


def reject_vendor_names(value: str) -> str:
    """Fail when a model class smuggles in a vendor or model name."""
    lowered = value.lower()
    for token in VENDOR_TOKENS:
        if token in lowered.split("-"):
            raise ValueError(
                f"model_class {value!r} names a vendor or model ({token!r}); "
                "use a class resolved by routing/registry.yaml"
            )
    return value


class CanonicalModel(BaseModel):
    """Base for every canonical file: unknown keys are errors, never ignored."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


NonEmptyList = Field(min_length=1)
