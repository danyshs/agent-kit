"""Goal: static lint flags each defect it claims to, the baseline only hides what it lists, and
`bin/eval static` prints one line per target plus a status line with empty stderr.
Method: plant one defect per test in a throwaway tree; the real-home test runs the actual
wrapper and asserts on its output text, never on the exit code alone (debugging-discipline #5)."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

from lib.cli import EVALS_ROOT, main, parse_args, run_static
from lib.dataset import (
    LintFinding,
    lint_all,
    lint_skill,
    lint_steering,
    load_lint_baseline,
    split_baseline,
)
from tests.helpers import rule, steering_case, write_json
from tests.test_dataset import TreeTest


class LintTest(TreeTest):
    def skill_findings(self, text: str) -> list[tuple[str, str]]:
        skill_dir = self.kiro_home / "skills" / "alpha"
        (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")
        return [(f.rule, f.message) for f in lint_skill(skill_dir, self.limits)]

    def test_clean_skill_has_no_findings(self) -> None:
        self.assertEqual(self.skill_findings("---\nname: alpha\ndescription: ok\n---\n"), [])

    def test_each_skill_defect_is_found(self) -> None:
        long_desc = "d" * (self.limits.description_chars_max + 1)
        cases = {
            "name mismatch": ("---\nname: beta\ndescription: ok\n---\n", "name"),
            "no description": ("---\nname: alpha\n---\n", "description"),
            "description too long": (
                f"---\nname: alpha\ndescription: {long_desc}\n---\n",
                "description-length",
            ),
            "bad frontmatter": ("no fence\n", "frontmatter"),
            "missing reference": (
                "---\nname: alpha\ndescription: ok\n---\nsee references/nope.md.\n",
                "missing-reference",
            ),
            "over byte budget": (
                "---\nname: alpha\ndescription: ok\n---\n" + "x" * self.limits.skill_bytes_max,
                "byte-budget",
            ),
        }
        for label, (text, rule_name) in cases.items():
            with self.subTest(label):
                self.assertIn(rule_name, [r for r, _ in self.skill_findings(text)])

    def test_skill_byte_budget_is_inclusive(self) -> None:
        # exactly at the limit passes, one byte over fails
        head = "---\nname: alpha\ndescription: ok\n---\n"
        at_limit = head + "x" * (self.limits.skill_bytes_max - len(head))
        self.assertEqual(self.skill_findings(at_limit), [])
        rules = [r for r, _ in self.skill_findings(at_limit + "x")]
        self.assertEqual(rules, ["byte-budget"])

    def test_existing_reference_is_not_a_finding(self) -> None:
        ref = self.kiro_home / "skills" / "alpha" / "references"
        ref.mkdir()
        (ref / "yes.md").write_text("x", encoding="utf-8")
        text = (
            "---\nname: alpha\ndescription: ok\n---\nsee references/yes.md and references/<x>.md\n"
        )
        self.assertEqual(self.skill_findings(text), [])

    def test_reference_boundary_is_exact_message(self) -> None:
        text = "---\nname: alpha\ndescription: ok\n---\nsee `agents/zoom.md`.\n"
        self.assertEqual(self.skill_findings(text), [("missing-reference", "agents/zoom.md")])

    def test_steering_defects(self) -> None:
        path = self.kiro_home / "steering" / "rule-b.md"
        path.write_text("---\ninclusion: sometimes\n---\n", encoding="utf-8")
        self.assertEqual([f.rule for f in lint_steering(path, self.limits)], ["inclusion"])
        path.write_text("# no frontmatter\n", encoding="utf-8")
        self.assertEqual([f.rule for f in lint_steering(path, self.limits)], ["frontmatter"])
        path.write_text(
            "---\ninclusion: manual\n---\n" + "x" * self.limits.steering_bytes_max, "utf-8"
        )
        self.assertEqual([f.rule for f in lint_steering(path, self.limits)], ["byte-budget"])

    def test_always_on_budget_counts_only_always_files(self) -> None:
        # rule-b is manual: growing it must not trip the always-on total
        path_b = self.kiro_home / "steering" / "rule-b.md"
        path_b.write_text(
            "---\ninclusion: manual\n---\n" + "x" * self.limits.always_on_bytes_max, "utf-8"
        )
        rules = {f.rule for f in lint_all(self.kiro_home, self.world, self.limits)}
        self.assertNotIn("always-on-budget", rules)
        path_a = self.kiro_home / "steering" / "rule-a.md"
        path_a.write_text(
            "---\ninclusion: always\n---\n" + "x" * self.limits.always_on_bytes_max, "utf-8"
        )
        rules = {f.rule for f in lint_all(self.kiro_home, self.world, self.limits)}
        self.assertIn("always-on-budget", rules)

    def test_lint_paths_are_relative_to_kiro_home(self) -> None:
        (self.kiro_home / "skills" / "alpha" / "SKILL.md").write_text("bad", encoding="utf-8")
        found = lint_all(self.kiro_home, self.world, self.limits)
        self.assertEqual(found[0].path_text, "skills/alpha/SKILL.md")


class BaselineTest(unittest.TestCase):
    def test_split_hides_only_exact_matches(self) -> None:
        known = LintFinding("skills/a/SKILL.md", "missing-reference", "agents/x.md")
        other_message = LintFinding("skills/a/SKILL.md", "missing-reference", "agents/y.md")
        other_path = LintFinding("skills/b/SKILL.md", "missing-reference", "agents/x.md")
        baseline = frozenset({("missing-reference", "skills/a/SKILL.md", "agents/x.md")})
        new, old = split_baseline([known, other_message, other_path], baseline)
        self.assertEqual(old, [known])
        self.assertEqual(new, [other_message, other_path])

    def test_baseline_file_shape_is_validated(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.json"
            write_json(path, [{"rule": "r", "path": "p"}])
            with self.assertRaises(ValueError):
                load_lint_baseline(path)
            write_json(path, [{"rule": "r", "path": "p", "message": "m"}])
            self.assertEqual(load_lint_baseline(path), frozenset({("r", "p", "m")}))


class StaticRunTest(TreeTest):
    def test_status_line_and_one_row_per_target(self) -> None:
        lines, code = run_static(self.kiro_home, self.root)
        targets = 1 + 2 + len(self.world.tool_groups)
        self.assertEqual(
            len([ln for ln in lines if ln.startswith(("skill", "steering", "tool_group"))]), targets
        )
        self.assertEqual(
            lines[-1],
            f"static: targets={targets} covered=0 uncovered={targets} human_only=0 "
            "lint_new=0 lint_baseline=0 rejections=0",
        )
        self.assertEqual(code, 0)

    def test_covered_row_and_count(self) -> None:
        write_json(
            self.root / "steering" / "rule-a" / "cases.json",
            [steering_case("c1", "fire"), steering_case("c2", "no_fire")],
        )
        write_json(self.root / "steering" / "rule-a" / "rules.json", [rule()])
        lines, _ = run_static(self.kiro_home, self.root)
        rows = [ln for ln in lines if ln.split()[:2] == ["steering", "rule-a"]]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].split()[2:], ["covered"])
        self.assertIn("covered=1", lines[-1])

    def test_rejection_and_new_lint_fail_the_run(self) -> None:
        write_json(self.root / "steering" / "rule-a" / "cases.json", [{"id": "x"}])
        lines, code = run_static(self.kiro_home, self.root)
        self.assertEqual(code, 1)
        self.assertIn("rejections=1", lines[-1])

    def test_baselined_lint_does_not_fail(self) -> None:
        (self.kiro_home / "skills" / "alpha" / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: ok\n---\nsee agents/q.md\n", encoding="utf-8"
        )
        _, code = run_static(self.kiro_home, self.root)
        self.assertEqual(code, 1)
        write_json(
            self.root / "lint_baseline.json",
            [
                {
                    "rule": "missing-reference",
                    "path": "skills/alpha/SKILL.md",
                    "message": "agents/q.md",
                }
            ],
        )
        lines, code = run_static(self.kiro_home, self.root)
        self.assertEqual(code, 0)
        self.assertIn("lint_new=0 lint_baseline=1", lines[-1])


class ArgsTest(unittest.TestCase):
    def test_parse_args(self) -> None:
        self.assertIsInstance(parse_args([]), str)
        self.assertIsInstance(parse_args(["run"]), str)
        self.assertIsInstance(parse_args(["static", "--kiro-home"]), str)
        self.assertEqual(parse_args(["static", "--kiro-home", "/x"]), Path("/x"))

    def test_main_usage_error_goes_to_stderr_with_code_2(self) -> None:
        import contextlib
        import io

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main(["bogus"])
        self.assertEqual(code, 2)
        self.assertIn("usage:", err.getvalue())


class RealHomeTest(unittest.TestCase):
    """The done-gate: the real wrapper on the real ~/.claude (the default) and ~/.kiro."""

    def check_home(self, home: Path, argv: list[str]) -> None:
        done = subprocess.run(
            [str(EVALS_ROOT / "bin" / "eval"), "static", *argv],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(done.stderr, "")
        lines = done.stdout.splitlines()
        self.assertTrue(lines[-1].startswith("static: targets="), lines[-1])
        skills = sorted(p.parent.name for p in (home / "skills").glob("*/SKILL.md"))
        steering = sorted(p.stem for p in (home / "steering").glob("*.md"))
        self.assertTrue(skills and steering, f"{home} has skills and steering to list")
        for kind, names in (("skill", skills), ("steering", steering)):
            for name in names:
                rows = [ln for ln in lines if ln.split()[:2] == [kind, name]]
                self.assertEqual(len(rows), 1, (kind, name))
                self.assertRegex(rows[0], r"(covered|uncovered)")

    @unittest.skipUnless(Path("~/.claude/CLAUDE.md").expanduser().is_file(), "no ~/.claude here")
    def test_static_on_the_claude_home(self) -> None:
        self.check_home(Path("~/.claude").expanduser(), [])

    @unittest.skipUnless(Path("~/.kiro/skills").expanduser().is_dir(), "no ~/.kiro on this machine")
    def test_static_on_the_kiro_home(self) -> None:
        self.check_home(Path("~/.kiro").expanduser(), ["--home", str(Path("~/.kiro").expanduser())])


if __name__ == "__main__":
    unittest.main()
