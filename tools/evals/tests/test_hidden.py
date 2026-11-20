"""Goal: g_hidden_tests_pass grades model-written code against hidden tests, inside the sandbox,
from pytest's summary line, and never runs anything when the sandbox is down (design 3.4.1, 3.5,
3.6). Method: tiny hidden dirs built per test; good code PASSes, planted bad code FAILs, and
hostile code (network, writes outside, infinite loop, test shadowing) is contained. Exit codes
are never the verdict: a crafted run that exits 0 without a summary must FAIL."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib import hidden
from lib.grade import GRADERS, extracted_files
from lib.hidden import count_tests, is_solution_name, parse_summary, run_hidden_tests
from lib.run import Event, Verdict
from tests.helpers_run import call, make_view

TESTS = (
    "import importlib\n\n\n"
    "def test_double():\n"
    "    assert importlib.import_module('calc').double(2) == 4\n\n\n"
    "def test_negative():\n"
    "    assert importlib.import_module('calc').double(-3) == -6\n"
)
GOOD = "def double(x):\n    return x * 2\n"
BAD = "def double(x):\n    return x + x if x > 0 else 0\n"


def write_call(path: str, text: str) -> Event:
    return call("fs_write", {"path": path, "text": text})


@unittest.skipUnless(sys.platform == "darwin", "the sandbox is macOS only")
class RunHiddenTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.tests_dir = self.root / "hidden" / "calc"
        self.tests_dir.mkdir(parents=True)
        (self.tests_dir / "test_calc.py").write_text(TESTS, encoding="utf-8")

    def run_it(self, files: dict[str, str]) -> tuple[str, str]:
        return run_hidden_tests(files, self.tests_dir)

    def test_good_code_passes_with_counts_in_the_evidence(self) -> None:
        self.assertEqual(
            self.run_it({"calc.py": GOOD}),
            ("PASS", "hidden tests passed=2 failed=0 errors=0 total=2"),
        )

    def test_planted_bad_code_fails_and_counts_the_failure(self) -> None:
        verdict, evidence = self.run_it({"calc.py": BAD})
        self.assertEqual(verdict, "FAIL")
        self.assertIn("passed=1 failed=1 errors=0 total=2", evidence)

    def test_a_missing_module_fails_every_test(self) -> None:
        verdict, evidence = self.run_it({"other.py": GOOD})
        self.assertEqual(verdict, "FAIL")
        self.assertIn("passed=0", evidence)
        self.assertIn("total=2", evidence)

    def test_a_skipped_test_is_not_a_pass(self) -> None:
        # one test passes, one is skipped by the solution itself: passed=1, failed=0, errors=0
        code = (
            "import pytest\n"
            "def double(x):\n"
            "    if x < 0:\n"
            "        pytest.skip('not today')\n"
            "    return x * 2\n"
        )
        verdict, evidence = self.run_it({"calc.py": code})
        self.assertEqual(verdict, "FAIL")
        self.assertIn("passed=1 failed=0 errors=0 total=2", evidence)

    def test_a_syntax_error_in_the_solution_fails(self) -> None:
        verdict, _ = self.run_it({"calc.py": "def double(x:\n"})
        self.assertEqual(verdict, "FAIL")

    def test_exit_zero_without_a_summary_line_is_not_a_pass(self) -> None:
        # solution that kills pytest cleanly before any report: exit status 0, no summary
        sneaky = "import os\nos._exit(0)\n"
        verdict, evidence = self.run_it({"calc.py": sneaky})
        self.assertEqual(verdict, "FAIL")
        self.assertIn("no pytest summary line", evidence)

    def test_an_infinite_loop_times_out_and_fails(self) -> None:
        with mock.patch.object(hidden, "HIDDEN_TIMEOUT_S", 2.0):
            verdict, evidence = self.run_it({"calc.py": "while True:\n    pass\n"})
        self.assertEqual(verdict, "FAIL")
        self.assertIn("timed out", evidence)

    def test_network_is_denied_to_the_solution(self) -> None:
        code = (
            "import socket\n"
            "try:\n"
            "    socket.create_connection(('127.0.0.1', 9), timeout=2)\n"
            "    REACHED = True\n"
            "except OSError as err:\n"
            "    REACHED = isinstance(err, ConnectionRefusedError)\n"
            "def double(x):\n    assert not REACHED, 'network was reachable'\n    return x * 2\n"
        )
        self.assertEqual(self.run_it({"calc.py": code})[0], "PASS")

    def test_writes_outside_the_scratch_dir_are_denied(self) -> None:
        target = self.root / "escaped.txt"
        code = (
            f"try:\n    open({str(target)!r}, 'w').write('x')\nexcept PermissionError:\n    pass\n"
            + GOOD
        )
        self.assertEqual(self.run_it({"calc.py": code})[0], "PASS")
        self.assertFalse(target.exists(), "the sandbox stopped the write")

    def test_the_solution_cannot_replace_the_hidden_tests(self) -> None:
        self.assertFalse(is_solution_name("test_calc.py"))
        self.assertFalse(is_solution_name("conftest.py"))
        with self.assertRaises(AssertionError):
            self.run_it({"test_calc.py": "def test_double():\n    pass\n"})

    def test_nothing_runs_when_the_sandbox_is_down(self) -> None:
        marker = self.root / "ran.txt"
        code = f"open({str(marker)!r}, 'w').write('x')\n" + GOOD
        with mock.patch.object(hidden, "check_sandbox", return_value="no sandbox here"):
            self.assertEqual(self.run_it({"calc.py": code}), ("UNKNOWN", "no sandbox here"))
        self.assertFalse(marker.exists())

    def test_an_empty_hidden_dir_is_unknown_not_a_pass(self) -> None:
        empty = self.root / "hidden" / "empty"
        empty.mkdir()
        verdict, evidence = run_hidden_tests({"calc.py": GOOD}, empty)
        self.assertEqual(verdict, "UNKNOWN")
        self.assertIn("no test functions", evidence)

    def test_scratch_dirs_are_cleaned_up(self) -> None:
        before = len(list(Path(tempfile.gettempdir()).glob("evhidden-*")))
        self.run_it({"calc.py": GOOD})
        self.assertEqual(len(list(Path(tempfile.gettempdir()).glob("evhidden-*"))), before)


@unittest.skipUnless(sys.platform == "darwin", "the sandbox is macOS only")
class GraderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        (self.root / "hidden" / "calc").mkdir(parents=True)
        (self.root / "hidden" / "calc" / "test_calc.py").write_text(TESTS, encoding="utf-8")
        self.args: dict[str, object] = {"tests_dir": "hidden/calc", "module": "calc"}

    def grade(self, events: list[Event], final_text: str = "") -> tuple[Verdict, str]:
        view = make_view(events, final_text)
        return GRADERS["g_hidden_tests_pass"](view, self.args, self.root)

    def test_pass_on_good_write_and_fail_on_bad_write(self) -> None:
        self.assertEqual(self.grade([write_call("src/calc.py", GOOD)])[0], "PASS")
        self.assertEqual(self.grade([write_call("src/calc.py", BAD)])[0], "FAIL")

    def test_fenced_block_is_the_fallback_when_nothing_was_written(self) -> None:
        text = f"Here you go:\n```python\n{GOOD}```\n"
        self.assertEqual(self.grade([], text)[0], "PASS")

    def test_no_code_at_all_is_fail_not_pass(self) -> None:
        verdict, evidence = self.grade([], "I could not do it")
        self.assertEqual(verdict, "FAIL")
        self.assertIn("no evidence", evidence)


class ExtractionTest(unittest.TestCase):
    def test_first_attempt_per_base_name_wins_and_unsafe_names_are_dropped(self) -> None:
        view = make_view(
            [
                call("fs_write", {"path": "src/calc.py", "text": "first"}),
                call("fs_write", {"path": "other/calc.py", "text": "second"}),
                call("fs_write", {"path": "tests/test_calc.py", "text": "shadow"}),
                call("fs_write", {"path": "conftest.py", "text": "shadow"}),
                call("fs_write", {"path": "notes.md", "text": "not code"}),
                call("fs_write", {"path": "run.fish", "text": "echo hi"}),
            ]
        )
        self.assertEqual(extracted_files(view, "calc"), {"calc.py": "first", "run.fish": "echo hi"})

    def test_fences_are_named_after_the_module(self) -> None:
        view = make_view([], "```py\nx = 1\n```\n```fish\necho hi\n```\n```sh\nls\n```\n")
        self.assertEqual(extracted_files(view, "m"), {"m.py": "x = 1\n", "m.fish": "echo hi\n"})

    def test_a_write_beats_a_fence(self) -> None:
        view = make_view([call("fs_write", {"path": "a.py", "text": "w"})], "```python\nf\n```")
        self.assertEqual(extracted_files(view, "m"), {"a.py": "w"})


class SummaryTest(unittest.TestCase):
    def test_summary_variants(self) -> None:
        self.assertEqual(parse_summary("..\n2 passed in 0.01s\n"), {"passed": 2})
        self.assertEqual(
            parse_summary("x\n1 failed, 3 passed, 1 warning in 0.5s\n"),
            {"failed": 1, "passed": 3, "warning": 1},
        )
        self.assertEqual(parse_summary("2 errors in 0.1s"), {"errors": 2})
        self.assertEqual(parse_summary("1 error in 0.1s"), {"errors": 1})

    def test_teardown_error_fails_even_when_every_test_passed(self) -> None:
        verdict, evidence = hidden._verdict({"passed": 2, "errors": 1}, 2)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(verdict, "FAIL")
        self.assertIn("errors=1", evidence)

    def test_no_summary_means_none(self) -> None:
        self.assertIsNone(parse_summary(""))
        self.assertIsNone(parse_summary("2 passed"))  # no timing: a program printed this
        self.assertIsNone(parse_summary("passed in 0.1s"))

    def test_count_tests_reads_the_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "test_a.py").write_text(
                "def test_x():\n    pass\ndef helper():\n    pass\n"
            )
            (Path(tmp) / "test_b.py").write_text("async def test_y():\n    pass\n")
            self.assertEqual(count_tests(Path(tmp)), 2)
