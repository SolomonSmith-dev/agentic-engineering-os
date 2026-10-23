"""Load and validate the files under canonical/ against the pydantic schemas.

Layout (section 6 of docs/ARCHITECTURE.md):

    canonical/agents/*.md           frontmatter + prompt body
    canonical/skills/*/SKILL.md     frontmatter + procedure body
    canonical/workflows/*.yaml
    canonical/tools/*.yaml
    canonical/policies/*.yaml

Every problem is reported as a `Problem(file, field, message)` so the caller
can print `path: field: message` and the user knows exactly what to fix.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from agent_team.schemas import SCHEMAS

FRONTMATTER_FENCE = "---"

# Directory under canonical/ -> (kind, glob of files that must validate)
LAYOUT: dict[str, tuple[str, str]] = {
    "agents": ("agent", "*.md"),
    "skills": ("skill", "*/SKILL.md"),
    "workflows": ("workflow", "*.yaml"),
    "tools": ("tool", "*.yaml"),
    "policies": ("policy", "*.yaml"),
}

BODY_REQUIRED = {"agent", "skill"}


@dataclass(frozen=True)
class Problem:
    file: Path
    field: str
    message: str

    def render(self, root: Path | None = None) -> str:
        shown = self.file
        if root is not None:
            try:
                shown = self.file.relative_to(root)
            except ValueError:
                pass
        return f"{shown}: {self.field}: {self.message}"


@dataclass
class Document:
    path: Path
    kind: str
    data: dict[str, Any]
    body: str
    spec: Any = None  # the validated pydantic model, when validation passed


@dataclass
class Report:
    root: Path
    documents: list[Document] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def split_frontmatter(text: str) -> tuple[str, str]:
    """Return (frontmatter_yaml, body). Raises ValueError when the fences are missing."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != FRONTMATTER_FENCE:
        raise ValueError("file must start with a '---' frontmatter fence")
    for index in range(1, len(lines)):
        if lines[index].strip() == FRONTMATTER_FENCE:
            return "".join(lines[1:index]), "".join(lines[index + 1 :])
    raise ValueError("frontmatter opened with '---' but never closed")


def parse_file(path: Path, kind: str) -> tuple[dict[str, Any], str]:
    """Parse one canonical file into (mapping, body)."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".md":
        raw, body = split_frontmatter(text)
    else:
        raw, body = text, ""
    loaded = yaml.safe_load(raw)
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise ValueError(f"top level must be a mapping, got {type(loaded).__name__}")
    return loaded, body


def _loc_to_field(loc: tuple[Any, ...]) -> str:
    if not loc:
        return "<root>"
    return ".".join(str(part) for part in loc)


def validate_document(path: Path, kind: str) -> tuple[Document | None, list[Problem]]:
    """Validate one file. Returns the document (spec set on success) and any problems."""
    try:
        data, body = parse_file(path, kind)
    except (ValueError, yaml.YAMLError) as exc:
        return None, [Problem(path, "<file>", str(exc).splitlines()[0])]

    problems: list[Problem] = []
    if kind in BODY_REQUIRED and not body.strip():
        problems.append(Problem(path, "body", f"{kind} body is empty; the body is the prompt"))

    doc = Document(path=path, kind=kind, data=data, body=body)
    model = SCHEMAS[kind]
    try:
        doc.spec = model.model_validate(data)
    except ValidationError as exc:
        for err in exc.errors():
            problems.append(Problem(path, _loc_to_field(err["loc"]), err["msg"]))
    return doc, problems


def _cross_check(report: Report) -> None:
    """Referential rules that span files: an agent's skills must exist."""
    skills = {d.spec.name for d in report.documents if d.kind == "skill" and d.spec is not None}
    for doc in report.documents:
        if doc.kind != "agent" or doc.spec is None:
            continue
        for skill in doc.spec.skills:
            if skill not in skills:
                report.problems.append(
                    Problem(
                        doc.path,
                        "skills",
                        f"skill {skill!r} has no canonical/skills/{skill}/SKILL.md",
                    )
                )
        if doc.spec.name != doc.path.stem:
            report.problems.append(
                Problem(doc.path, "name", f"name {doc.spec.name!r} must match file stem {doc.path.stem!r}")
            )
    for doc in report.documents:
        if doc.kind == "skill" and doc.spec is not None and doc.spec.name != doc.path.parent.name:
            report.problems.append(
                Problem(
                    doc.path,
                    "name",
                    f"name {doc.spec.name!r} must match directory {doc.path.parent.name!r}",
                )
            )


def validate_tree(root: Path) -> Report:
    """Validate every canonical file under `root` (a canonical/ directory)."""
    report = Report(root=root)
    if not root.is_dir():
        report.problems.append(Problem(root, "<dir>", "canonical directory not found"))
        return report

    for subdir, (kind, pattern) in LAYOUT.items():
        for path in sorted((root / subdir).glob(pattern)):
            doc, problems = validate_document(path, kind)
            if doc is not None:
                report.documents.append(doc)
            report.problems.extend(problems)

    if not report.documents and not report.problems:
        report.problems.append(Problem(root, "<dir>", "no canonical files found"))
        return report

    _cross_check(report)
    report.problems.sort(key=lambda p: (str(p.file), p.field))
    return report
