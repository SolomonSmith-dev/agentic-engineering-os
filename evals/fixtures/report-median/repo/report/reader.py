"""CSV in, columns of floats out."""

import csv
from typing import IO


def read_columns(stream: IO[str]) -> dict[str, list[float]]:
    rows = list(csv.DictReader(stream))
    columns: dict[str, list[float]] = {}
    for row in rows:
        for name, value in row.items():
            if value is None or value.strip() == "":
                continue
            columns.setdefault(name, []).append(float(value))
    return columns
