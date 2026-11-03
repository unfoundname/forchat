"""Tests for stats.py, runnable directly: python test_stats.py"""

import stats


def test_mean_basic():
    assert stats.mean([1, 2, 3]) == 2
    assert stats.mean([2, 4]) == 3
    assert stats.mean([1.5, 2.5, 3.5]) == 2.5


def test_median_odd():
    assert stats.median([3, 1, 2]) == 2
    assert stats.median([5]) == 5
    assert stats.median([7, 1, 3, 5, 9]) == 5


def test_median_even():
    assert stats.median([1, 2, 3, 4]) == 2.5
    assert stats.median([4, 2]) == 3
    assert stats.median([1, 2, 3, 4, 5, 6]) == 3.5


def test_empty_raises():
    for func in (stats.mean, stats.median):
        try:
            func([])
        except ValueError:
            pass
        else:
            raise AssertionError("%s([]) should raise ValueError" % func.__name__)


def main():
    test_mean_basic()
    test_median_odd()
    test_median_even()
    test_empty_raises()
    print("ALL PASS")


if __name__ == "__main__":
    main()
