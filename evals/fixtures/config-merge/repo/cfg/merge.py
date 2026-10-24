"""Layering settings."""

from typing import Any


def merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """`override` on top of `base`; neither argument is modified."""
    merged = dict(base)
    merged.update(override)
    return merged
