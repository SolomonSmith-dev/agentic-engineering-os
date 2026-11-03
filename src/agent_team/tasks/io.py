"""Task-file I/O: load, splice the `result:` block, atomic write-back.

Pure and reusable: the one shared finish path calls `write_result` / `write_sidecar`.
Only the top-level `result:` block is ever replaced; every other byte is kept.
"""

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from agent_team.schemas.task_file import TaskFile, TaskResult

RESULT_KEY = re.compile(r"^result[ \t]*:(?=[ \t\r\n]|$)")


class TaskFileError(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class LoadedTaskFile:
    path: Path
    text: str
    task: TaskFile


def _read_text(path: Path) -> str:
    # bytes, not read_text: universal newlines would hide CRLF.
    return path.read_bytes().decode("utf-8")


def load_task_file(path: Path) -> LoadedTaskFile:
    try:
        text = _read_text(path)
    except (OSError, UnicodeDecodeError) as exc:
        raise TaskFileError([f"file: cannot read {path}: {exc}"]) from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise TaskFileError([f"yaml: {exc}".replace("\n", " ")]) from exc
    if not isinstance(data, dict):
        raise TaskFileError(["file: top level must be a mapping"])
    try:
        task = TaskFile.model_validate(data)
    except ValidationError as exc:
        raise TaskFileError(
            [f"{'.'.join(str(p) for p in e['loc']) or 'file'}: {e['msg']}" for e in exc.errors()]
        ) from exc
    if task.id != path.stem:
        raise TaskFileError([f"id: {task.id!r} does not match file name {path.stem!r}"])
    return LoadedTaskFile(path=path, text=text, task=task)


def _scan_flow_line(
    s: str,
    stack: list[str],
    in_single: bool,
    in_double: bool,
    escape: bool,
) -> tuple[bool, bool, bool, bool, bool]:
    i = 0
    while i < len(s):
        ch = s[i]
        if in_double:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_double = False
        elif in_single:
            if ch == "'":
                if i + 1 < len(s) and s[i + 1] == "'":
                    i += 1
                else:
                    in_single = False
        else:
            if ch == "#":
                break
            elif ch == '"':
                in_double = True
            elif ch == "'":
                in_single = True
            elif ch in ("{", "["):
                stack.append("}" if ch == "{" else "]")
            elif ch in ("}", "]"):
                if not stack or stack.pop() != ch:
                    return False, False, in_single, in_double, escape
                if not stack:
                    return True, True, in_single, in_double, escape
        i += 1
    return False, True, in_single, in_double, escape


def find_result_block(text: str) -> tuple[int, int] | None:
    # A column-zero continuation inside a quoted scalar is not a mapping key.
    # Let the YAML parser identify the real top-level key before scanning its bytes.
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return None
    if not isinstance(root, yaml.MappingNode) or root.flow_style:
        return None
    keys = [key for key, _ in root.value if isinstance(key, yaml.ScalarNode) and key.value == "result"]
    if len(keys) != 1 or keys[0].start_mark.column != 0:
        return None
    key_offset = keys[0].start_mark.index
    start = end = None
    last_field_end = None
    offset = 0
    lines = text.splitlines(keepends=True)
    for idx, line in enumerate(lines):
        if start is None:
            if offset == key_offset and (m := RESULT_KEY.match(line)):
                start = offset
                last_field_end = offset + len(line)
                val = line[m.end() :].lstrip(" \t")
                if val.startswith(("{", "[")):
                    stack: list[str] = []
                    closed, ok, in_single, in_double, escape = _scan_flow_line(
                        val, stack, False, False, False
                    )
                    if not ok:
                        return None
                    if not closed:
                        curr_offset = offset + len(line)
                        for next_line in lines[idx + 1 :]:
                            closed, ok, in_single, in_double, escape = _scan_flow_line(
                                next_line, stack, in_single, in_double, escape
                            )
                            if not ok:
                                return None
                            if closed:
                                end = curr_offset + len(next_line)
                                break
                            curr_offset += len(next_line)
                        if not closed:
                            return None
                        while end > start and text[:end].endswith(("\n\n", "\r\n\r\n")):
                            end -= 2 if text[:end].endswith("\r\n\r\n") else 1
                        return start, end
        elif line[:1] not in (" ", "\t", "\r", "\n", "#"):  # any column-0 line other than # ends it: key, ---, ...
            end = offset
            break
        else:
            if line[:1] in (" ", "\t") and line.strip() and not line.strip().startswith("#"):
                last_field_end = offset + len(line)
        offset += len(line)
    if start is None:
        return None
    end = last_field_end if last_field_end is not None else (offset if end is None else end)
    while end > start and text[:end].endswith(("\n\n", "\r\n\r\n")):  # trailing blank lines stay outside
        end -= 2 if text[:end].endswith("\r\n\r\n") else 1
    return start, end


def without_result(text: str) -> str:
    span = find_result_block(text)
    return text if span is None else text[: span[0]] + text[span[1] :]


def render_result(result: TaskResult, newline: str = "\n") -> str:
    dumped = yaml.safe_dump(
        {"result": result.model_dump(mode="json")}, sort_keys=False, default_flow_style=False, allow_unicode=True
    )
    return dumped.replace("\n", newline)


def splice_result(text: str, result: TaskResult) -> str:
    newline = "\r\n" if "\r\n" in text else "\n"
    block = render_result(result, newline)
    span = find_result_block(text)
    if span is not None:
        return text[: span[0]] + block + text[span[1] :]
    parsed = yaml.safe_load(text)
    if not isinstance(parsed, dict) or "result" in parsed:
        raise TaskFileError(["yaml: cannot safely locate the result entry"])
    # Keep an explicit end marker (and any trailing comments) after the new entry.
    insertion = len(text)
    try:
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.DocumentEndEvent) and event.explicit:
                insertion = event.start_mark.index
                break
    except yaml.YAMLError as exc:
        raise TaskFileError(["yaml: cannot safely append a result"]) from exc
    prefix, suffix = text[:insertion], text[insertion:]
    if prefix and not prefix.endswith("\n"):
        prefix += newline
    candidate = prefix + block + suffix
    # Unsupported mapping shapes must never silently overwrite request metadata.
    before, after = yaml.safe_load(text), yaml.safe_load(candidate)
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise TaskFileError(["yaml: result requires a top-level mapping"])
    if {k: v for k, v in before.items() if k != "result"} != {k: v for k, v in after.items() if k != "result"}:
        raise TaskFileError(["yaml: result splice changed task metadata"])
    return candidate


def sidecar_path(path: Path, task_id: str) -> Path:
    return path.with_name(f"{task_id}.result.yaml")


def _atomic_write(path: Path, text: str, mode_from: Path | None = None) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        if mode_from is not None:
            os.chmod(tmp, mode_from.stat().st_mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def write_sidecar(path: Path, task_id: str, result: TaskResult) -> Path:
    target = sidecar_path(path, task_id)
    _atomic_write(target, f"kind: task-result\nid: {task_id}\n" + render_result(result))
    return target


def write_result(loaded: LoadedTaskFile, result: TaskResult) -> Path:
    path = loaded.path
    try:
        current = _read_text(path)
    except (OSError, UnicodeDecodeError):
        return write_sidecar(path, loaded.task.id, result)
    if without_result(current) != without_result(loaded.text):
        return write_sidecar(path, loaded.task.id, result)
    try:
        updated = splice_result(current, result)
    except (TaskFileError, yaml.YAMLError):
        return write_sidecar(path, loaded.task.id, result)
    _atomic_write(path, updated, mode_from=path)
    return path
