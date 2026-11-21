"""Goal: no function in lib/ runs past 70 lines, counting its docstring (tiger-style's hard
limit as this repo sets it). Method: parse each file with ast and measure def line to last line;
a planted 71-line function must trip the same measurer, so an empty or broken scan can't pass."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FUNCTION_LINES_MAX = 70
FILES_MIN = 10  # fewer means the scan pointed at the wrong place


def long_functions(source: str, name: str) -> list[tuple[str, int]]:
    """(function name, line count) for each function over the limit in one file's source."""
    found: list[tuple[str, int]] = []
    for node in ast.walk(ast.parse(source, filename=name)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.end_lineno is not None, "parsed nodes carry an end line"
            lines = node.end_lineno - node.lineno + 1
            assert lines >= 1, "a function spans at least its def line"
            if lines > FUNCTION_LINES_MAX:
                found.append((node.name, lines))
    return found


class FunctionLengthTest(unittest.TestCase):
    def test_measurer_trips_on_71_lines_and_passes_70(self) -> None:
        def body(lines: int) -> str:
            return "def f():\n" + "    x = 1\n" * (lines - 1)

        self.assertEqual(long_functions(body(70), "ok.py"), [])
        self.assertEqual(long_functions(body(71), "bad.py"), [("f", 71)])

    def test_no_function_in_lib_is_over_the_limit(self) -> None:
        files = sorted((ROOT / "lib").rglob("*.py"))
        self.assertGreaterEqual(len(files), FILES_MIN, "scan found the package")
        over = {
            f"{path.relative_to(ROOT)}:{name}": lines
            for path in files
            for name, lines in long_functions(path.read_text(encoding="utf-8"), str(path))
        }
        self.assertEqual(over, {})


if __name__ == "__main__":
    unittest.main()
