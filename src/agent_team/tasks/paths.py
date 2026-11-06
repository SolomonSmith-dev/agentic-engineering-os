"""Scope path helpers: normalize a path, test overlap, test containment.

A task's write scope is a list of repo-relative paths. Paths are compared as
normalized POSIX strings; `.` means the whole repo.
"""

import re

WHOLE_REPO = "."
_DRIVE = re.compile(r"^[A-Za-z]:")


def normalize(path: str) -> str:
    """Strip `./` prefixes, trailing and doubled `/`; empty or `.` is the whole repo.

    Raises ValueError for absolute paths, `~` paths, Windows drive paths (`C:x`), `..` components, backslashes and NUL.
    """
    if "\x00" in path:
        raise ValueError(f"scope path {path!r} contains a NUL byte")
    if "\\" in path:
        raise ValueError(f"scope path {path!r} contains a backslash; use /")
    if path.startswith("/"):
        raise ValueError(f"scope path {path!r} is absolute; use a repo-relative path")
    if _DRIVE.match(path):
        raise ValueError(f"scope path {path!r} starts with a drive letter; use a repo-relative path")
    if path.startswith("~"):
        raise ValueError(f"scope path {path!r} starts with ~; use a repo-relative path")
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise ValueError(f"scope path {path!r} has a .. component")
    return "/".join(parts) or WHOLE_REPO


def overlaps(a: str, b: str) -> bool:
    """True when normalized paths are equal or one contains the other; `.` overlaps everything."""
    if a == WHOLE_REPO or b == WHOLE_REPO or a == b:
        return True
    return a.startswith(b + "/") or b.startswith(a + "/")


def within(file: str, scope: str) -> bool:
    """True when `file` is `scope` or sits under it; scope `.` contains everything."""
    return scope == WHOLE_REPO or file == scope or file.startswith(scope + "/")
