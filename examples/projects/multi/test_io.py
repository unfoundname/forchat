"""Tests for io_utils.read_lines. Runnable directly: ``python test_io.py``."""

import os
import sys
import tempfile

# Make ``import io_utils`` work no matter which directory we are launched from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from io_utils import read_lines  # noqa: E402


def test_read_lines_returns_all_lines_without_newlines(tmpdir):
    path = os.path.join(tmpdir, "sample.txt")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("alpha\nbeta\ngamma\n")

    lines = read_lines(path)

    assert isinstance(lines, list), "read_lines must return a list"
    assert len(lines) == 3, "expected all 3 lines, got %r" % (lines,)
    assert lines == ["alpha", "beta", "gamma"], "unexpected content: %r" % (lines,)
    for line in lines:
        assert "\n" not in line and "\r" not in line, (
            "line endings must be stripped, got %r" % (line,)
        )
    # No newline is invented for the caller either.
    assert not lines[-1].endswith("\n"), "trailing newline should be removed"


def test_read_lines_keeps_order_and_blank_lines(tmpdir):
    path = os.path.join(tmpdir, "blank.txt")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("first\n\nthird\n")

    lines = read_lines(path)

    assert len(lines) == 3, "blank line must be preserved as an empty string"
    assert lines == ["first", "", "third"], "order/padding wrong: %r" % (lines,)


def test_read_lines_missing_file_returns_empty_list(tmpdir):
    missing = os.path.join(tmpdir, "does-not-exist.txt")

    result = read_lines(missing)

    assert isinstance(result, list), "missing file must yield a list, got %r" % (type(result),)
    assert result == [], "missing file must yield an empty list, got %r" % (result,)


def main():
    """Run every test_* function with a fresh temporary directory each time."""
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    assert len(tests) >= 1, "no tests collected"

    for name, func in tests:
        with tempfile.TemporaryDirectory() as tmpdir:
            func(tmpdir)
        print("ok %s" % name)

    print("ALL PASS")


if __name__ == "__main__":
    main()
