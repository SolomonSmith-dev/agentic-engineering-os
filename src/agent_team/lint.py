"""Agent-definition lint: rules stricter than the schemas, run by `agent-team validate --lint`.

Rules read the raw parsed mapping (`Document.data`), not the validated spec, so a
file that already fails schema validation is still linted.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Any

import yaml

from agent_team.canonical import Document, Problem, Report, split_frontmatter

WRITE_CAPS: tuple[str, ...] = ("fs.write.*", "git.commit", "git.write.*", "shell.execute.*", "github.pr.*", "deploy.*")
READ_CAPS: tuple[str, ...] = ("fs.read", "git.read", "shell.read")
REVIEWER_DENIES: tuple[str, ...] = ("fs.write.*", "git.commit")
ROLE_OPENING = "You are"


def cap_matches(cap: str, pattern: str) -> bool:
    """True when either side, read as an fnmatch glob, matches the other."""
    return fnmatchcase(cap, pattern) or fnmatchcase(pattern, cap)


class DuplicateKeyError(yaml.YAMLError):
    pass


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses a mapping with a repeated key."""


def _construct_mapping(loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in seen
        except TypeError:  # unhashable key; the safe constructor reports it below
            continue
        if duplicate:
            raise DuplicateKeyError(f"duplicate key {key!r} (line {key_node.start_mark.line + 1})")
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def _strict_yaml(doc: Document) -> list[Problem]:
    try:
        text = doc.path.read_text(encoding="utf-8")
        raw = split_frontmatter(text)[0] if doc.path.suffix == ".md" else text
        yaml.load(raw, Loader=_StrictLoader)  # SafeLoader subclass
    except DuplicateKeyError as exc:
        return [Problem(doc.path, "<file>", str(exc))]
    except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError) as exc:
        return [Problem(doc.path, "<file>", str(exc).splitlines()[0])]
    return []


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _agent_rules(doc: Document) -> list[Problem]:
    problems: list[Problem] = []
    data = doc.data
    caps = data.get("capabilities")
    caps = caps if isinstance(caps, dict) else {}
    allow, deny = _str_list(caps.get("allow")), _str_list(caps.get("deny"))

    for cap in allow:
        hits = [d for d in deny if cap_matches(cap, d)]
        if hits:
            problems.append(Problem(doc.path, "capabilities", f"allow {cap!r} is matched by deny {hits}"))

    if data.get("workspace") == "read_only_mirror":
        for cap in allow:
            if any(cap_matches(cap, w) for w in WRITE_CAPS):
                problems.append(
                    Problem(doc.path, "capabilities.allow", f"read_only_mirror workspace allows write capability {cap!r}")
                )
    if data.get("max_autonomy") == "A0":
        for cap in allow:
            if cap not in READ_CAPS:
                problems.append(
                    Problem(doc.path, "capabilities.allow", f"A0 agent may only read {list(READ_CAPS)}; allows {cap!r}")
                )
    if "review_verdict" in _str_list(data.get("outputs")):
        for required in REVIEWER_DENIES:
            if required not in deny:
                problems.append(
                    Problem(doc.path, "capabilities.allow", f"a review_verdict agent must deny {required!r}")
                )

    first = next((line.strip() for line in doc.body.splitlines() if line.strip()), "")
    if not first.startswith(ROLE_OPENING):
        problems.append(Problem(doc.path, "body", f"first line must open the role paragraph with {ROLE_OPENING!r}"))
    return problems


def lint_tree(report: Report) -> list[Problem]:
    """Lint every document `validate_tree` parsed. Returns lint problems only."""
    problems: list[Problem] = []
    for doc in report.documents:
        problems.extend(_strict_yaml(doc))
        if doc.kind == "agent":
            problems.extend(_agent_rules(doc))
    problems.sort(key=lambda p: (str(p.file), p.field))
    return problems
