"""The one definition of done for this tool: ruff lint, ruff format, pyright strict and pytest.
Every step captures its output, matches the text it must show and prints what it measured; a step
that measured less than its floor fails. An exit code alone is never accepted. The floors live in
check.json next to pyproject.toml. This file is the same in tools/evals and tools/trajectory, and
a trajectory test keeps the two copies equal."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
FLOORS_PATH = ROOT / "check.json"
TOOLS = ("uv", "ruff", "pyright")
STEP_SECONDS_MAX = 900  # pytest, the slowest step, takes about a minute on a laptop
DETAIL_CHARS_MAX = 4000  # the tail of a failing step's output
FLOOR_KEYS = ("py_files_min", "typed_files_min", "tests_min", "skipped_max")
PYTEST = ("uv", "run", "--quiet", "--no-project", "--python", "3.12", "--with", "pytest")
PYTEST_ARGS = ("python", "-m", "pytest", "-q", "-rs")
FORMATTED = re.compile(r"[0-9]+ files? already formatted")
PASSED = re.compile(r"^([0-9]+) passed")
SKIPPED = re.compile(r"([0-9]+) skipped")
SUMMARY_WORDS_BANNED = ("failed", "error", "warning", "no tests ran")
PYRIGHT_COUNTS = ("errorCount", "warningCount", "informationCount")


def _fields(value: object) -> dict[str, object] | None:
    """A decoded JSON object with its keys checked to be strings, else None."""
    if not isinstance(value, dict):
        return None
    raw = cast(dict[object, object], value)  # widening only: json gives dict[Unknown, Unknown]
    if not all(isinstance(key, str) for key in raw):
        return None
    return cast(dict[str, object], raw)  # every key checked on the line above


def _count(value: object) -> int | None:
    """value when it is a whole number 0 or more; JSON true would pass as 1 without the bool
    test."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


class CheckFailed(Exception):
    """A step failed. message says which step and what it measured; detail is its output."""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


@dataclass(frozen=True)
class Floors:
    py_files_min: int
    typed_files_min: int  # pyright reads only the paths its config includes
    tests_min: int  # passed, not run: skips don't count toward it
    skipped_max: int
    skip_reason: re.Pattern[str] | None  # None: no reason is good enough to skip


def parse_floors(text: str) -> Floors:
    """check.json: the four counts, skip_reason (a regex or null) and why, nothing else."""
    try:
        raw: object = json.loads(text)
    except json.JSONDecodeError as error:
        raise CheckFailed(f"check.json is not JSON: {error}") from error
    fields = _fields(raw)
    if fields is None:
        raise CheckFailed("check.json is not an object")
    expected = {*FLOOR_KEYS, "skip_reason", "why"}
    if set(fields) != expected:
        raise CheckFailed(f"check.json keys {sorted(fields)}, expected {sorted(expected)}")
    counts: dict[str, int] = {}
    for key in FLOOR_KEYS:
        value = _count(fields[key])
        if value is None:
            raise CheckFailed(f"check.json {key} is not a count: {fields[key]!r}")
        counts[key] = value
    if counts["py_files_min"] == 0 or counts["typed_files_min"] == 0 or counts["tests_min"] == 0:
        raise CheckFailed("check.json: a zero floor lets an empty scope pass")
    return Floors(**counts, skip_reason=_skip_pattern(fields["skip_reason"]))


def _skip_pattern(value: object) -> re.Pattern[str] | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise CheckFailed(f"check.json skip_reason is not a regex or null: {value!r}")
    try:
        return re.compile(value)
    except re.error as error:
        raise CheckFailed(f"check.json skip_reason does not compile: {error}") from error


def run(argv: Sequence[str]) -> tuple[int, str]:
    """argv in the tool's root, stdout and stderr together, bounded in time."""
    assert argv and argv[0] in TOOLS, "only the tools checked for up front run here"
    try:
        done = subprocess.run(
            argv,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=STEP_SECONDS_MAX,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise CheckFailed(f"{argv[0]} ran past {STEP_SECONDS_MAX} s") from error
    return done.returncode, done.stdout


def ruff_lint_verdict(listing: str, out: str, floors: Floors) -> int:
    """The file count comes from ruff itself (--show-files), so an empty scope cannot pass."""
    count = sum(1 for line in listing.splitlines() if line.endswith(".py"))
    if count < floors.py_files_min:
        raise CheckFailed(f"ruff scope has {count} files, floor {floors.py_files_min}", listing)
    if out.strip() != "All checks passed!":
        raise CheckFailed("ruff-check", out)
    return count


def ruff_format_verdict(out: str, floors: Floors) -> int:
    text = out.strip()
    if "would reformat" in text.lower() or FORMATTED.fullmatch(text) is None:
        raise CheckFailed("ruff-format", out)
    count = int(text.split(" ", 1)[0])
    if count < floors.py_files_min:
        raise CheckFailed(f"ruff format saw {count} files, floor {floors.py_files_min}")
    return count


def pyright_verdict(out: str, floor: int) -> int:
    """`0 errors` is also what pyright prints with no files, so files analyzed is part of it."""
    assert floor > 0, "a zero floor lets an empty scope pass"
    try:
        raw: object = json.loads(out)
    except json.JSONDecodeError as error:
        raise CheckFailed("pyright did not print JSON", out) from error
    top = _fields(raw)
    fields = _fields(top.get("summary")) if top is not None else None
    if fields is None:
        raise CheckFailed("pyright JSON has no summary", out)
    if any(_count(fields.get(key)) != 0 for key in PYRIGHT_COUNTS):
        counts = {key: fields.get(key) for key in PYRIGHT_COUNTS}
        raise CheckFailed(f"pyright reported {counts}", out)
    analyzed = _count(fields.get("filesAnalyzed"))
    if analyzed is None or analyzed < floor:
        raise CheckFailed(f"pyright analyzed {fields.get('filesAnalyzed')!r} files, floor {floor}")
    return analyzed


def pytest_verdict(code: int, out: str, floors: Floors) -> tuple[int, int]:
    """(passed, skipped). A skip for an unlisted reason, or past the allowance, means tests went
    quiet, so it fails like an error does."""
    lines = out.splitlines()
    last = lines[-1] if lines else ""
    if code != 0:
        raise CheckFailed("pytest-exit", out)
    passed = PASSED.match(last)
    if passed is None:
        raise CheckFailed("pytest-summary", out)
    for word in SUMMARY_WORDS_BANNED:
        if word in last:
            raise CheckFailed(f"pytest-summary {word}", out)
    count = int(passed.group(1))
    if count < floors.tests_min:
        raise CheckFailed(f"pytest ran {count} tests, floor {floors.tests_min}")
    skip_lines = [line for line in lines if line.startswith("SKIPPED ")]
    pattern = floors.skip_reason
    odd = [line for line in skip_lines if pattern is None or pattern.search(line) is None]
    if odd:
        raise CheckFailed("pytest skipped tests for a reason not in skip_reason", "\n".join(odd))
    found = SKIPPED.search(last)
    skipped = int(found.group(1)) if found else 0
    if skipped != len(skip_lines):
        raise CheckFailed(f"pytest skip count {skipped}, listed {len(skip_lines)}", out)
    if skipped > floors.skipped_max:
        raise CheckFailed(f"pytest skipped {skipped} tests, at most {floors.skipped_max}", out)
    return count, skipped


def check_all(floors: Floors) -> list[str]:
    """Each step's ok line, in order; the first failure raises."""
    missing = [tool for tool in TOOLS if shutil.which(tool) is None]
    if missing:
        raise CheckFailed(f"missing {', '.join(missing)}")
    _, listing = run(("ruff", "check", ".", "--show-files"))
    _, out = run(("ruff", "check", "."))
    lint = ruff_lint_verdict(listing, out, floors)
    _, out = run(("ruff", "format", "--check", "."))
    formatted = ruff_format_verdict(out, floors)
    # -p: a pyrightconfig.json higher up must not replace this tool's strict config
    _, out = run(("pyright", "-p", "pyproject.toml", "--outputjson"))
    analyzed = pyright_verdict(out, floors.typed_files_min)
    code, out = run((*PYTEST, *PYTEST_ARGS))
    passed, skipped = pytest_verdict(code, out, floors)
    return [
        f"check: ruff lint ok, {lint} files",
        f"check: ruff format ok, {formatted} files",
        f"check: pyright ok, {analyzed} files, 0 errors, 0 warnings",
        f"check: pytest ok, {passed} tests, {skipped} skipped",
    ]


def main(argv: Sequence[str]) -> int:
    if argv:
        print("usage: bin/check (no arguments)")
        return 2
    try:
        floors = parse_floors(FLOORS_PATH.read_text(encoding="utf-8"))
        for line in check_all(floors):
            print(line)
    except (CheckFailed, OSError) as failure:
        message = failure.message if isinstance(failure, CheckFailed) else str(failure)
        detail = failure.detail if isinstance(failure, CheckFailed) else ""
        if detail:
            print(detail[-DETAIL_CHARS_MAX:].rstrip())
        print(f"check: FAIL {message}")
        return 1
    print("check: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
