#!/usr/bin/env python3
"""Word frequency counter.

Usable both as an importable module and as a command line tool:

    python wordcount.py <file> [-n N]
    python wordcount.py <file> [--top N]

N defaults to 5. Only the Python standard library is used.
"""

import argparse
import re
import sys

__all__ = ["count_words", "top_words", "format_output", "main"]

# A "word" is a run of letters/digits (unicode friendly, underscore treated as a
# separator) optionally containing internal apostrophes, e.g. "don't".
_TOKEN_RE = re.compile(r"[^\W_]+(?:'[^\W_]+)*", re.UNICODE)

DEFAULT_TOP_N = 5


def count_words(text):
    """Count word occurrences in *text*.

    Words are split on whitespace and punctuation, lowercased and counted.
    Empty strings are never counted.

    >>> count_words("the cat the dog the")
    {'the': 3, 'cat': 1, 'dog': 1}
    """
    if not text:
        return {}

    counts = {}
    for token in _TOKEN_RE.findall(text.lower()):
        if not token:
            continue
        counts[token] = counts.get(token, 0) + 1
    return counts


def top_words(counts, n=DEFAULT_TOP_N):
    """Return the *n* most frequent words as ``[(word, count), ...]``.

    Ordering is frequency descending, ties broken by word ascending, so the
    result is fully deterministic. ``n <= 0`` yields an empty list.

    >>> top_words({"the": 3, "cat": 1, "dog": 1}, n=2)
    [('the', 3), ('cat', 1)]
    """
    if not counts or n is None or n <= 0:
        return []
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return ordered[:n]


def format_output(pairs):
    """Render ``(word, count)`` pairs as ``"word: count"`` lines.

    Returns ``""`` (no lines at all) for empty input.
    """
    return "\n".join("{}: {}".format(word, count) for word, count in pairs)


def _read_text(path):
    """Read *path* as text, tolerating undecodable bytes."""
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="wordcount.py",
        description="Count the most frequent words in a text file.",
    )
    parser.add_argument("file", help="path of the text file to analyse")
    parser.add_argument(
        "-n",
        "--top",
        dest="n",
        type=int,
        default=DEFAULT_TOP_N,
        metavar="N",
        help="how many words to print (default: %d)" % DEFAULT_TOP_N,
    )
    return parser


def main(argv=None):
    """Command line entry point. Returns the process exit code."""
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse already printed usage/help
        code = exc.code
        return code if isinstance(code, int) else 2

    if args.n < 0:
        print(
            "wordcount.py: error: -n/--top must be >= 0 (got %r)" % (args.n,),
            file=sys.stderr,
        )
        return 2

    try:
        text = _read_text(args.file)
    except OSError as exc:
        reason = getattr(exc, "strerror", None) or str(exc)
        print(
            "wordcount.py: error: cannot read %r: %s" % (args.file, reason),
            file=sys.stderr,
        )
        return 2

    output = format_output(top_words(count_words(text), args.n))
    if output:
        print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
