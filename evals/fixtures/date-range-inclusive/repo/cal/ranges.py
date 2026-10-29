"""Date ranges."""

from datetime import date, timedelta


def days_in_range(start: date, end: date) -> list[date]:
    """Every day from `start` to `end`, both included. Empty when `end` is before `start`."""
    days = []
    current = start
    while current < end:
        days.append(current)
        current += timedelta(days=1)
    return days


def nights(check_in: date, check_out: date) -> int:
    """Nights in a stay: check-out day is not a night."""
    return max((check_out - check_in).days, 0)
