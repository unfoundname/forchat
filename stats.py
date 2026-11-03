"""Tiny statistics helpers, standard library only.

Public API contract (must stay stable, other modules depend on it):

* ``mean(values)`` -> ``float``
  Accepts any iterable of numbers (list, tuple, generator, ...) and returns the
  arithmetic mean as a ``float``.
* An empty sequence raises :class:`ValueError` (never ``ZeroDivisionError``).
"""

from typing import Iterable

__all__ = ["mean"]


def mean(values: Iterable[float]) -> float:
    """Return the arithmetic mean of ``values`` as a ``float``.

    Args:
        values: An iterable (list, tuple, generator, ...) of numbers.

    Returns:
        The arithmetic mean of the values, always as a ``float``
        (``mean([1, 2, 3]) == 2.0``).

    Raises:
        ValueError: If ``values`` contains no elements.

    Examples:
        >>> mean([1, 2, 3])
        2.0
        >>> mean((2.0,))
        2.0
    """
    total = 0.0
    count = 0
    for value in values:
        total += value
        count += 1

    if count == 0:
        raise ValueError("mean() requires at least one value, got an empty sequence")

    return total / count
