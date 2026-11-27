"""Goal: the sample reports in examples/ are what the report code prints for the recorded
ledgers beside them, so a reader can trust them without paying for runs. Method: render each
ledger into a throwaway root and compare with the saved text; a planted twin flips one recorded
result and must change the output."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from lib.cli import EVALS_ROOT
from lib.report import EXIT_OK, run_report
from lib.skill_report import run_skill_report

EXAMPLES = EVALS_ROOT / "examples"
SKILL_ARGS = ["skill/tiger-style", "--model", "claude-haiku-5-5"]
SHELL_MODEL = "claude-haiku-5.5"  # these runs went through the Kiro engine


def make_root(tmp: Path, ledger: str, flip_last: bool = False) -> Path:
    """A dataset root holding the shipped limits and one example ledger."""
    source = EXAMPLES / "ledger" / ledger
    assert source.is_file(), source
    root = tmp / "root"
    (root / "ledger").mkdir(parents=True)
    shutil.copy(EVALS_ROOT / "limits.json", root / "limits.json")
    lines = source.read_text(encoding="utf-8").splitlines()
    if flip_last:  # the planted twin; the last line, since a later rerun replaces an earlier
        row: dict[str, object] = json.loads(lines[-1])
        row["case_passed"] = not row["case_passed"]
        lines[-1] = json.dumps(row)
    (root / "ledger" / ledger).write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert len(lines) > 0, ledger
    return root


def render_skill(tmp: Path, flip_last: bool = False) -> str:
    root = make_root(tmp, "skill_tiger-style.jsonl", flip_last)
    lines, code = run_skill_report(SKILL_ARGS, root)
    assert code == EXIT_OK, lines
    return "\n".join(lines) + "\n"


def render_shell(tmp: Path, flip_last: bool = False) -> str:
    root = make_root(tmp, "steering_shell.jsonl", flip_last)
    home = tmp / "home"  # no rule file: the size columns print ? on every machine
    home.mkdir()
    argv = ["steering/shell", "--model", SHELL_MODEL, "--home", str(home)]
    lines, code = run_report(argv, root, home)
    assert code == EXIT_OK, lines
    return "\n".join(lines) + "\n"


class Examples(unittest.TestCase):
    def saved(self, name: str) -> str:
        return (EXAMPLES / name).read_text(encoding="utf-8")

    def test_skill_report_matches_its_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(render_skill(Path(tmp)), self.saved("skill_tiger-style.report.txt"))

    def test_shell_report_matches_its_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(render_shell(Path(tmp)), self.saved("steering_shell.report.txt"))

    def test_a_flipped_result_changes_each_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill = render_skill(Path(tmp) / "a", flip_last=True)
            shell = render_shell(Path(tmp) / "b", flip_last=True)
        self.assertNotEqual(skill, self.saved("skill_tiger-style.report.txt"))
        self.assertNotEqual(shell, self.saved("steering_shell.report.txt"))


if __name__ == "__main__":
    unittest.main()
