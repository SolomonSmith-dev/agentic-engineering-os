"""Canonical pydantic models (build-order step 2).

One model per canonical kind. `SCHEMAS` maps the kind name to its model and is
the single registry the JSON Schema export and `agent-team validate` read.
"""

from typing import Any

from pydantic import BaseModel

from agent_team.schemas.agent import AgentSpec, Capabilities
from agent_team.schemas.doctor import DoctorReport
from agent_team.schemas.environment import EnvironmentSpec, NetworkPolicy, PortPolicy
from agent_team.schemas.policy import PolicySpec, RedactionRule
from agent_team.schemas.registry import ModelEntry, RegistrySpec
from agent_team.schemas.runs import (
    Checkpoint,
    RunOutcome,
    RunRecord,
    RunState,
    StateTransition,
    ToolCallRecord,
    ToolResultStatus,
)
from agent_team.schemas.skill import SkillSpec
from agent_team.schemas.tool import Implementation, ToolSpec
from agent_team.schemas.workflow import Node, Transition, WorkflowSpec

SCHEMAS: dict[str, type[BaseModel]] = {
    "agent": AgentSpec,
    "skill": SkillSpec,
    "workflow": WorkflowSpec,
    "tool": ToolSpec,
    "policy": PolicySpec,
    "environment": EnvironmentSpec,
    "registry": RegistrySpec,
}

SCHEMA_ID_BASE = "https://github.com/SolomonSmith-dev/agentic-engineering-os/schemas"


def json_schema(kind: str) -> dict[str, Any]:
    """JSON Schema (draft 2020-12) for one canonical kind."""
    model = SCHEMAS[kind]
    schema = model.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"{SCHEMA_ID_BASE}/{kind}.schema.json"
    return schema


__all__ = [
    "SCHEMAS",
    "AgentSpec",
    "Capabilities",
    "Checkpoint",
    "DoctorReport",
    "EnvironmentSpec",
    "Implementation",
    "ModelEntry",
    "NetworkPolicy",
    "Node",
    "PolicySpec",
    "PortPolicy",
    "RedactionRule",
    "RegistrySpec",
    "RunOutcome",
    "RunRecord",
    "RunState",
    "SkillSpec",
    "StateTransition",
    "ToolCallRecord",
    "ToolResultStatus",
    "ToolSpec",
    "Transition",
    "WorkflowSpec",
    "json_schema",
]
