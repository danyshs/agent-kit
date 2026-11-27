"""Goal: traj/check.py stays the same file as tools/evals/lib/check.py, whose tests cover it, and
this tool's check.json loads. Method: compare the two files' bytes, and parse the shipped floors."""

from __future__ import annotations

import unittest
from pathlib import Path

from traj import check

EVALS_COPY = Path(__file__).resolve().parents[2] / "evals" / "lib" / "check.py"


class CheckCopyTest(unittest.TestCase):
    def test_the_evals_copy_is_the_same_file(self) -> None:
        mine = Path(check.__file__).read_bytes()
        self.assertGreater(len(mine), 1000, "read the real file")
        self.assertEqual(mine, EVALS_COPY.read_bytes(), "a fix to one copy must reach the other")

    def test_the_shipped_floors_load_and_allow_no_skip(self) -> None:
        floors = check.parse_floors(check.FLOORS_PATH.read_text(encoding="utf-8"))
        self.assertEqual((floors.skipped_max, floors.skip_reason), (0, None))
