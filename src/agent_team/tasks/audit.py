"""Ported crew-audit rules: task text, declared paths, the target at base, dependency changes.

Every rule yields an `AuditFinding`; `critical` and `high` refuse the run (`refusals`).
Git is only ever run through `orchestrator.workspace.git` (no hooks, no inherited repo env).
"""

import codecs
import json
import re
import tomllib
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from agent_team.orchestrator.workspace import git, git_bytes
from agent_team.tasks.io import LoadedTaskFile
from agent_team.tasks.paths import WHOLE_REPO, normalize

Severity = Literal["critical", "high", "medium", "info"]
REFUSING: frozenset[Severity] = frozenset({"critical", "high"})
EVIDENCE_MAX = 160


@dataclass(frozen=True)
class AuditFinding:
    rule: str
    severity: Severity
    where: str
    line: int | None
    evidence: str

    def render(self) -> str:
        at = f":{self.line}" if self.line else ""
        return f"{self.rule} ({self.severity}) {_evidence(self.where)}{at}: {_evidence(self.evidence)}"


def refusals(findings: Iterable[AuditFinding]) -> list[AuditFinding]:
    return [f for f in findings if f.severity in REFUSING]


def _evidence(text: str) -> str:
    flat = " ".join(text.split())
    flat = "".join(c for c in flat if not unicodedata.category(c).startswith("C"))  # no ANSI/control/format chars
    return flat if len(flat) <= EVIDENCE_MAX else flat[: EVIDENCE_MAX - 3] + "..."


# ---- task text -----------------------------------------------------------------------------

_AUD = re.compile(r"\b(?:audit(?:or|ors|ing|s)?|reviewer|scanner|findings?|security\s+(?:check|review))\b", re.I)
_AI = re.compile(r"\b(?:AI|LLM|assistant|Claude|GPT|chatbot)\b", re.I)
_IMPER = re.compile(r"\b(?:ignore|skip|do\s+not|don't|never|report|mark|classify|treat|pretend)\b", re.I)
_PHRASE = re.compile(
    r"ignore\s+(?:all\s+)?(?:previous|prior|above)\s+(?:instructions|findings|rules)"
    r"|\byou\s+are\s+now\b|disregard\s+(?:all\s+|any\s+)?(?:previous|prior)"
    r"|\bsystem\s+prompt\b|\bdo\s+not\s+(?:report|flag|mention)\b|\breport\s+no\s+findings\b",
    re.I,
)
_COMMENT = re.compile(r"<!--(.*?)-->", re.S)
_BOM = "﻿"
# Direction marks that reorder text; everything else invisible goes to unicode-invisible.
_BIDI_CHARS = frozenset(chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A), 0x200E, 0x200F, 0x061C))
# Invisible or filler code points outside the Cf/Zl/Zp/Co categories.
_EXTRA_INVISIBLE = frozenset(
    chr(c)
    for c in (0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, *range(0x180B, 0x180F), 0x3164, 0xFFA0,
              *range(0xFE00, 0xFE10), *range(0xE0100, 0xE01F0))
)  # fmt: skip


def _is_invisible(ch: str) -> bool:
    if ch in _BIDI_CHARS:
        return False
    return ch in _EXTRA_INVISIBLE or unicodedata.category(ch) in ("Cf", "Zl", "Zp", "Co")


def _clean(text: str) -> str:
    """NFKC-fold and drop invisible and bidi characters, so hostile Unicode cannot split a phrase."""
    folded = unicodedata.normalize("NFKC", text)
    return "".join(c for c in folded if not (_is_invisible(c) or c in _BIDI_CHARS))


def _collapse(text: str) -> str:
    return " ".join(text.split())


def audit_text(text: str, where: str = "task file") -> list[AuditFinding]:
    found: list[AuditFinding] = []
    lines = re.split(r"\r\n|\n|\r", text)
    seen_phrases: set[str] = set()
    for n, raw in enumerate(lines, start=1):
        line = _clean(raw)
        for m in _PHRASE.finditer(_collapse(line)):
            seen_phrases.add(m.group(0).casefold())
        if _PHRASE.search(line):
            found.append(AuditFinding("inject-phrase", "critical" if _AUD.search(line) else "high", where, n, _evidence(line)))
        scan = raw.removeprefix(_BOM) if n == 1 else raw
        if any(_is_invisible(c) for c in scan):
            found.append(AuditFinding("unicode-invisible", "high", where, n, "invisible or zero-width character"))
        if any(c in _BIDI_CHARS for c in scan):
            found.append(AuditFinding("unicode-bidi", "high", where, n, "bidirectional control character"))
    joined = _collapse(_clean(text))
    for m in _PHRASE.finditer(joined):  # a phrase wrapped across lines
        if m.group(0).casefold() not in seen_phrases:
            sev: Severity = "critical" if _AUD.search(joined) else "high"
            found.append(AuditFinding("inject-phrase", sev, where, None, _evidence(m.group(0))))
    body_text = _clean(text)
    for m in _COMMENT.finditer(body_text):
        body = m.group(1)
        if not (_AI.search(body) or (_AUD.search(body) and _IMPER.search(body))):
            continue
        critical = _AUD.search(body) and (_AI.search(body) or _IMPER.search(body))
        line_no = 1 + body_text.count("\n", 0, m.start())
        found.append(AuditFinding("inject-hidden-comment", "critical" if critical else "high", where, line_no, _evidence(body)))
    rest = _COMMENT.sub(lambda m: " " * len(m.group(0)), body_text)
    if (at := rest.find("<!--")) != -1:
        found.append(AuditFinding("inject-hidden-comment", "high", where, 1 + rest.count("\n", 0, at), "unterminated HTML comment"))
    return sorted(found, key=lambda f: f.line or 0)


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def audit_task_file(loaded: LoadedTaskFile) -> list[AuditFinding]:
    """Raw text, then every decoded string of the parsed task (escapes and folded scalars hide phrases in raw YAML)."""
    found = audit_text(loaded.text) + audit_declared_paths(loaded.task.paths)
    for value in _strings(loaded.task.model_dump(mode="python")):
        for f in audit_text(value, where="task file value"):
            if not any(g.rule == f.rule and f.evidence in g.evidence for g in found):
                found.append(f)
    return found


# ---- declared paths ------------------------------------------------------------------------


def _key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def audit_declared_paths(paths: Sequence[str]) -> list[AuditFinding]:
    found: list[AuditFinding] = []
    for p in paths:
        try:
            if p.startswith(":"):
                raise ValueError(f"scope path {p!r} starts with ':' (git pathspec magic)")
            norm = normalize(p)
            if any(_key(part) == ".git" for part in norm.split("/")):
                raise ValueError(f"scope path {p!r} has a .git component")
        except ValueError as exc:
            found.append(AuditFinding("path-invalid", "high", p, None, str(exc)))
    return found


# ---- the target at base --------------------------------------------------------------------

_MODE_RULES = {"120000": ("path-symlink", "is a symlink at base"), "160000": ("path-submodule", "is a submodule at base")}


def _tree_entries(repo: Path, commit: str, parent: str) -> dict[str, list[tuple[str, str]]] | None:
    """Entries of one tree as {folded name: [(mode, name), ...]}; None when git failed.

    Names that fold to the same key are all kept: a case-insensitive checkout would merge them.
    """
    target = f"{commit}:{parent}" if parent else commit
    res = git(repo, "ls-tree", "-z", target, check=False)
    if res.returncode != 0:
        return None
    entries: dict[str, list[tuple[str, str]]] = {}
    for rec in filter(None, res.stdout.split("\0")):
        meta, _, name = rec.partition("\t")
        entries.setdefault(_key(name), []).append((meta.split(" ", 1)[0], name))
    return entries


def audit_target(repo: Path, base: str, paths: Sequence[str]) -> tuple[str | None, list[AuditFinding]]:
    unknown = AuditFinding("base-unknown", "high", base, None, f"base {base!r} does not resolve to a commit")
    if not base or base.startswith("-"):
        return None, [unknown]
    resolved = git(repo, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}", check=False)
    commit = resolved.stdout.strip()
    if resolved.returncode != 0 or not commit:
        return None, [unknown]
    found: list[AuditFinding] = []
    for raw in paths:
        try:
            norm = normalize(raw)
        except ValueError:
            continue  # reported by audit_declared_paths
        if norm == WHOLE_REPO:
            continue
        frontier = [""]  # every real directory a folded prefix could name
        for part in norm.split("/"):
            nxt: list[str] = []
            for parent in frontier:
                entries = _tree_entries(repo, commit, parent)
                if entries is None:
                    found.append(AuditFinding("path-unverifiable", "high", raw, None, f"cannot list {parent or '.'} at base"))
                    return commit, found
                for mode, real in entries.get(_key(part), []):
                    where = f"{parent}/{real}" if parent else real
                    if mode in _MODE_RULES:
                        rule, why = _MODE_RULES[mode]
                        found.append(AuditFinding(rule, "high", raw, None, f"{where} {why}"))
                    elif mode == "040000":
                        nxt.append(where)
            frontier = nxt
            if not frontier:
                break
    return commit, found


# ---- dependency changes --------------------------------------------------------------------
#
# Fail closed: a watched file passes only when its change is provably a plain version edit.
# Anything unreadable, unparseable or unexpected is a finding, never a pass and never a crash.

LOCKFILES = frozenset({
    "uv.lock", "poetry.lock", "Pipfile.lock", "pdm.lock", "package-lock.json", "npm-shrinkwrap.json",
    "pnpm-lock.yaml", "yarn.lock", "bun.lockb", "bun.lock", "Cargo.lock", "go.sum", "Gemfile.lock", "composer.lock",
})  # fmt: skip
_LOCKFILES_FOLDED = frozenset(n.casefold() for n in LOCKFILES)
# Files that only configure where dependencies come from, or Python code no line rule can judge: any change flags.
_CONFIG_ONLY = frozenset({
    "uv.toml", "pip.conf", "pip.ini", ".pypirc", ".npmrc", ".yarnrc", ".yarnrc.yml", "pnpm-workspace.yaml",
    ".pnpmfile.cjs", "bunfig.toml", "pipfile", "go.work", "setup.py", "setup.cfg",
})  # fmt: skip  (case-folded names)
_CONFIG_ONLY_PATHS = (".cargo/config.toml", ".cargo/config")
_UNTRUSTED_MODES = {"120000": "a symlink", "160000": "a submodule"}

_Added = list[tuple[int, str]]  # (line number in head, text)


class _DiffError(Exception):
    pass


def _diff_lines(root: Path, base: str, head: str, path: str) -> tuple[_Added, list[str]]:
    """Added lines (head line number, text) and removed lines of one file."""
    res = git_bytes(root, "diff", "-U0", "--no-ext-diff", "--no-textconv", "--no-renames", "--text", base, head, "--", path)
    if res.returncode != 0:
        raise _DiffError(f"git diff failed for {path}")
    out = res.stdout.decode("utf-8", errors="replace")
    added: _Added = []
    removed: list[str] = []
    n, in_hunk = 0, False
    for line in out.split("\n"):
        hunk = re.match(r"@@ -\S+ \+(\d+)(?:,\d+)? @@", line)
        if hunk:
            n, in_hunk = int(hunk.group(1)), True
        elif in_hunk and line.startswith("+"):
            added.append((n, line[1:]))
            n += 1
        elif in_hunk and line.startswith("-"):
            removed.append(line[1:])
    return added, removed


# Every byte-order mark (UTF-8 included): pip and other readers switch decoding on them.
_BOMS = (b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")


def _blob(root: Path, rev: str, path: str) -> bytes | None:
    res = git_bytes(root, "show", f"{rev}:{path}")
    return res.stdout if res.returncode == 0 else None


def _unreadable(data: bytes | None) -> bool:
    """NUL bytes, any BOM or invalid UTF-8: content a text scan cannot be trusted to see."""
    if data is None:
        return False
    if b"\0" in data or data.startswith(_BOMS):
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


_CODING = re.compile(rb"#.*coding[:=]\s*([-\w.]+)")


def _foreign_coding(data: bytes | None) -> str | None:
    """A PEP 263 declaration in the first two lines (pip honours it) naming anything but UTF-8 or ASCII."""
    for line in re.split(rb"\r\n|\r|\n", data or b"")[:2]:
        if m := _CODING.search(line):
            name = m.group(1).decode("ascii", errors="replace")
            try:
                codec = codecs.lookup(name).name
            except LookupError:
                return name
            if codec not in ("utf-8", "ascii"):
                return name
    return None


def _show(root: Path, rev: str, path: str) -> str | None:
    data = _blob(root, rev, path)
    return None if data is None else data.decode("utf-8", errors="replace")


def _flat(value: Any, prefix: tuple[Any, ...] = ()) -> dict[tuple[Any, ...], str]:
    """Leaf values of nested dicts and lists as {key tuple: json text}; tuples cannot collide the way dotted keys do."""
    if value is None and not prefix:
        return {}
    if isinstance(value, dict):
        out: dict[tuple[Any, ...], str] = {}
        for k, v in value.items():
            out.update(_flat(v, (*prefix, str(k))))
        return out
    if isinstance(value, list):
        out = {}
        for i, v in enumerate(value):
            marker = ("item", json.dumps(v, default=str, sort_keys=True)) if not isinstance(v, (dict, list)) else ("index", i)
            out.update(_flat(v, (*prefix, marker)))
        return out
    return {prefix: json.dumps(value, default=str)}


def _changed(base: Any, head: Any) -> dict[tuple[Any, ...], str]:
    """Leaves added or changed at head (removing a dependency is not a source change)."""
    b, h = _flat(base), _flat(head)
    return {k: v for k, v in h.items() if b.get(k) != v}


def _any_change(base: Any, head: Any) -> list[tuple[Any, ...]]:
    """Every leaf added, changed or removed: in a source or config table, removing a key redirects too."""
    b, h = _flat(base), _flat(head)
    return [k for k, v in h.items() if b.get(k) != v] + [k for k in b if k not in h]


def _name(key: tuple[Any, ...]) -> str:
    return ".".join(k if isinstance(k, str) else str(k[1]) for k in key)


def _last(key: tuple[Any, ...]) -> str:
    return key[-1] if key and isinstance(key[-1], str) else ""


def _d(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _dig(data: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        if not isinstance(data, dict) or part not in data:
            return None
        data = data[part]
    return data


HitFn = Callable[[Path, str, str, str], list[str]]  # (root, base, head, path) -> evidence strings


def _parse(text: str | None, loader: Callable[[str], Any]) -> tuple[Any, bool]:
    if text is None:
        return {}, True
    try:
        return loader(text), True
    except Exception:  # noqa: BLE001 - hostile content (deep nesting -> RecursionError included) is unparseable
        return None, False


def _both(root: Path, base: str, head: str, path: str, loader: Callable[[str], Any]) -> tuple[Any, Any] | None:
    b, ok_b = _parse(_show(root, base, path), loader)
    h, ok_h = _parse(_show(root, head, path), loader)
    return (b, h) if ok_b and ok_h else None


def _unparseable(path: str) -> list[str]:
    return [f"{path} cannot be parsed at base or head; dependency changes cannot be verified"]


# ---- plain PEP 508 requirements (allowlist) ----

_PEP_NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
_PEP_SPEC = r"(?:===|==|~=|!=|<=|>=|<|>)\s*[A-Za-z0-9.*+!_-]+"
_PEP_SPECS = rf"{_PEP_SPEC}(?:\s*,\s*{_PEP_SPEC})*"
_REQ_HEAD = re.compile(
    rf"\s*(?P<name>{_PEP_NAME})\s*(?:\[\s*(?:{_PEP_NAME}(?:\s*,\s*{_PEP_NAME})*)?\s*\])?\s*"
    rf"(?:\(\s*{_PEP_SPECS}\s*\)|{_PEP_SPECS})?\s*",
    re.ASCII,
)
_ARCHIVE_EXTS = (".whl", ".zip", ".tar", ".tgz", ".tbz", ".txz", ".tlz", ".gz", ".bz2", ".xz", ".lz", ".lzma")
_MARKER_VARS = frozenset({
    "python_version", "python_full_version", "os_name", "sys_platform", "platform_release", "platform_system",
    "platform_version", "platform_machine", "platform_python_implementation", "implementation_name",
    "implementation_version", "extra", "extras", "dependency_groups",
})  # fmt: skip
_MARKER_TOKEN = re.compile(
    r"\s*(?:(?P<str>'[\w.*+!,<>=~ -]*'|\"[\w.*+!,<>=~ -]*\")|(?P<op>===|==|~=|!=|<=|>=|<|>)"
    r"|(?P<word>not\s+in\b|[A-Za-z_]+)|(?P<paren>[()]))",
    re.ASCII,
)
_MARKER_MAX = 400


def _marker_tokens(text: str) -> list[tuple[str, str]] | None:
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        if text[pos:].strip(" \t") == "":
            break
        m = _MARKER_TOKEN.match(text, pos)
        if not m:
            return None
        kind = m.lastgroup or ""
        value = m.group(kind)
        if kind == "word":
            word = " ".join(value.split())
            if word in ("and", "or", "in", "not in"):
                kind = "op" if word in ("in", "not in") else word
            elif word in _MARKER_VARS:
                kind = "var"
            else:
                return None
            value = word
        tokens.append((kind, value))
        pos = m.end()
    return tokens


def _plain_marker(text: str) -> bool:
    """PEP 508 marker grammar only: known variables, comparison ops, quoted plain literals, and/or, parentheses."""
    if len(text) > _MARKER_MAX:
        return False
    tokens = _marker_tokens(text)
    if not tokens:
        return False
    pos, depth = 0, 0

    def atom() -> bool:
        nonlocal pos, depth
        if pos < len(tokens) and tokens[pos] == ("paren", "("):
            depth += 1
            pos += 1
            if depth > 8 or not expr() or pos >= len(tokens) or tokens[pos] != ("paren", ")"):
                return False
            depth -= 1
            pos += 1
            return True
        if pos + 3 > len(tokens):
            return False
        left, op, right = tokens[pos : pos + 3]
        pos += 3
        return left[0] in ("var", "str") and op[0] == "op" and right[0] in ("var", "str") and "var" in (left[0], right[0])

    def expr() -> bool:
        nonlocal pos
        if not atom():
            return False
        while pos < len(tokens) and tokens[pos][0] in ("and", "or"):
            pos += 1
            if not atom():
                return False
        return True

    return expr() and pos == len(tokens)


def _plain_requirement(text: str) -> bool:
    """A bare PEP 508 requirement: name, extras, version specifiers and a strict marker; no URL, path or option."""
    if "\n" in text or len(text) > 1000:
        return False
    head, sep, marker = text.partition(";")
    m = _REQ_HEAD.fullmatch(head)
    if not m or m.group("name").casefold().endswith(_ARCHIVE_EXTS):  # pip installs `x-1.0.tar.gz` as a local file
        return False
    return not sep or _plain_marker(marker)


# ---- per-manifest rules ----

_PLAIN_TEXT = re.compile(r"^[\w\s.*^~<>=!|,+;()'\"\[\]-]*$", re.ASCII)  # no ':' '/' '@' or backslash
_BENIGN_DEP_KEYS = frozenset({"version", "features", "optional", "default-features", "default_features", "python",
                              "markers", "extras", "allow-prereleases"})  # fmt: skip


def _dep_leaf_is_plain(key: tuple[Any, ...], leaf: str) -> bool:
    """True when a changed dependency-table leaf is provably a version-spec edit (Cargo and Poetry shapes)."""
    value = json.loads(leaf)
    names = [k for k in key if isinstance(k, str)]
    if isinstance(value, bool):
        return len(key) > 1 and names[-1] in _BENIGN_DEP_KEYS
    if not isinstance(value, str) or not _PLAIN_TEXT.match(value):
        return False
    return len(key) == 1 or (bool(names) and names[-1] in _BENIGN_DEP_KEYS)


_PLAIN_RANGE = re.compile(r"^[\s0-9A-Za-z.*^~<>=|,+-]*$")


def _json_leaf_source(leaf: str) -> bool:
    """Anything in a package.json dependency slot that is not a plain semver range or dist-tag is a source."""
    value = json.loads(leaf)
    if not isinstance(value, str):
        return True  # fail closed: a dependency slot holding a non-string is not a version edit
    if value.startswith(("workspace:", "catalog:")):
        return False
    return not _PLAIN_RANGE.match(value) or set(value.strip()) == {"."}


_JS_SECTIONS = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies",
                "resolutions", "overrides", "pnpm.overrides")  # fmt: skip


def _package_json_hits(root: Path, base: str, head: str, path: str) -> list[str]:
    pair = _both(root, base, head, path, json.loads)
    if pair is None:
        return _unparseable(path)
    b, h = pair
    hits = []
    for section in _JS_SECTIONS:
        for key, leaf in _changed(_dig(b, section), _dig(h, section)).items():
            if _json_leaf_source(leaf):
                hits.append(f"{section}.{_name(key)} -> {json.loads(leaf)}")
    return hits


# pyproject: only these requirement lists and Poetry dependency tables take plain version edits; project metadata
# and the listed linters/formatters are free; every other change (tool.uv, tool.poetry, tool.pdm, build-system,
# any other tool, unknown top-level keys) flags.
_REQ_LISTS = ("project.dependencies", "project.optional-dependencies", "dependency-groups")
_HARMLESS_TOOLS = frozenset({
    "ruff", "black", "isort", "mypy", "pytest", "coverage", "pyright", "basedpyright", "pylint", "flake8",
    "codespell", "bandit", "pydocstyle", "docformatter", "interrogate", "vulture", "towncrier",
})  # fmt: skip


def _poetry_tables(data: Any) -> dict[str, Any]:
    poetry = _d(_dig(data, "tool.poetry"))
    tables = {"tool.poetry.dependencies": poetry.get("dependencies"), "tool.poetry.dev-dependencies": poetry.get("dev-dependencies")}
    for name, group in _d(poetry.get("group")).items():
        tables[f"tool.poetry.group.{name}.dependencies"] = _d(group).get("dependencies")
    return tables


def _guarded(data: Any) -> dict[str, Any]:
    """The pyproject parts where any change flags: everything except the slots judged item by item and the free parts."""
    data = _d(data)
    out = {k: v for k, v in data.items() if k not in ("project", "dependency-groups", "tool")}
    for k in ("project", "dependency-groups", "tool"):
        if k in data and not isinstance(data[k], dict):
            out[k] = data[k]
    tool = {k: v for k, v in _d(data.get("tool")).items() if k not in _HARMLESS_TOOLS}
    if isinstance(tool.get("poetry"), dict):
        poetry = {k: v for k, v in tool["poetry"].items() if k not in ("dependencies", "dev-dependencies")}
        if isinstance(poetry.get("group"), dict):
            poetry["group"] = {
                n: {k: v for k, v in g.items() if k != "dependencies"} if isinstance(g, dict) else g
                for n, g in poetry["group"].items()
            }
        tool["poetry"] = poetry
    out["tool"] = tool
    return out


def _dep_table_hits(base_tables: dict[str, Any], head_tables: dict[str, Any]) -> list[str]:
    """Cargo/Poetry dependency tables: changed leaves must be plain version edits; dropping a non-version key from a
    dependency that is kept (`source`, `registry`, `git`) moves where it resolves from, so that flags too.

    Removing a whole dependency is not a source change. A dependency rewritten from `foo = "1"` to an inline table
    loses its one-element key, but every leaf of the new table is judged by `_changed`, so that removal is silent.
    A table that is not a mapping at all (`dependencies = "x"`) has an empty key and always flags.
    """
    hits = []
    for sub in dict.fromkeys([*base_tables, *head_tables]):
        b, h = base_tables.get(sub), head_tables.get(sub)
        hits += [f"{sub}.{_name(k)} changed" for k, v in _changed(b, h).items() if not _dep_leaf_is_plain(k, v)]
        head_flat = _flat(h)
        kept = {k[0] for k in head_flat if k}
        for k in _flat(b):
            if k in head_flat:
                continue
            names = [n for n in k if isinstance(n, str)]
            dropped_subkey = len(k) > 1 and k[0] in kept and not (len(names) > 1 and names[-1] in _BENIGN_DEP_KEYS)
            if not k or dropped_subkey:
                hits.append(f"{sub}.{_name(k)} removed")
    return hits


def _toml_hits(b: Any, h: Any) -> list[str]:
    hits = []
    for sub in _REQ_LISTS:
        for k, v in _changed(_dig(b, sub), _dig(h, sub)).items():
            value = json.loads(v)
            if _last(k) == "include-group" and isinstance(value, str) and re.fullmatch(_PEP_NAME, value):
                continue
            if not (isinstance(value, str) and _plain_requirement(value)):
                hits.append(f"{sub}: {value}")
    hits += _dep_table_hits(_poetry_tables(b), _poetry_tables(h))
    hits += [f"{_name(k)} changed" for k in _any_change(_guarded(b), _guarded(h))]
    return hits


def _pyproject_hits(root: Path, base: str, head: str, path: str) -> list[str]:
    if _blob(root, head, path) is None:
        return []  # deleted: nothing left to install from
    pair = _both(root, base, head, path, tomllib.loads)
    return _unparseable(path) if pair is None else _toml_hits(*pair)


def _cargo_dep_tables(data: Any) -> dict[str, Any]:
    kinds = ("dependencies", "dev-dependencies", "build-dependencies")
    tables = {k: data.get(k) for k in kinds}
    tables["workspace.dependencies"] = _dig(data, "workspace.dependencies")
    for tgt, body in _d(data.get("target")).items():
        for k in kinds:
            tables[f"target.{tgt}.{k}"] = _d(body).get(k)
    return tables


def _cargo_hits(root: Path, base: str, head: str, path: str) -> list[str]:
    pair = _both(root, base, head, path, tomllib.loads)
    if pair is None:
        return _unparseable(path)
    b, h = pair
    hits = []
    for sub in ("patch", "source", "registries", "replace"):
        hits += [f"{sub}.{_name(k)} changed" for k in _any_change(b.get(sub), h.get(sub))]
    return hits + _dep_table_hits(_cargo_dep_tables(b), _cargo_dep_tables(h))


def _go_directives(text: str | None) -> set[tuple[str, str]]:
    """(verb, rest) for every directive, with `verb ( ... )` blocks expanded."""
    found: set[tuple[str, str]] = set()
    block = ""
    for raw in (text or "").split("\n"):
        line = " ".join(raw.split("//", 1)[0].split())
        if not line:
            continue
        if block:
            if line == ")":
                block = ""
            else:
                found.add((block, line))
        elif m := re.match(r"(\w+)\s*\($", line):
            block = m.group(1)
        else:
            verb, _, rest = line.partition(" ")
            found.add((verb, rest))
    return found


_GO_PLAIN = re.compile(r"^(?:go\s+[\d.]+|toolchain\s+go[\w.-]+|[\w./~-]+\s+v[\w.+-]+(?:\s*//.*)?)$", re.ASCII)


def _go_mod_hits(root: Path, base: str, head: str, path: str) -> list[str]:
    b, h = _show(root, base, path), _show(root, head, path)
    hits = []
    for verb, rest in sorted(_go_directives(h) - _go_directives(b)):
        plain = (verb in ("go", "toolchain") and _GO_PLAIN.match(f"{verb} {rest}")) or (
            verb == "require" and re.match(r"^[\w./~-]+\s+v[\w.+-]+$", rest)
        )
        if not plain:
            hits.append(f"{verb} {rest}")
    return hits


_SEPARATORS = re.compile("[\r\x0b\x0c\x1c-\x1e\x85  ]")
_REQ_COMMENT = re.compile(r"(?:^|\s+)#.*$", re.S | re.ASCII)  # pip's comment rule


def _requirement_line_ok(text: str) -> bool:
    """Allowlist: blank, a comment, or one plain PEP 508 requirement (an optional trailing comment)."""
    if _SEPARATORS.search(text):
        return False
    line = _REQ_COMMENT.sub("", text).strip()
    return not line or _plain_requirement(line)


def _is_req_comment(line: str) -> bool:
    return re.match(r"[ \t\f\v]*#", line) is not None  # pip's COMMENT_RE.match: a comment never continues


def _pip_logical_lines(lines: list[str]) -> list[tuple[str, set[int]]]:
    """pip's join_lines: a trailing backslash joins the next physical line (changed or not); a comment line ends it."""
    out: list[tuple[str, set[int]]] = []
    parts: list[str] = []
    nums: set[int] = set()
    for n, line in enumerate(lines, start=1):
        if not line.endswith("\\") or _is_req_comment(line):
            if _is_req_comment(line):
                line = " " + line
            out.append(("".join([*parts, line]), {*nums, n}))
            parts, nums = [], set()
        else:
            parts.append(line.strip("\\"))
            nums.add(n)
    if parts:
        out.append(("".join(parts), nums))
    return out


def _requirements_hits(root: Path, base: str, head: str, path: str) -> list[str]:
    """Allowlist over every pip logical line that holds an added line, every added line alone, and every removed line;
    an added continuation and any non-\\n line separator in the file flag outright."""
    added, removed = _diff_lines(root, base, head, path)
    hits: list[str] = []
    text = _show(root, head, path)
    if text is not None:
        if _SEPARATORS.search(text):
            return [f"{path} has a line separator other than \\n; pip would split its lines differently"]
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        added_at = {n for n, _ in added}
        hits += [line for line, nums in _pip_logical_lines(lines) if nums & added_at and not _requirement_line_ok(line)]
    for _, line in added:
        if not _requirement_line_ok(line.removesuffix("\\")):
            hits.append(line)
        elif line.endswith("\\") and not _is_req_comment(line):
            hits.append(f"{line} (continues onto the next line)")
    hits += [f"removed: {line}" for line in removed if not _requirement_line_ok(line.removesuffix("\\"))]
    return [h.strip() for h in dict.fromkeys(hits)]


_MANIFESTS: dict[str, HitFn] = {
    "pyproject.toml": _pyproject_hits,
    "package.json": _package_json_hits,
    "cargo.toml": _cargo_hits,
    "go.mod": _go_mod_hits,
}


def _is_requirements_file(path: str) -> bool:
    leaf = path.rsplit("/", 1)[-1]
    return (
        (leaf.startswith("requirements") and leaf.endswith(".txt"))
        or leaf.endswith("-requirements.txt")
        or (leaf.startswith("constraints") and leaf.endswith(".txt"))
        or ("requirements" in path.split("/")[:-1] and leaf.endswith(".txt"))
    )


def _watched(path: str) -> str | None:
    """What kind of dependency file a path is (names compared case-folded), or None."""
    folded = path.casefold()
    leaf = folded.rsplit("/", 1)[-1]
    if leaf in _LOCKFILES_FOLDED:
        return "lockfile"
    if leaf == ".gitmodules":
        return "gitmodules"
    if leaf in _CONFIG_ONLY or any(folded == n or folded.endswith("/" + n) for n in _CONFIG_ONLY_PATHS):
        return "config"
    if _is_requirements_file(folded):
        return "requirements"
    return leaf if leaf in _MANIFESTS else None


def _unverifiable(where: str, why: str) -> AuditFinding:
    return AuditFinding("dep-unverifiable", "high", where, None, why)


def _audit_path(root: Path, base: str, head: str, path: str, new_mode: str) -> list[AuditFinding]:
    kind = _watched(path)
    if kind is None:
        return []
    if kind == "lockfile":
        return [AuditFinding("dep-lockfile", "high", path, None, "lockfile changed")]
    if kind == "gitmodules":
        return [AuditFinding("gitmodules", "high", path, None, ".gitmodules changed")]
    if new_mode in _UNTRUSTED_MODES:
        return [AuditFinding("dep-source", "high", path, None, f"is now {_UNTRUSTED_MODES[new_mode]}; content cannot be verified")]
    if kind == "config":
        return [AuditFinding("dep-source", "high", path, None, "source-redirecting or executable config changed")]
    b, h = _blob(root, base, path), _blob(root, head, path)
    if b is None and h is None:
        return [_unverifiable(path, "cannot be read at base or head")]
    if _unreadable(h) or _unreadable(b):
        return [AuditFinding("dep-source", "high", path, None, "binary, NUL, BOM or non-UTF-8 content; cannot be scanned")]
    decide: HitFn
    if kind == "requirements":
        if coding := (_foreign_coding(b) or _foreign_coding(h)):
            return [AuditFinding("dep-source", "high", path, None, f"declares coding {coding!r}; cannot be scanned as UTF-8")]
        decide = _requirements_hits
    else:
        decide = _MANIFESTS[kind]
    try:
        hits = decide(root, base, head, path)
    except _DiffError as exc:
        hits = [f"{exc}; dependency changes cannot be verified"]
    return [AuditFinding("dep-source", "high", path, None, _evidence(t)) for t in hits]


def _real(path: str, links: dict[str, str]) -> str | None:
    """The in-tree path `path` names, following symlinks one component at a time as the OS does (`..` after a link
    climbs from the link's target). None when it leaves the tree, is absolute, or loops."""
    queue = path.split("/")
    done: list[str] = []
    hops = 0
    while queue:
        part = queue.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            if not done:
                return None
            done.pop()
            continue
        done.append(part)
        target = links.get("/".join(done))
        if target is not None:
            hops += 1
            if hops > 40 or target.startswith("/"):
                return None
            done.pop()
            queue = target.split("/") + queue
    return "/".join(done)


def _may_hold_dependency_file(link: str) -> bool:
    """The link's own name, or a directory name that makes its children dependency files (`requirements/`, `.cargo/`)."""
    return any(_watched(p) for p in (link, f"{link}/x.txt", f"{link}/config"))


def _symlink_alias_findings(root: Path, head: str, changed: set[str]) -> list[AuditFinding]:
    """A symlink at head can give a dependency-file name to content stored under another name (`requirements.txt ->
    deps.list`, `requirements/ -> vendor/`). Flag when such an alias reaches changed content, or the link itself changed."""
    res = git_bytes(root, "ls-tree", "-r", "-z", "--full-tree", head)
    if res.returncode != 0:
        return [_unverifiable("git ls-tree", "cannot list the head tree to check symlinks")]
    tree: list[str] = []
    link_blobs: dict[str, str] = {}
    for rec in filter(None, res.stdout.split(b"\0")):
        meta, _, raw = rec.partition(b"\t")
        path = raw.decode("utf-8", errors="surrogateescape")
        tree.append(path)
        mode, _, rest = meta.decode("ascii", errors="replace").partition(" ")
        if mode == "120000":
            link_blobs[path] = rest.rsplit(" ", 1)[-1]
    if not link_blobs:
        return []
    links: dict[str, str] = {}
    for path, sha in link_blobs.items():
        blob = git_bytes(root, "cat-file", "blob", sha)
        if blob.returncode != 0:
            return [_unverifiable(path, "cannot read symlink target at head")]
        links[path] = blob.stdout.decode("utf-8", errors="surrogateescape")
    found: list[AuditFinding] = []
    for link in links:
        target = _real(link, links)
        if target is None:
            if link in changed or _may_hold_dependency_file(link):
                found.append(AuditFinding("dep-source", "high", link, None, f"symlink {link} leaves the tree or loops"))
            continue
        reach = tree if link in changed else changed
        for p in reach:
            if target == "" or p == target or p.startswith(target + "/"):
                alias = f"{link}/{p}" if target == "" else link + p[len(target) :]
                kind = _watched(alias)
                if alias != p and kind and (link in changed or kind != _watched(p)):  # same kind: p is judged itself
                    found.append(AuditFinding("dep-source", "high", link, None, f"symlink {link}: {alias} resolves to {p}"))
                    break
    return found


def _catalog_findings(root: Path, head: str) -> list[AuditFinding]:
    """A changed pnpm-workspace.yaml re-points every `catalog:` dependency, changed or not."""
    res = git_bytes(root, "ls-tree", "-r", "-z", "--name-only", "--full-tree", head)
    if res.returncode != 0:
        return [_unverifiable("package.json", "cannot list the head tree to check catalog: references")]
    found = []
    for raw in filter(None, res.stdout.split(b"\0")):
        path = raw.decode("utf-8", errors="surrogateescape")
        if path.casefold().rsplit("/", 1)[-1] != "package.json":
            continue
        data, ok = _parse(_show(root, head, path), json.loads)
        values = [json.loads(v) for sec in _JS_SECTIONS for v in _flat(_dig(data, sec)).values()]
        if not ok or any(isinstance(v, str) and v.startswith("catalog:") for v in values):
            found.append(_unverifiable(path, "uses catalog: entries while pnpm-workspace.yaml changed"))
    return found


def _raw_changes(out: bytes) -> list[tuple[str, bytes]]:
    """(new mode, raw path) per record of `git diff --raw -z --no-renames`."""
    fields = out.split(b"\0")
    changes: list[tuple[str, bytes]] = []
    i = 0
    while i < len(fields) and fields[i]:
        meta = fields[i].decode("ascii")
        if not meta.startswith(":") or i + 1 >= len(fields):
            raise ValueError("unexpected git diff --raw output")
        changes.append((meta[1:].split(" ")[1], fields[i + 1]))
        i += 2
    return changes


def _audit_dependency_changes(root: Path, base: str, head: str) -> list[AuditFinding]:
    listing = git_bytes(root, "diff", "--raw", "-z", "--no-renames", "--no-abbrev", base, head, "--")
    if listing.returncode != 0:
        return [AuditFinding("dep-source", "high", "git diff", None, "git diff failed; dependency changes cannot be verified")]
    found: list[AuditFinding] = []
    changed: set[str] = set()
    for new_mode, raw in _raw_changes(listing.stdout):
        try:
            path = raw.decode("utf-8")
        except UnicodeDecodeError:
            shown = raw.decode("utf-8", errors="backslashreplace")
            found.append(_unverifiable(shown, "path name is not valid UTF-8; cannot be verified"))
            continue
        changed.add(path)
        try:
            found.extend(_audit_path(root, base, head, path, new_mode))
        except Exception as exc:  # noqa: BLE001 - hostile content must refuse, never crash or pass
            found.append(_unverifiable(path, f"audit failed ({type(exc).__name__}); dependency changes cannot be verified"))
    try:
        if any(p.casefold().rsplit("/", 1)[-1] == "pnpm-workspace.yaml" for p in changed):
            found.extend(_catalog_findings(root, head))
    except Exception as exc:  # noqa: BLE001
        found.append(_unverifiable("package.json", f"catalog check failed ({type(exc).__name__})"))
    try:
        found.extend(_symlink_alias_findings(root, head, changed))
    except Exception as exc:  # noqa: BLE001
        found.append(_unverifiable("symlinks", f"symlink check failed ({type(exc).__name__})"))
    return found


def audit_dependency_changes(root: Path, base: str, head: str) -> list[AuditFinding]:
    for rev in (base, head):
        if not rev or rev.startswith("-"):
            return [AuditFinding("base-unknown", "high", rev, None, f"revision {rev!r} is not a usable commit")]
    try:
        return _audit_dependency_changes(root, base, head)
    except Exception as exc:  # noqa: BLE001 - never raise on hostile repo content
        return [_unverifiable("git diff", f"dependency audit failed ({type(exc).__name__})")]
