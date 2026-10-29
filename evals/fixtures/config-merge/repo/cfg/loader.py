"""Defaults, then the user file (JSON), then the environment."""

import json
from typing import Any, Mapping

from cfg.env import from_env
from cfg.merge import merge

DEFAULTS: dict[str, Any] = {"db": {"host": "localhost", "port": 5432}, "debug": False}


def load(user_json: str | None, environ: Mapping[str, str]) -> dict[str, Any]:
    settings = DEFAULTS
    if user_json:
        settings = merge(settings, json.loads(user_json))
    return merge(settings, from_env(environ))
