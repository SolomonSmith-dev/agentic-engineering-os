"""Compile one canonical agent into Claude Code's native config (build-order step 6).

Output lands in `.agent-team/compiled/<run_id>/` and reaches Claude Code only
through flags, so nothing is written into the target repository's `.claude/`
(only `configure`, step 8, writes there):

    agents.json       --agents: the agent, its tools and its prompt
    settings.json     --settings: permission allow/deny rules, dontAsk, PreToolUse hook, fallbackModel
    policy.json       read by the hook: protected paths and the roots they resolve against
    agents/<name>.md  the same agent as a native subagent file, for inspection and step 8
    schema.json       --json-schema, when the agent's output has a registered schema

Skills are inlined into the prompt rather than preloaded through the `skills`
field, so the agent sees them whatever Claude Code's skill discovery does.
"""

import json
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from agent_team.adapters.claude.capabilities import Permissions, resolve
from agent_team.schemas.adapter import OUTPUT_SCHEMAS
from agent_team.schemas.agent import AgentSpec
from agent_team.schemas.common import Autonomy
from agent_team.schemas.policy import PolicySpec
from agent_team.schemas.skill import SkillSpec

# Protected in every compiled session on top of the policy's own list (architecture section 8).
DEFAULT_PROTECTED: tuple[str, ...] = (
    ".env*", "*.pem", "~/.ssh/**", "~/.aws/**", "~/.config/gcloud/**", "~/.claude/**",
)  # fmt: skip

PERMISSION_MODE = "dontAsk"

# Claude Code adds this tool when --json-schema is set and the agent returns its answer through
# it; an agent `tools` allowlist that leaves it out hides the schema from the model.
STRUCTURED_OUTPUT_TOOL = "StructuredOutput"


@dataclass(frozen=True)
class CompiledAgent:
    name: str
    model: str
    fallback_models: tuple[str, ...]
    ceiling: Autonomy
    directory: Path
    agents_json: Path
    settings_json: Path
    policy_json: Path
    native_md: Path
    schema_json: Path | None
    permissions: Permissions


def output_schema(agent: AgentSpec) -> dict[str, Any] | None:
    """JSON Schema for the agent's structured output, when its single output has one."""
    registered = [OUTPUT_SCHEMAS[o] for o in agent.outputs if o in OUTPUT_SCHEMAS]
    return registered[0].model_json_schema() if len(registered) == 1 else None


def path_rule(tool: str, pattern: str) -> str:
    """A permission rule for a gitignore-shaped pattern; bare names match at any depth."""
    if pattern.startswith("~/"):
        return f"{tool}({pattern})"
    if "/" not in pattern.rstrip("/"):
        return f"{tool}(**/{pattern})"
    return f"{tool}(./{pattern})"


def build_prompt(
    body: str, agent: AgentSpec, skills: list[tuple[SkillSpec, str]], permissions: Permissions, has_schema: bool
) -> str:
    parts = [body.strip(), "## Definition of done", *(f"- {item}" for item in agent.definition_of_done)]
    for spec, skill_body in skills:
        parts.append(f"## Skill: {spec.name}\n\n{spec.description}\n\n{skill_body.strip()}")
    allowed = "\n".join(f"- `{rule}`" for rule in permissions.allow) or "- none"
    parts.append(
        "## Environment\n\n"
        "Your working directory is the repository checkout for this run. Run every command from it as written: "
        "no `cd`, no `git -C`, no absolute paths to the checkout. Any call that no rule below allows is denied "
        "without a prompt; do not look for another way to do a denied action.\n\n"
        f"Allowed:\n{allowed}"
    )
    if has_schema:
        parts.append(
            f"## Output\n\nReturn your answer by calling the `{STRUCTURED_OUTPUT_TOOL}` tool with an object that "
            "matches its schema. Do not write the JSON into your reply instead."
        )
    return "\n\n".join(parts) + "\n"


def compile_agent(
    agent: AgentSpec,
    body: str,
    skills: list[tuple[SkillSpec, str]],
    policy: PolicySpec,
    *,
    ceiling: Autonomy,
    model: str,
    fallback_models: tuple[str, ...] = (),
    out_dir: Path,
    roots: list[Path],
    home: Path,
    test_commands: tuple[str, ...] = (),
    python: str = sys.executable,
) -> CompiledAgent:
    """Write the compiled files for one agent under `out_dir` and describe them."""
    deny = [*agent.capabilities.deny, *policy.denied_capabilities]
    permissions = resolve(agent.capabilities.allow, deny, ceiling, test_commands=test_commands)
    protected = list(dict.fromkeys([*policy.protected_paths, *DEFAULT_PROTECTED]))
    schema = output_schema(agent)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "agents").mkdir(exist_ok=True)
    policy_json = out_dir / "policy.json"
    policy_json.write_text(
        json.dumps({"protected_paths": protected, "roots": [str(r) for r in roots], "home": str(home)}, indent=2) + "\n",
        encoding="utf-8",
    )

    path_denies = [path_rule(tool, p) for p in protected for tool in ("Read", "Edit", "Write")]
    settings = {
        "permissions": {
            "defaultMode": PERMISSION_MODE,
            "allow": permissions.allow,
            "deny": [*permissions.deny, *path_denies],
        },
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": shlex.join([python, "-m", "agent_team.adapters.claude.hook", str(policy_json)]),
                        }
                    ],
                }
            ]
        },
        "attribution": {"commit": "", "pr": ""},
    }
    if fallback_models:
        # Tried in order when the model is overloaded or unavailable; Claude Code reads a list here.
        settings["fallbackModel"] = list(fallback_models)
    settings_json = out_dir / "settings.json"
    settings_json.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")

    # Agent-level disallowedTools removes a whole tool, so only bare tool names go there.
    disallowed = [r for r in permissions.deny if "(" not in r]
    definition: dict[str, Any] = {
        "description": agent.purpose,
        "prompt": build_prompt(body, agent, skills, permissions, schema is not None),
        "tools": [*permissions.tools, STRUCTURED_OUTPUT_TOOL] if schema is not None else permissions.tools,
        "disallowedTools": disallowed,
        "model": model,
        "permissionMode": PERMISSION_MODE,
    }
    agents_json = out_dir / "agents.json"
    agents_json.write_text(json.dumps({agent.name: definition}, indent=2) + "\n", encoding="utf-8")

    frontmatter = {"name": agent.name, **{k: v for k, v in definition.items() if k != "prompt"}}
    native_md = out_dir / "agents" / f"{agent.name}.md"
    native_md.write_text(
        "---\n" + yaml.safe_dump(frontmatter, sort_keys=False) + "---\n\n" + definition["prompt"], encoding="utf-8"
    )

    schema_json = None
    if schema is not None:
        schema_json = out_dir / "schema.json"
        schema_json.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")

    return CompiledAgent(
        name=agent.name,
        model=model,
        fallback_models=tuple(fallback_models),
        ceiling=ceiling,
        directory=out_dir,
        agents_json=agents_json,
        settings_json=settings_json,
        policy_json=policy_json,
        native_md=native_md,
        schema_json=schema_json,
        permissions=permissions,
    )
