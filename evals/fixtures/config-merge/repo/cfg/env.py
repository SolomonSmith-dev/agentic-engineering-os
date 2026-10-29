"""APP_DB__PORT=6543 becomes {"db": {"port": "6543"}}."""

from typing import Any, Mapping

PREFIX = "APP_"


def from_env(environ: Mapping[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in environ.items():
        if not key.startswith(PREFIX):
            continue
        path = key[len(PREFIX):].lower().split("__")
        node = out
        for part in path[:-1]:
            node = node.setdefault(part, {})
        node[path[-1]] = value
    return out
