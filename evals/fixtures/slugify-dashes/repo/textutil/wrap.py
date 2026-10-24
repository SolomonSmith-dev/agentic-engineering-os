"""Wrap text at a width."""

import textwrap


def wrap(text: str, width: int = 72) -> list[str]:
    return textwrap.wrap(text, width=width)
