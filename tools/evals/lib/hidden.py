"""Hidden-test runner for write-mode tasks (design 3.4.1, 3.5, 3.6).

Model-written files plus the task's hidden test_*.py go into a throwaway dir outside the repo and
pytest runs there inside the sandbox. The verdict comes from pytest's summary line, never from
the exit code (debugging-discipline #5). No sandbox, no run: the verdict is UNKNOWN.
"""

from __future__ import annotations

import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Final

from lib.run import Verdict
from lib.sandbox import STATUS_OK, check_sandbox, run_sandboxed

Result = tuple[Verdict, str]  # (verdict, evidence)
TEST_DEF_RE: Final = re.compile(r"^\s*(?:async\s+)?def (test_\w+)\(", re.MULTILINE)
SUMMARY_COUNT_RE: Final = re.compile(
    r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|warnings?|deselected)"
)
SUMMARY_TIME_RE: Final = re.compile(r"\bin \d+(\.\d+)?s\b")
SOLUTION_SUFFIXES: Final = (".py", ".fish", ".sh")
SOLUTION_NAME_RE: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
HIDDEN_TIMEOUT_S: Final = 60.0
TEST_FILES_MAX: Final = 16
PYTEST_ARGV: Final = ("-m", "pytest", "-q", "-p", "no:cacheprovider", "--no-header", "--tb=no")


def is_solution_name(name: str) -> bool:
    """A model file may not shadow the hidden tests: no conftest, no test_ prefix."""
    assert name, "a file name"
    assert "/" not in name, "a base name, not a path"
    ok = SOLUTION_NAME_RE.fullmatch(name) is not None and name.endswith(SOLUTION_SUFFIXES)
    return ok and name != "conftest.py" and not name.startswith("test_")


def hidden_test_files(tests_dir: Path) -> list[Path]:
    assert tests_dir.is_dir(), "the hidden dir exists"
    found = sorted(tests_dir.glob("test_*.py"))
    assert len(found) <= TEST_FILES_MAX, "a hidden dir has few test files"
    return found


def count_tests(tests_dir: Path) -> int:
    """Test functions in the hidden dir, by source; parametrize is banned so this is exact."""
    sources = [p.read_text(encoding="utf-8") for p in hidden_test_files(tests_dir)]
    total = sum(len(TEST_DEF_RE.findall(source)) for source in sources)
    assert total >= 0, "a count"
    return total


def parse_summary(stdout_text: str) -> dict[str, int] | None:
    """Counts from pytest's last line ('2 failed, 3 passed in 0.1s'), or None without one."""
    lines = [line for line in stdout_text.splitlines() if line.strip()]
    if not lines or SUMMARY_TIME_RE.search(lines[-1]) is None:
        return None
    counts: dict[str, int] = {}
    for number, word in SUMMARY_COUNT_RE.findall(lines[-1]):
        key = "errors" if word.startswith("error") else word
        counts[key] = counts.get(key, 0) + int(number)
    assert all(value >= 0 for value in counts.values()), "counts are not negative"
    return counts or None


def _verdict(counts: dict[str, int], total: int) -> Result:
    passed, failed, errors = (
        counts.get("passed", 0),
        counts.get("failed", 0),
        counts.get("errors", 0),
    )
    assert total >= 1, "a hidden dir with tests"
    ok = passed == total and failed == 0 and errors == 0
    evidence = f"hidden tests passed={passed} failed={failed} errors={errors} total={total}"
    return ("PASS" if ok else "FAIL"), evidence


def run_hidden_tests(files: dict[str, str], tests_dir: Path) -> Result:
    """Run `files` (name -> text) against the hidden tests; see the module docstring."""
    assert files, "caller handles the no-code case"
    assert all(is_solution_name(name) for name in files), "names are safe solution file names"
    total = count_tests(tests_dir)
    if total == 0:
        return "UNKNOWN", f"{tests_dir.name} holds no test functions"
    problem = check_sandbox()
    if problem is not None:
        return "UNKNOWN", problem
    scratch = Path(tempfile.mkdtemp(prefix="evhidden-")).resolve()
    try:
        for name, text in files.items():
            (scratch / name).write_text(text, encoding="utf-8")
        for path in hidden_test_files(
            tests_dir
        ):  # copied last: a model file can never replace a test
            shutil.copy2(path, scratch / path.name)
        done = run_sandboxed([sys.executable, *PYTEST_ARGV], scratch, HIDDEN_TIMEOUT_S)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if done.status != STATUS_OK:
        return "FAIL", f"hidden tests timed out after {HIDDEN_TIMEOUT_S:.0f}s, total={total}"
    counts = parse_summary(done.stdout_text)
    if counts is None:
        return "FAIL", f"no pytest summary line (rc {done.returncode}), total={total}"
    return _verdict(counts, total)
