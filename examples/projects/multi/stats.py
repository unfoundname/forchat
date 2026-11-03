"""Minimal statistics helpers (standard library only)."""


def _check(xs):
    values = list(xs)
    if not values:
        raise ValueError("empty sequence")
    return values


def mean(xs):
    """Return the arithmetic mean of ``xs``.

    Raises ValueError for an empty sequence.
    """
    values = _check(xs)
    return sum(values) / len(values)


def median(xs):
    """Return the median of ``xs`` (averaging the two middle values when the
    number of values is even).

    Raises ValueError for an empty sequence.
    """
    values = sorted(_check(xs))
    n = len(values)
    mid = n // 2
    if n % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2
