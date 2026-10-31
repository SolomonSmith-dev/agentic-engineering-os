"""Compile one canonical agent into Claude Code's native config (build-order step 6).

Output lands in `.agent-team/compiled/<run_id>/` and reaches Claude Code only
through flags, so nothing is written into the target repository's `.claude/`
(only `configure`, step 8, writes there):

    agents.json       --agents: the agent, its tools and its prompt
    settings.json     --settings: permission allow/deny rules, dontAsk, PreToolUse hook, fallbackModel
    policy.json       read by the hook: protected paths, the roots they resolve against, the write root
    focus.json        read by the PostToolUse focus hook: the task brief, test commands, thresholds
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
from agent_team.environments.sandbox import HOME_LOGIN_DIRS, HOME_LOGIN_FILES, KEY_FILES, DenyRead, srt_deny_read
from agent_team.schemas.adapter import OUTPUT_SCHEMAS
from agent_team.schemas.agent import AgentSpec
from agent_team.schemas.common import Autonomy
from agent_team.schemas.policy import PolicySpec
from agent_team.schemas.skill import SkillSpec

# Protected in every compiled session on top of the policy's own list (architecture section 8).
DEFAULT_PROTECTED: tuple[str, ...] = (
    ".env*", "*.pem", "~/.ssh/**", "~/.aws/**", "~/.config/gcloud/**", "~/.claude/**",
    # The Codex login: Codex itself runs on CODEX_API_KEY with an empty per-run CODEX_HOME.
    "~/.codex/**",
    # GitHub credentials: the orchestrator holds github.*, and a worker must never read the token.
    "~/.config/gh/**", "~/.git-credentials", "~/.netrc",
    # Registry, cluster and cloud logins a build tool would pick up from home.
    *(f"~/{d}/**" for d in HOME_LOGIN_DIRS), *(f"~/{f}" for f in HOME_LOGIN_FILES),
)  # fmt: skip

# Key files are denied as files (permission deny rules, srt denyRead) but not given to the hook:
# its scan of every word in a command would also block `jq .key` or `event.key`.
PROTECTED_FILES: tuple[str, ...] = KEY_FILES

# The Claude worker reads its own login and config here, so srt cannot deny it; the hook and
# the Read/Edit/Write deny rules still keep the agent's tools out of it.
CLAUDE_HOME = "~/.claude/"

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
    deny_read: list[DenyRead]  # srt denyRead entries for a sandboxed run: protected paths but ~/.claude
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
    body: str,
    agent: AgentSpec,
    skills: list[tuple[SkillSpec, str]],
    permissions: Permissions,
    has_schema: bool,
    lessons_section: str | None = None,
) -> str:
    parts = [body.strip(), "## Definition of done", *(f"- {item}" for item in agent.definition_of_done)]
    if lessons_section:
        parts.append(lessons_section.strip())
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
    sandboxed: bool = False,
    keep: tuple[Path, ...] = (),
    python: str = sys.executable,
    focus_brief: str | None = None,
    planned_files: tuple[str, ...] = (),
    lessons_section: str | None = None,
) -> CompiledAgent:
    """Write the compiled files for one agent under `out_dir` and describe them.

    `roots[0]` is the session's working directory, the only place file tools may write.
    `focus_brief` turns on the PostToolUse focus hook (see `focus_hook`); `lessons_section`
    is appended to the prompt after the definition of done. `keep` names paths the run needs
    that a protected pattern would hide (its worktree and temp dir).
    """
    deny = [*agent.capabilities.deny, *policy.denied_capabilities]
    permissions = resolve(agent.capabilities.allow, deny, ceiling, test_commands=test_commands, sandboxed=sandboxed)
    protected = list(dict.fromkeys([*policy.protected_paths, *DEFAULT_PROTECTED]))
    schema = output_schema(agent)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "agents").mkdir(exist_ok=True)
    policy_json = out_dir / "policy.json"
    policy_json.write_text(
        json.dumps(
            {"protected_paths": protected, "roots": [str(r) for r in roots], "home": str(home), "write_root": str(roots[0])},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    path_denies = [
        path_rule(tool, p) for p in dict.fromkeys([*protected, *PROTECTED_FILES]) for tool in ("Read", "Edit", "Write")
    ]
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
    if focus_brief:
        focus_json = out_dir / "focus.json"
        focus_json.write_text(
            json.dumps(
                {
                    "brief": focus_brief,
                    "test_commands": list(test_commands),
                    "planned_files": list(planned_files),
                    "write_root": str(roots[0]),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        settings["hooks"]["PostToolUse"] = [
            {
                "matcher": "*",
                "hooks": [
                    {
                        "type": "command",
                        "command": shlex.join([python, "-m", "agent_team.adapters.claude.focus_hook", str(focus_json)]),
                    }
                ],
            }
        ]
    if fallback_models:
        # Tried in order when the model is overloaded or unavailable; Claude Code reads a list here.
        settings["fallbackModel"] = list(fallback_models)
    settings_json = out_dir / "settings.json"
    settings_json.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")

    # Agent-level disallowedTools removes a whole tool, so only bare tool names go there.
    disallowed = [r for r in permissions.deny if "(" not in r]
    definition: dict[str, Any] = {
        "description": agent.purpose,
        "prompt": build_prompt(body, agent, skills, permissions, schema is not None, lessons_section),
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
        deny_read=srt_deny_read(
            [p for p in dict.fromkeys([*protected, *PROTECTED_FILES]) if not p.startswith(CLAUDE_HOME)],
            roots,
            home,
            [*keep, out_dir],
        ),
    )
