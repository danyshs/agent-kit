"""Goal: each arm builds exactly the home its definition says (3.3) and nothing else.
Method: a throwaway source home from helpers.build_tree; assert on the files that land in dest."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lib.dataset import Arm, Target
from lib.home import HomeDir, build_home, plan_home, tree_sha
from tests.helpers import build_tree

ALWAYS_ON = frozenset({"rule-a", "rule-c"})


class PlanHomeTest(unittest.TestCase):
    skill = Target("alpha", "skill")
    steering = Target("rule-a", "steering")

    def test_skill_arms(self) -> None:
        with_ = plan_home(Arm("with", None), self.skill, ALWAYS_ON, ("rule-a",), ("beta",))
        without = plan_home(
            Arm("without", None), self.skill, ALWAYS_ON, ("rule-a",), ("alpha", "beta")
        )
        previous = plan_home(Arm("previous", None), self.skill, ALWAYS_ON, (), ())
        assert not isinstance(with_, str) and not isinstance(without, str)
        assert not isinstance(previous, str)
        self.assertEqual(with_.skills, ("alpha", "beta"))
        self.assertEqual(without.skills, ("beta",), "without drops the target even when listed")
        self.assertEqual(without.steering, ("rule-a",))
        self.assertEqual((previous.skills, previous.snapshot_skill), (("alpha",), "alpha"))

    def test_steering_arms(self) -> None:
        full = plan_home(Arm("full", None), self.steering, ALWAYS_ON, ("rule-b",), ("alpha",))
        minus = plan_home(Arm("minus", "rule-a"), self.steering, ALWAYS_ON, ("rule-b",), ("alpha",))
        assert not isinstance(full, str) and not isinstance(minus, str)
        self.assertEqual(full.steering, ("rule-a", "rule-b", "rule-c"))
        self.assertEqual(minus.steering, ("rule-b", "rule-c"))
        self.assertEqual(minus.skills, ("alpha",), "extra_skills stay in every non-none arm")

    def test_none_is_empty_even_with_extras(self) -> None:
        plan = plan_home(Arm("none", None), self.steering, ALWAYS_ON, ("rule-b",), ("alpha",))
        self.assertEqual(plan, plan_home(Arm("none", None), self.skill, ALWAYS_ON, ("x",), ("y",)))
        assert not isinstance(plan, str)
        self.assertEqual((plan.skills, plan.steering, plan.snapshot_skill), ((), (), None))

    def test_arm_target_mismatch_is_a_reason(self) -> None:
        self.assertIsInstance(plan_home(Arm("full", None), self.skill, ALWAYS_ON, (), ()), str)
        self.assertIsInstance(plan_home(Arm("with", None), self.steering, ALWAYS_ON, (), ()), str)

    def test_skill_instead_moves_the_target_from_the_rules_to_a_skill(self) -> None:
        arm = Arm("skill-instead", None)
        plan = plan_home(arm, self.steering, ALWAYS_ON, ("rule-b",), ("alpha",))
        full = plan_home(Arm("full", None), self.steering, ALWAYS_ON, ("rule-b",), ("alpha",))
        assert not isinstance(plan, str) and not isinstance(full, str)
        self.assertEqual(plan.steering, ("rule-b", "rule-c"))
        self.assertEqual((plan.skills, plan.rule_skill), (("alpha",), "rule-a"))
        self.assertIsNone(full.rule_skill)

    def test_skill_instead_on_a_target_it_cannot_move_is_a_reason(self) -> None:
        arm = Arm("skill-instead", None)
        for label, target, skills in (
            ("skill target", self.skill, ()),
            ("tool group target", Target("network", "tool_group"), ()),
            ("rule the full arm does not load", Target("rule-b", "steering"), ()),
            ("name taken by a skill", self.steering, ("rule-a",)),
        ):
            with self.subTest(label):
                self.assertIsInstance(plan_home(arm, target, ALWAYS_ON, (), skills), str)


class BuildHomeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src, _root = build_tree(Path(self.tmp.name))
        self.dest = Path(self.tmp.name) / "home"
        self.dest.mkdir()

    def build(
        self, arm: Arm, target: Target, snapshot: Path | None = None, files_max: int = 50
    ) -> HomeDir | str:
        return build_home(
            arm,
            target,
            snapshot,
            (),
            (),
            src_home=self.src,
            dest=self.dest,
            always_on=frozenset({"rule-a"}),
            files_max=files_max,
        )

    def test_full_copies_only_always_on_and_never_hooks_or_mcp(self) -> None:
        built = self.build(Arm("full", None), Target("rule-a", "steering"))
        assert isinstance(built, HomeDir)
        names = sorted(p.name for p in self.dest.rglob("*") if p.is_file())
        self.assertEqual(names, ["rule-a.md"], "the manual rule-b and the skill stay out")
        self.assertFalse((self.dest / "hooks").exists() or (self.dest / "mcp.json").exists())

    def test_with_and_without_differ_by_exactly_the_target(self) -> None:
        with_ = self.build(Arm("with", None), Target("alpha", "skill"))
        assert isinstance(with_, HomeDir)
        self.assertTrue((self.dest / "skills" / "alpha" / "SKILL.md").is_file())
        other = Path(self.tmp.name) / "home2"
        other.mkdir()
        without = build_home(
            Arm("without", None),
            Target("alpha", "skill"),
            None,
            (),
            (),
            src_home=self.src,
            dest=other,
            always_on=frozenset(),
            files_max=50,
        )
        assert isinstance(without, HomeDir)
        self.assertFalse((other / "skills").exists())
        self.assertNotEqual(with_.home_sha, without.home_sha)

    def test_previous_uses_the_snapshot_not_the_source(self) -> None:
        snap = Path(self.tmp.name) / "snap"
        snap.mkdir()
        (snap / "SKILL.md").write_text("---\nname: alpha\n---\nOLD\n", encoding="utf-8")
        built = self.build(Arm("previous", None), Target("alpha", "skill"), snapshot=snap)
        assert isinstance(built, HomeDir)
        self.assertIn(
            "OLD", (self.dest / "skills" / "alpha" / "SKILL.md").read_text(encoding="utf-8")
        )
        retry = Path(self.tmp.name) / "home-retry"
        retry.mkdir()
        missing = build_home(
            Arm("previous", None),
            Target("alpha", "skill"),
            None,
            (),
            (),
            src_home=self.src,
            dest=retry,
            always_on=frozenset(),
            files_max=50,
        )
        self.assertIsInstance(missing, str, "previous without a snapshot is a reason")

    def test_home_sha_is_content_addressed(self) -> None:
        first = tree_sha(self.src, 50)
        assert first is not None
        self.assertEqual(first, tree_sha(self.src, 50))
        (self.src / "steering" / "rule-a.md").write_text("changed", encoding="utf-8")
        self.assertNotEqual(first, tree_sha(self.src, 50))

    def test_file_bound_returns_a_reason(self) -> None:
        for number in range(5):
            (self.src / "skills" / "alpha" / f"extra{number}.md").write_text("x", encoding="utf-8")
        result = self.build(Arm("with", None), Target("alpha", "skill"), files_max=3)
        self.assertIsInstance(result, str)
        self.assertIsNone(tree_sha(self.src / "skills" / "alpha", 3))

    def test_missing_steering_source_is_a_reason(self) -> None:
        (self.src / "steering" / "rule-a.md").unlink()
        self.assertIsInstance(self.build(Arm("full", None), Target("rule-a", "steering")), str)

    def build_rule_skill(self, description: str | None, dest: Path | None = None) -> HomeDir | str:
        return build_home(
            Arm("skill-instead", None),
            Target("rule-a", "steering"),
            None,
            ("rule-b",),
            (),
            src_home=self.src,
            dest=self.dest if dest is None else dest,
            always_on=frozenset({"rule-a"}),
            files_max=50,
            plugin=True,
            claude_md=True,
            rule_skill_description=description,
        )

    def test_skill_instead_writes_the_rule_as_a_skill_and_not_into_claude_md(self) -> None:
        built = self.build_rule_skill("Use when writing rules.")
        assert isinstance(built, HomeDir)
        skill = (self.dest / "skills" / "rule-a" / "SKILL.md").read_text(encoding="utf-8")
        expected = '---\nname: rule-a\ndescription: "Use when writing rules."\n---\n\n# rule a\n'
        self.assertEqual(skill, expected, "the Kiro frontmatter is dropped, the body kept")
        claude_md = (self.dest / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("# rule b", claude_md)
        self.assertNotIn("# rule a", claude_md)
        self.assertFalse((self.dest / "steering" / "rule-a.md").exists())

    def test_a_rule_without_closed_frontmatter_is_copied_whole(self) -> None:
        for label, text in (("plain", "# plain\n"), ("unclosed", "---\ninclusion: always\n# x\n")):
            with self.subTest(label):
                (self.src / "steering" / "rule-a.md").write_text(text, encoding="utf-8")
                dest = Path(self.tmp.name) / f"home-{label}"
                dest.mkdir()
                self.assertIsInstance(self.build_rule_skill("Use it.", dest), HomeDir)
                skill = (dest / "skills" / "rule-a" / "SKILL.md").read_text(encoding="utf-8")
                self.assertTrue(skill.endswith(f'"Use it."\n---\n\n{text}'), skill)

    def test_skill_instead_without_a_description_or_a_source_is_a_reason(self) -> None:
        for number, description in enumerate((None, "")):
            dest = Path(self.tmp.name) / f"home-blank{number}"
            dest.mkdir()
            self.assertIsInstance(self.build_rule_skill(description, dest), str)
            self.assertFalse((dest / "skills").exists())
        (self.src / "steering" / "rule-a.md").unlink()
        self.assertIsInstance(self.build_rule_skill("Use it."), str)


if __name__ == "__main__":
    unittest.main()
