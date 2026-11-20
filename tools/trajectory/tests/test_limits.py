"""Constants are sane, and the 70-line function limit is enforced over the whole package."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from traj import limits, reader_kiro

PACKAGE = Path(__file__).resolve().parents[1] / "traj"
FUNCTION_LINES_MAX = 70


class LimitsTest(unittest.TestCase):
    def test_every_max_is_a_positive_int(self) -> None:
        names = [n for n in vars(limits) if n.endswith("_MAX")]
        self.assertGreater(len(names), 5)
        for name in names:
            value = getattr(limits, name)
            self.assertIsInstance(value, int, name)
            self.assertGreater(value, 0, name)

    def test_closed_lists(self) -> None:
        self.assertEqual(len(limits.FLAG_NAMES), 4)
        self.assertEqual(list(limits.FLAG_NAMES), sorted(set(limits.FLAG_NAMES)))
        self.assertEqual(len(set(limits.TOOL_CLASSES)), len(limits.TOOL_CLASSES))
        self.assertEqual(len(set(limits.STATUS_VALUES)), 5)
        for harness in limits.HARNESSES:
            self.assertIn(harness, limits.TREES)

    def test_ids_and_required_keys_cannot_drift(self) -> None:
        self.assertEqual(set(reader_kiro.ID_KEYS), set(limits.REQUIRED_KIRO_KEYS))
        for kind, ids in reader_kiro.ID_KEYS.items():
            self.assertTrue(set(ids) <= set(limits.REQUIRED_KIRO_KEYS[kind]), kind)
            self.assertGreater(len(ids), 0, kind)
        self.assertFalse(set(limits.REQUIRED_KIRO_KEYS) & limits.KIRO_IGNORED_TYPES)

    def test_tool_classes_are_in_the_closed_vocabulary(self) -> None:
        self.assertTrue(set(limits.TOOL_CLASS.values()) <= set(limits.TOOL_CLASSES))

    def test_no_function_over_the_line_limit(self) -> None:
        files = sorted(PACKAGE.glob("*.py"))
        self.assertGreater(len(files), 3)
        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    length = (node.end_lineno or node.lineno) - node.lineno + 1
                    self.assertLessEqual(length, FUNCTION_LINES_MAX, f"{path.name}:{node.name}")

    def test_no_recursion(self) -> None:
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef):
                    continue
                calls = {
                    c.func.id
                    for c in ast.walk(node)
                    if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                }
                self.assertNotIn(node.name, calls, f"{path.name}:{node.name} calls itself")
