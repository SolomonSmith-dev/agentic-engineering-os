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


def find_result_block(text: str) -> tuple[int, int] | None:
    start = end = None
    offset = 0
    for line in text.splitlines(keepends=True):
        if start is None:
            if RESULT_KEY.match(line):
                start = offset
        elif line[:1] not in (" ", "\t", "\r", "\n"):  # any column-0 line ends it: key, comment, ---, ...
            end = offset
            break
        offset += len(line)
    if start is None:
        return None
    end = offset if end is None else end
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
    if text and not text.endswith("\n"):
        text += newline
    return text + block


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
    _atomic_write(path, splice_result(current, result), mode_from=path)
    return path
