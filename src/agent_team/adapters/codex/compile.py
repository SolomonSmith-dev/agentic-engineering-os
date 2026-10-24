"""Compile one canonical agent into Codex's native config (build-order step 12).

Output lands in `.agent-team/compiled/<run_id>/`, never in a target repository's
`.codex/` (only `configure`, step 8, writes there):

    agents/<name>.toml  the agent as a Codex agent file: name, description, developer_instructions, model
    config.toml         the session config, as dotted keys; `codex exec` gets the same keys as `-c key=value`
    schema.json         --output-schema, when the agent's output has a registered schema (strict form)

A Codex agent file defines a role that a session can spawn; it cannot set the
sandbox or approval policy, and `codex exec` itself does not run as a role. So
the agent file is emitted for inspection and for step 8, and the session gets
the same instructions through `-c developer_instructions=...`.

Codex has no hook this adapter uses, so protected paths cannot be checked per
tool call. They compile to srt `denyRead` rules instead (`deny_read`), which
hold for the Codex process and every command it runs.
"""

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_team.adapters.claude.compile import DEFAULT_PROTECTED, output_schema
from agent_team.adapters.codex.capabilities import CodexPermissions, resolve
from agent_team.schemas.agent import AgentSpec
from agent_team.schemas.common import Autonomy
from agent_team.schemas.policy import PolicySpec
from agent_team.schemas.skill import SkillSpec

# One srt denyRead entry: a gitignore-shaped glob, or a path srt must take literally.
DenyRead = str | dict[str, Any]


@dataclass(frozen=True)
class CompiledCodexAgent:
    name: str
    model: str
    ceiling: Autonomy
    directory: Path
    agent_toml: Path
    config_toml: Path
    schema_json: Path | None
    overrides: dict[str, Any]
    deny_read: list[DenyRead]
    permissions: CodexPermissions


def toml_value(value: Any) -> str:
    """`value` as a TOML value. JSON string escapes are valid TOML basic-string escapes."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list | tuple):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    raise TypeError(f"no TOML form for {type(value).__name__}")


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """`schema` in the shape OpenAI strict structured output takes.

    Codex sends `--output-schema` with `strict: true`. Strict mode wants every
    property listed in `required` (an optional one is a union with null) and
    `additionalProperties: false` on every object; `default` is dropped.
    """
    # unverified: that strict mode accepts the `minLength` and `minimum` keywords the schema keeps.

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(n) for n in node]
        if not isinstance(node, dict):
            return node
        out = {k: walk(v) for k, v in node.items() if k != "default"}
        if out.get("type") == "object" and isinstance(out.get("properties"), dict):
            out["required"] = list(out["properties"])
            out["additionalProperties"] = False
        return out

    return walk(schema)


def build_prompt(
    body: str,
    agent: AgentSpec,
    skills: list[tuple[SkillSpec, str]],
    permissions: CodexPermissions,
    test_commands: Sequence[str],
    has_schema: bool,
) -> str:
    parts = [body.strip(), "## Definition of done", *(f"- {item}" for item in agent.definition_of_done)]
    for spec, skill_body in skills:
        parts.append(f"## Skill: {spec.name}\n\n{spec.description}\n\n{skill_body.strip()}")
    tests = "\n".join(f"- `{cmd}`" for cmd in test_commands) if "test.run" in permissions.granted else ""
    environment = (
        "## Environment\n\n"
        "Your working directory is the repository checkout for this run. Run every command from it as written: "
        "no `cd`, no `git -C`, no absolute paths to the checkout. "
        f"The sandbox is `{permissions.sandbox_mode}`"
        + (": commands that write files fail. " if permissions.sandbox_mode == "read-only" else ". ")
        + "Protected files are unreadable by design; do not look for another way to reach a denied file or action."
    )
    if tests:
        environment += f"\n\nTest commands you may run:\n{tests}"
    parts.append(environment)
    if has_schema:
        parts.append(
            "## Output\n\nYour final message is the JSON object the output schema describes, and nothing else: "
            "no prose and no code fence around it."
        )
    return "\n\n".join(parts) + "\n"


def _is_ancestor(base: Path, paths: Iterable[Path]) -> bool:
    return any(p == base or p.is_relative_to(base) for p in paths)


def _deny_around(base: Path, keep: list[Path]) -> list[DenyRead]:
    """Every entry under `base` except the paths in `keep` and the directories leading to them."""
    if not base.is_dir():
        return []
    denied: list[DenyRead] = []
    for child in sorted(base.iterdir()):
        if child in keep:
            continue
        if child.is_dir() and not child.is_symlink() and _is_ancestor(child, keep):
            denied.extend(_deny_around(child, keep))
        else:
            denied.append({"path": str(child), "literal": True})
    return denied


def srt_deny_read(protected: Sequence[str], roots: Sequence[Path], home: Path, keep: Sequence[Path]) -> list[DenyRead]:
    """srt denyRead entries for gitignore-shaped protected paths.

    `~/...` is under home; a bare name such as `.env*` matches at any depth under
    each root; a pattern with a slash is relative to each root. A directory
    pattern that would hide a path the run needs (its worktree, temp dir or
    compiled files, all under `.agent-team/`) is expanded to the entries around
    that path instead, so `.agent-team/**` still hides the run record and every
    other run without hiding this run's own checkout.
    """
    keep = [Path(p) for p in keep]
    entries: list[DenyRead] = []
    for pattern in protected:
        if pattern.startswith("~/"):
            entries.append(str(home / pattern[2:]))
            continue
        bare = "/" not in pattern.rstrip("/")
        for root in roots:
            if bare:
                entries.append(f"{root}/**/{pattern}")
                continue
            relative = pattern.removesuffix("/**").rstrip("/")
            base = root / relative
            if pattern.endswith("/**") and _is_ancestor(base, keep) and "*" not in relative:
                entries.extend(_deny_around(base, keep))
            else:
                entries.append(str(root / pattern))
    unique: list[DenyRead] = []
    for entry in entries:
        if entry not in unique:
            unique.append(entry)
    return unique


def compile_agent(
    agent: AgentSpec,
    body: str,
    skills: list[tuple[SkillSpec, str]],
    policy: PolicySpec,
    *,
    ceiling: Autonomy,
    model: str,
    out_dir: Path,
    roots: list[Path],
    home: Path,
    keep: list[Path],
    test_commands: tuple[str, ...] = (),
) -> CompiledCodexAgent:
    """Write the compiled files for one agent under `out_dir` and describe them."""
    deny = [*agent.capabilities.deny, *policy.denied_capabilities]
    permissions = resolve(agent.capabilities.allow, deny, ceiling, test_commands=test_commands)
    protected = list(dict.fromkeys([*policy.protected_paths, *DEFAULT_PROTECTED]))
    schema = output_schema(agent)
    prompt = build_prompt(body, agent, skills, permissions, test_commands, schema is not None)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "agents").mkdir(exist_ok=True)

    # Every key here is a Codex config.toml key; invoke passes each as `-c key=value`.
    overrides: dict[str, Any] = {
        "model": model,
        "sandbox_mode": permissions.sandbox_mode,
        "approval_policy": "never",  # headless: a request for approval is a denial, never a prompt
        "web_search": "disabled",
        # Shell commands get the core variables only, and never one named like a key, secret or token.
        "shell_environment_policy.inherit": "core",
        "shell_environment_policy.ignore_default_excludes": False,
        "developer_instructions": prompt,
    }
    config_toml = out_dir / "config.toml"
    config_toml.write_text(
        "# Session config for codex exec; each key is passed as -c key=value. Not read from this file.\n"
        + "".join(f"{key} = {toml_value(value)}\n" for key, value in overrides.items()),
        encoding="utf-8",
    )

    agent_toml = out_dir / "agents" / f"{agent.name}.toml"
    agent_toml.write_text(
        f"name = {toml_value(agent.name)}\n"
        f"description = {toml_value(agent.purpose)}\n"
        f"model = {toml_value(model)}\n"
        f"developer_instructions = {toml_value(prompt)}\n",
        encoding="utf-8",
    )

    schema_json = None
    if schema is not None:
        schema_json = out_dir / "schema.json"
        schema_json.write_text(json.dumps(strict_schema(schema), indent=2) + "\n", encoding="utf-8")

    return CompiledCodexAgent(
        name=agent.name,
        model=model,
        ceiling=ceiling,
        directory=out_dir,
        agent_toml=agent_toml,
        config_toml=config_toml,
        schema_json=schema_json,
        overrides=overrides,
        deny_read=srt_deny_read(protected, roots, home, [*keep, out_dir]),
        permissions=permissions,
    )
