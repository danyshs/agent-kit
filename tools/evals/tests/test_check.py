"""Goal: bin/check fails on every way a step can show less than a clean, full run, and passes a
clean one: an exit code alone is never proof, and each guard must be seen to trip.
Method: feed each verdict the text the real tool prints, clean and broken, and run main with the
tools faked, so the order of the steps and the stop at the first failure are tested without
running ruff, pyright or pytest inside pytest. check.py is the same file in tools/trajectory; its
test there checks the copies match, so these tests cover both."""

from __future__ import annotations

import io
import json
import re
import subprocess
import tempfile
import unittest
from collections.abc import Sequence
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from lib import check
from lib.check import CheckFailed, Floors

FLOORS = Floors(
    py_files_min=3,
    typed_files_min=3,
    tests_min=10,
    skipped_max=1,
    skip_reason=re.compile(r": no \S+ here$"),
)
GOOD_FLOORS: dict[str, object] = {
    "why": "test",
    "py_files_min": 3,
    "typed_files_min": 3,
    "tests_min": 10,
    "skipped_max": 1,
    "skip_reason": r": no \S+ here$",
}


def pyright_json(**changes: object) -> str:
    summary: dict[str, object] = {
        "errorCount": 0,
        "warningCount": 0,
        "informationCount": 0,
        "filesAnalyzed": 9,
    }
    return json.dumps({"summary": {**summary, **changes}})


def floors_text(**changes: object) -> str:
    return json.dumps({**GOOD_FLOORS, **changes})


class FloorsTest(unittest.TestCase):
    def test_a_good_file_loads(self) -> None:
        floors = check.parse_floors(floors_text(skip_reason=None))
        self.assertEqual((floors.py_files_min, floors.tests_min, floors.skip_reason), (3, 10, None))
        self.assertEqual(check.parse_floors(floors_text()).skip_reason, FLOORS.skip_reason)

    def test_the_shipped_file_loads(self) -> None:
        floors = check.parse_floors(check.FLOORS_PATH.read_text(encoding="utf-8"))
        self.assertGreater(floors.tests_min, 0)

    def test_bad_files_refuse(self) -> None:
        missing = {k: v for k, v in GOOD_FLOORS.items() if k != "tests_min"}
        cases = {
            "not json": "{",
            "not an object": "[]",
            "missing key": json.dumps(missing),
            "unknown key": floors_text(test_min=5),
            "negative": floors_text(skipped_max=-1),
            "true is not a count": floors_text(tests_min=True),
            "a float": floors_text(tests_min=2.5),
            "zero floor": floors_text(typed_files_min=0),
            "bad regex": floors_text(skip_reason="("),
            "empty regex": floors_text(skip_reason=""),
        }
        for name, text in cases.items():
            with self.subTest(name), self.assertRaises(CheckFailed):
                check.parse_floors(text)


class RuffTest(unittest.TestCase):
    def test_lint_counts_python_files_and_needs_the_pass_line(self) -> None:
        listing = "/r/a.py\n/r/b.py\n/r/c.py\n"
        self.assertEqual(check.ruff_lint_verdict(listing, "All checks passed!\n", FLOORS), 3)
        with self.assertRaisesRegex(CheckFailed, "floor 3"):
            short = "/r/a.py\n/r/b.py\n/r/stub.pyi\n/r/README.md\n"
            check.ruff_lint_verdict(short, "All checks passed!", FLOORS)
        for out in ("a.py:1:1: F401 unused\nFound 1 error.", "warning: bad\nAll checks passed!"):
            with self.subTest(out), self.assertRaisesRegex(CheckFailed, "ruff-check"):
                check.ruff_lint_verdict(listing, out, FLOORS)

    def test_format_needs_the_already_formatted_line_at_the_floor(self) -> None:
        self.assertEqual(check.ruff_format_verdict("3 files already formatted\n", FLOORS), 3)
        with self.assertRaisesRegex(CheckFailed, "floor 3"):
            check.ruff_format_verdict("1 file already formatted", FLOORS)
        bad = "Would reformat: a.py\n1 file would be reformatted, 2 files already formatted"
        extra = "3 files already formatted\nwarning: unknown setting"
        for out in (bad, "", extra, "3 files already formatted, 1 file would reformat"):
            with self.subTest(out), self.assertRaisesRegex(CheckFailed, "ruff-format"):
                check.ruff_format_verdict(out, FLOORS)


class PyrightTest(unittest.TestCase):
    def test_a_clean_run_at_the_floor_passes(self) -> None:
        self.assertEqual(check.pyright_verdict(pyright_json(), 9), 9)

    def test_anything_less_than_clean_and_full_refuses(self) -> None:
        cases = {
            "empty scope": pyright_json(filesAnalyzed=0),
            "under the floor": pyright_json(filesAnalyzed=8),
            "an error": pyright_json(errorCount=1),
            "a warning": pyright_json(warningCount=1),
            "an information": pyright_json(informationCount=1),
            "false is not zero": pyright_json(errorCount=False),
            "a count missing": json.dumps({"summary": {"filesAnalyzed": 9}}),
            "no summary": "{}",
            "junk": "not json",
        }
        for name, out in cases.items():
            with self.subTest(name), self.assertRaises(CheckFailed):
                check.pyright_verdict(out, 9)


class PytestTest(unittest.TestCase):
    def test_a_clean_run_passes_with_its_counts(self) -> None:
        self.assertEqual(check.pytest_verdict(0, "....\n12 passed in 1.0s", FLOORS), (12, 0))
        out = "SKIPPED [1] tests/x.py:3: no HOME here\n12 passed, 1 skipped in 1.0s"
        self.assertEqual(check.pytest_verdict(0, out, FLOORS), (12, 1))

    def test_a_failure_or_a_quiet_summary_refuses(self) -> None:
        cases = {
            "exit code": (1, "12 passed in 1s", "pytest-exit"),
            "no summary": (0, "", "pytest-summary"),
            "no tests ran": (0, "no tests ran in 0.01s", "pytest-summary"),
            "failed first": (0, "3 failed, 12 passed in 1s", "pytest-summary"),
            "a warning": (0, "12 passed, 1 warning in 1s", "warning"),
            "an error": (0, "12 passed, 1 error in 1s", "error"),
            "under the floor": (0, "9 passed in 1s", "floor 10"),
        }
        for name, (code, out, reason) in cases.items():
            with self.subTest(name), self.assertRaisesRegex(CheckFailed, reason):
                check.pytest_verdict(code, out, FLOORS)

    def test_skips_need_a_listed_reason_a_matching_count_and_room(self) -> None:
        odd = "SKIPPED [1] tests/x.py:3: flaky\n12 passed, 1 skipped in 1s"
        with self.assertRaisesRegex(CheckFailed, "reason"):
            check.pytest_verdict(0, odd, FLOORS)
        line = "SKIPPED [1] tests/x.py:3: no HOME here"
        with self.assertRaisesRegex(CheckFailed, "reason"):
            no_reasons = Floors(3, 3, 10, 1, skip_reason=None)
            check.pytest_verdict(0, f"{line}\n12 passed, 1 skipped in 1s", no_reasons)
        with self.assertRaisesRegex(CheckFailed, "skip count 2, listed 1"):
            check.pytest_verdict(0, f"{line}\n12 passed, 2 skipped in 1s", FLOORS)
        with self.assertRaisesRegex(CheckFailed, "at most 1"):
            check.pytest_verdict(0, f"{line}\n{line}\n12 passed, 2 skipped in 1s", FLOORS)


def fake_run(pyright_out: str) -> mock.Mock:
    def run(argv: Sequence[str]) -> tuple[int, str]:
        if argv[0] == "ruff" and "--show-files" in argv:
            return 0, "/r/a.py\n/r/b.py\n/r/c.py\n"
        if argv[:2] == ("ruff", "check"):
            return 0, "All checks passed!\n"
        if argv[:2] == ("ruff", "format"):
            return 0, "3 files already formatted\n"
        if argv[0] == "pyright":
            return 0, pyright_out
        assert argv[0] == "uv", f"unexpected step {argv}"
        return 0, "....\n12 passed in 1.0s\n"

    return mock.Mock(side_effect=run)


class MainTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "check.json"
        path.write_text(floors_text(), encoding="utf-8")
        patcher = mock.patch.object(check, "FLOORS_PATH", path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def main(self, run: mock.Mock, which: object = "/bin/tool") -> tuple[int, list[str]]:
        out = io.StringIO()
        with (
            mock.patch.object(check, "run", run),
            mock.patch.object(check.shutil, "which", return_value=which),
            redirect_stdout(out),
        ):
            code = check.main([])
        return code, out.getvalue().splitlines()

    def test_a_clean_run_prints_each_step_then_ok(self) -> None:
        code, lines = self.main(fake_run(pyright_json(filesAnalyzed=3)))
        self.assertEqual(code, 0)
        self.assertEqual(
            lines,
            [
                "check: ruff lint ok, 3 files",
                "check: ruff format ok, 3 files",
                "check: pyright ok, 3 files, 0 errors, 0 warnings",
                "check: pytest ok, 12 tests, 0 skipped",
                "check: ok",
            ],
        )

    def test_a_missing_tool_fails_before_any_step_runs(self) -> None:
        run = fake_run(pyright_json())
        code, lines = self.main(run, which=None)
        self.assertEqual((code, lines[-1]), (1, "check: FAIL missing uv, ruff, pyright"))
        run.assert_not_called()

    def test_a_failing_step_stops_the_check(self) -> None:
        run = fake_run(pyright_json(errorCount=2))
        code, lines = self.main(run)
        self.assertEqual(code, 1)
        self.assertTrue(lines[-1].startswith("check: FAIL pyright reported"), lines[-1])
        self.assertNotIn("check: ok", lines)
        self.assertNotIn("uv", [call.args[0][0] for call in run.call_args_list], "pytest skipped")

    def test_a_bad_floors_file_fails(self) -> None:
        check.FLOORS_PATH.write_text("{}", encoding="utf-8")
        code, lines = self.main(fake_run(pyright_json()))
        self.assertEqual(code, 1)
        self.assertTrue(lines[-1].startswith("check: FAIL check.json keys"), lines[-1])

    def test_arguments_are_refused(self) -> None:
        with redirect_stdout(io.StringIO()):
            self.assertEqual(check.main(["--fast"]), 2)

    def test_run_takes_only_the_listed_tools(self) -> None:
        with self.assertRaises(AssertionError):
            check.run(("rm", "-rf", "x"))

    def test_run_is_bounded_in_time(self) -> None:
        late = subprocess.TimeoutExpired(["ruff"], check.STEP_SECONDS_MAX)
        with mock.patch.object(check.subprocess, "run", side_effect=late) as run:
            with self.assertRaisesRegex(CheckFailed, "ran past"):
                check.run(("ruff", "check", "."))
        self.assertEqual(run.call_args.kwargs["timeout"], check.STEP_SECONDS_MAX)
