"""The counts."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Counts:
    lines: int
    words: int
    chars: int


def count(text: str) -> Counts:
    return Counts(lines=text.count("\n"), words=len(text.split()), chars=len(text))
