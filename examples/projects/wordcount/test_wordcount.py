#!/usr/bin/env python3
"""Real assertions for wordcount.py.

Run from anywhere:

    python examples/projects/wordcount/test_wordcount.py
    python test_wordcount.py        # from inside the project folder
"""

import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import wordcount  # noqa: E402  (import after sys.path fix-up)

MODULE_PATH = os.path.join(HERE, "wordcount.py")


def run_main(argv):
    """Call wordcount.main(argv) and capture (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = wordcount.main(argv)
    return code, out.getvalue(), err.getvalue()


class CountWordsTests(unittest.TestCase):
    def test_basic_frequency(self):
        # "the cat the dog the" -> the:3, cat:1, dog:1
        self.assertEqual(
            wordcount.count_words("the cat the dog the"),
            {"the": 3, "cat": 1, "dog": 1},
        )

    def test_case_insensitive_and_punctuation(self):
        counts = wordcount.count_words("The CAT, the dog!\nthe  cat;dog?")
        self.assertEqual(counts["the"], 3)
        self.assertEqual(counts["cat"], 2)
        self.assertEqual(counts["dog"], 2)
        # punctuation runs must not produce an empty-string key
        self.assertNotIn("", counts)

    def test_empty_input(self):
        self.assertEqual(wordcount.count_words(""), {})
        self.assertEqual(wordcount.count_words("   \n\t ,.!? "), {})

    def test_top_words_of_empty_input(self):
        self.assertEqual(wordcount.top_words({}), [])
        self.assertEqual(wordcount.top_words(wordcount.count_words("")), [])


class TopWordsTests(unittest.TestCase):
    def test_truncation_and_tie_break_by_word(self):
        counts = {"b": 2, "a": 2, "c": 5, "d": 1, "e": 1, "f": 9}
        self.assertEqual(
            wordcount.top_words(counts, 3),
            [("f", 9), ("c", 5), ("a", 2)],
        )
        # full ordering: count desc, then word asc -> stable and testable
        self.assertEqual(
            wordcount.top_words(counts, 10),
            [("f", 9), ("c", 5), ("a", 2), ("b", 2), ("d", 1), ("e", 1)],
        )

    def test_default_n_is_five_and_n_bounds(self):
        # a..h with counts 1..8
        counts = {chr(ord("a") + i): i + 1 for i in range(8)}
        self.assertEqual(wordcount.top_words(counts, 5), wordcount.top_words(counts))
        self.assertEqual(len(wordcount.top_words(counts)), 5)
        self.assertEqual(
            wordcount.top_words(counts),
            [("h", 8), ("g", 7), ("f", 6), ("e", 5), ("d", 4)],
        )
        self.assertEqual(wordcount.top_words(counts, 0), [])
        self.assertEqual(wordcount.top_words(counts, -3), [])

    def test_format_output(self):
        self.assertEqual(
            wordcount.format_output([("the", 3), ("cat", 1)]),
            "the: 3\ncat: 1",
        )
        self.assertEqual(wordcount.format_output([]), "")


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)

    def _write(self, name, content):
        path = os.path.join(self._tmpdir.name, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return path

    def test_end_to_end_default_top_n(self):
        # 8 distinct words, one per decreasing frequency -> default N=5
        text = (
            "alpha beta gamma delta epsilon zeta eta theta "
            "alpha beta gamma delta epsilon zeta eta "
            "alpha beta gamma delta epsilon zeta "
            "alpha beta gamma delta epsilon "
            "alpha beta gamma delta "
            "alpha beta gamma "
            "alpha beta "
            "alpha"
        )
        path = self._write("many.txt", text)
        code, out, err = run_main([path])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        lines = out.splitlines()
        self.assertEqual(len(lines), 5, "default N must be 5, got %r" % (lines,))
        self.assertEqual(
            lines,
            ["alpha: 8", "beta: 7", "gamma: 6", "delta: 5", "epsilon: 4"],
        )

    def test_end_to_end_custom_n_both_flags(self):
        path = self._write("small.txt", "the cat the dog the")
        code, out, _ = run_main([path, "-n", "2"])
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["the: 3", "cat: 1"])

        code, out, _ = run_main([path, "--top", "1"])
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["the: 3"])

    def test_empty_file_outputs_no_lines(self):
        path = self._write("empty.txt", "")
        code, out, err = run_main([path])
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_missing_file_is_graceful_error(self):
        missing = os.path.join(self._tmpdir.name, "does-not-exist.txt")
        # must not raise an uncaught exception
        code, out, err = run_main([missing])
        self.assertNotEqual(code, 0)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("does-not-exist.txt", err)

    def test_missing_argument_prints_usage(self):
        code, out, err = run_main([])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("usage", err.lower())

    def test_cli_script_path_absolute(self):
        path = self._write("sub.txt", "one two two three three three")
        proc = subprocess.run(
            [sys.executable, MODULE_PATH, path, "-n", "2"],
            cwd=HERE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(), ["three: 3", "two: 2"])
        self.assertEqual(proc.stderr, "")

    def test_cli_relative_script_name(self):
        # run the tool by its bare file name while cwd is the project folder
        path = self._write("rel.txt", "red blue red green red blue")
        proc = subprocess.run(
            [sys.executable, "wordcount.py", path],
            cwd=HERE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout.splitlines(),
            ["red: 3", "blue: 2", "green: 1"],
        )

    def test_suite_runs_from_its_own_directory(self):
        # acceptance criterion: `python test_wordcount.py` inside the folder
        if os.environ.get("WORDCOUNT_NESTED_RUN") == "1":
            self.skipTest("inner run of the nested suite")
        env = dict(os.environ)
        env["WORDCOUNT_NESTED_RUN"] = "1"
        proc = subprocess.run(
            [sys.executable, os.path.basename(os.path.abspath(__file__))],
            cwd=HERE,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("ALL PASS", proc.stdout)


def run_tests():
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful():
        print("ALL PASS (%d tests)" % result.testsRun)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(run_tests())
