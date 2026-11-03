"""Scratch self-check for stats.mean (not part of the deliverable)."""

import math

import stats

# 1) basic list of ints -> float 2.0
m = stats.mean([1, 2, 3])
assert m == 2.0, m
assert isinstance(m, float), type(m)

# 2) single-element tuple of floats
m = stats.mean((2.0,))
assert m == 2.0, m
assert isinstance(m, float), type(m)

# 3) empty list -> ValueError (and definitely not ZeroDivisionError)
try:
    stats.mean([])
except ValueError as exc:
    print("empty list -> ValueError:", exc)
else:
    raise AssertionError("mean([]) did not raise ValueError")

# 4) generator input (iterable, single pass)
assert stats.mean(x for x in (1, 2, 3, 4)) == 2.5

# 5) empty tuple and empty generator both raise ValueError
for empty in ((), (x for x in ())):
    try:
        stats.mean(empty)
    except ValueError:
        pass
    else:
        raise AssertionError("empty sequence did not raise ValueError")

# 6) float accumulation / mixed types
assert stats.mean([1, 2.0, 3]) == 2.0
assert math.isclose(stats.mean([0.1, 0.2]), 0.15000000000000002, rel_tol=1e-12)

# 7) signature sanity: exactly one required positional parameter named values
import inspect

params = list(inspect.signature(stats.mean).parameters.values())
assert params[0].name == "values", params
assert params[0].default is inspect.Parameter.empty, params

print("ALL PASS")
