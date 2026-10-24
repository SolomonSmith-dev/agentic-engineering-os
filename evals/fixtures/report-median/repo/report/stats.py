"""Summary statistics."""


def mean(values: list[float]) -> float:
    if not values:
        raise ValueError("mean of no values")
    return sum(values) / len(values)


def median(values: list[float]) -> float:
    if not values:
        raise ValueError("median of no values")
    ordered = sorted(values)
    return ordered[len(ordered) // 2]
