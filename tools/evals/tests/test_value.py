"""Goal: the V2 plumbing (design 6.3, 3.3, 4) does what it says: one prompt per arm, the three
tiger-style arms build the homes they name, a value suite loads and a malformed one is rejected,
and the planted-file grader scores the outside-checklist defects apart. Method: throwaway trees,
assert on rejection reasons, home contents and the argv a fake kiro-cli receives."""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path

from lib.cli import EVALS_ROOT
from lib.dataset import (
    VALUE_ARMS,
    Arm,
    Case,
    Limits,
    Target,
    World,
    load_cases,
    parse_arm,
)
from lib.grade import GRADERS, Finding, Planted, _planted_verdict  # pyright: ignore[reportPrivateUsage]  # pinned directly; not part of the grader API
from lib.home import HomeDir, build_home, plan_home
from lib.report import run_spend
from lib.run import prompts_for
from lib.run_cli import parse_run_args
from tests.helpers import assertion, build_tree, skill_case, world_and_limits, write_json
from tests.helpers_run import make_view
from tests.test_run import make_case
from tests.test_skill_report import MODEL, row, write_ledger

SKILL = Target("tiger-style", "skill")
OTHERS = ("rule-a",)  # the "other steering" every V2 arm keeps
RULE = "tiger-style-on-branches"
ALWAYS = "---\ninclusion: always\n---\n\n# rule\n"
SKILL_MD = "---\nname: {name}\ndescription: A skill.\n---\n\n# {name}\n"


def value_case(case_id: str = "v1", **over: object) -> dict[str, object]:
    prompts = {"none-ts": "task", "skill": "Apply tiger style. task", "skill+rule": "branch task"}
    case: dict[str, object] = {
        **skill_case(case_id),
        "target": "tiger-style",
        "arms": list(VALUE_ARMS),
        "arm_prompts": prompts,
        "assertions": [assertion("g_skill_read", {"skill": "tiger-style"})],
    }
    del case["prompt"]
    case.update(over)
    return case


class ValueTreeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.kiro_home, self.root = build_tree(Path(self._tmp.name))
        for name in ("tiger-style", "ponytail"):
            (self.kiro_home / "skills" / name).mkdir()
            text = SKILL_MD.format(name=name)
            (self.kiro_home / "skills" / name / "SKILL.md").write_text(text, encoding="utf-8")
        (self.kiro_home / "steering" / f"{RULE}.md").write_text(ALWAYS, encoding="utf-8")
        self.world: World
        self.limits: Limits
        self.world, self.limits = world_and_limits(self.kiro_home, self.root)

    def load(self, cases: Sequence[object], suite_kind: str = "value") -> list[str]:
        path = (
            self.root
            / ("value" if suite_kind == "value" else "skills")
            / ("v2" if suite_kind == "value" else "alpha")
            / "cases.json"
        )
        write_json(path, cases)
        loaded, rejections = load_cases(path, suite_kind, frozenset(), self.world, self.limits)
        reasons = [r.reason for r in rejections]
        self.assertEqual(bool(loaded), not reasons, "all or nothing")
        return reasons

    def arm(self, text: str) -> Arm:
        parsed = parse_arm(text, self.world)
        assert isinstance(parsed, Arm), parsed
        return parsed

    def home_parts(
        self, arm_text: str, extra_steering: tuple[str, ...] = OTHERS
    ) -> tuple[set[str], set[str]]:
        plan = plan_home(
            self.arm(arm_text), SKILL, self.world.always_on_names, extra_steering, ("ponytail",)
        )
        assert not isinstance(plan, str), plan
        return set(plan.skills), set(plan.steering)


class ValueArmsTest(ValueTreeTest):
    def test_none_ts_has_neither_the_skill_nor_the_rule_but_keeps_the_rest(self) -> None:
        self.assertEqual(self.home_parts("none-ts"), ({"ponytail"}, {"rule-a"}))

    def test_none_ts_drops_the_rule_even_when_the_case_lists_it(self) -> None:
        self.assertEqual(
            self.home_parts("none-ts", (*OTHERS, RULE)), ({"ponytail"}, {"rule-a"}), "twin below"
        )
        self.assertEqual(self.home_parts("skill+rule", (*OTHERS, RULE))[1], {"rule-a", RULE})

    def test_skill_adds_only_the_skill(self) -> None:
        self.assertEqual(self.home_parts("skill"), ({"ponytail", "tiger-style"}, {"rule-a"}))

    def test_skill_plus_rule_adds_both(self) -> None:
        self.assertEqual(
            self.home_parts("skill+rule"), ({"ponytail", "tiger-style"}, {"rule-a", RULE})
        )

    def test_value_arms_fit_only_the_tiger_style_skill(self) -> None:
        for target in (Target("ponytail", "skill"), Target("rule-a", "steering")):
            plan = plan_home(self.arm("skill"), target, frozenset(), (), ())
            self.assertIsInstance(plan, str)

    def test_built_homes_hold_exactly_the_planned_files(self) -> None:
        for text, want_skill, want_rule in (
            ("none-ts", False, False),
            ("skill", True, False),
            ("skill+rule", True, True),
        ):
            with self.subTest(text), tempfile.TemporaryDirectory() as dest:
                built = build_home(
                    self.arm(text),
                    SKILL,
                    None,
                    OTHERS,
                    ("ponytail",),
                    src_home=self.kiro_home,
                    dest=Path(dest),
                    always_on=self.world.always_on_names,
                    files_max=64,
                )
                self.assertIsInstance(built, HomeDir)
                self.assertEqual((Path(dest) / "skills" / "tiger-style").is_dir(), want_skill)
                self.assertEqual((Path(dest) / "steering" / f"{RULE}.md").is_file(), want_rule)
                self.assertTrue((Path(dest) / "skills" / "ponytail" / "SKILL.md").is_file())
                self.assertTrue((Path(dest) / "steering" / "rule-a.md").is_file())

    def test_the_three_homes_hash_differently(self) -> None:
        shas: set[str] = set()
        for text in VALUE_ARMS:
            with tempfile.TemporaryDirectory() as dest:
                built = build_home(
                    self.arm(text),
                    SKILL,
                    None,
                    OTHERS,
                    ("ponytail",),
                    src_home=self.kiro_home,
                    dest=Path(dest),
                    always_on=self.world.always_on_names,
                    files_max=64,
                )
                assert isinstance(built, HomeDir), built
                shas.add(built.home_sha)
        self.assertEqual(len(shas), 3)


class ArmPromptsTest(ValueTreeTest):
    def test_a_valid_value_case_loads_with_one_prompt_per_arm(self) -> None:
        path = self.root / "value" / "v2" / "cases.json"
        write_json(path, [value_case()])
        cases, rejections = load_cases(path, "value", frozenset(), self.world, self.limits)
        self.assertEqual(rejections, [])
        self.assertEqual(dict(cases[0].arm_prompts)["skill"], "Apply tiger style. task")
        self.assertIsNone(cases[0].prompt)

    def test_prompts_for_returns_the_arm_prompt_only(self) -> None:
        path = self.root / "value" / "v2" / "cases.json"
        write_json(path, [value_case()])
        case = load_cases(path, "value", frozenset(), self.world, self.limits)[0][0]
        got = {a.kind: prompts_for(case, a) for a in case.arms}
        self.assertEqual(got["none-ts"], ("task",))
        self.assertEqual(got["skill+rule"], ("branch task",))
        self.assertEqual(prompts_for(make_case(), Arm("full", None)), ("do the thing",))

    def test_missing_extra_and_misnamed_arm_prompts_are_rejected(self) -> None:
        good = {"none-ts": "a", "skill": "b", "skill+rule": "c"}
        bad_sets: list[dict[str, str]] = [
            {"none-ts": "a", "skill": "b"},
            {**good, "with": "d"},
            {"none-ts": "a", "skill": "b", "skill-rule": "c"},
        ]
        for prompts in bad_sets:
            with self.subTest(prompts):
                reasons = self.load([value_case(arm_prompts=prompts)])
                self.assertIn("must be exactly the arms", reasons[0])

    def test_prompt_and_arm_prompts_together_are_rejected(self) -> None:
        reasons = self.load([value_case(prompt="x")])
        self.assertIn("exactly one of prompt, user_turns and arm_prompts", reasons[0])

    def test_empty_or_oversize_arm_prompts_are_rejected(self) -> None:
        big = "x" * (self.limits.prompt_bytes_max + 1)
        for prompts in ({"none-ts": " ", "skill": "b", "skill+rule": "c"}, {"none-ts": big}, {}):
            with self.subTest(list(prompts)):
                reasons = self.load([value_case(arm_prompts=prompts)])
                self.assertIn("arm_prompts must map arms", reasons[0])

    def test_arm_prompts_work_for_ordinary_arms_too(self) -> None:
        case = skill_case(arm_prompts={"with": "a", "without": "b"})
        del case["prompt"]
        self.assertEqual(self.load([case], "skill"), [])


class ValueSuiteTest(ValueTreeTest):
    def test_value_arms_must_all_be_present_and_aim_at_tiger_style(self) -> None:
        cases: list[dict[str, object]] = [
            value_case(arms=["none-ts", "skill"], arm_prompts={"none-ts": "a", "skill": "b"}),
            value_case(target="ponytail"),
            value_case(target_kind="steering", target="rule-a", rule_id="r1"),
        ]
        for case in cases:
            with self.subTest(case["target"], kinds=case["arms"]):
                self.assertTrue(self.load([case]))
        self.assertIn("go together", self.load([cases[0]])[0])

    def test_value_arms_are_rejected_in_an_ordinary_skill_suite(self) -> None:
        reasons = self.load([value_case(target="alpha")], "skill")
        self.assertTrue(reasons)

    def test_a_value_suite_holds_skill_cases_only(self) -> None:
        reasons = self.load([skill_case(target="ghost")])
        self.assertIn("value suite holds skill cases", reasons[0])

    def test_run_args_accept_the_value_kind(self) -> None:
        parsed = parse_run_args(["value/v2", "--arm", "skill+rule"], Path("/home/x/.kiro"))
        assert not isinstance(parsed, str), parsed
        self.assertEqual(
            (parsed.suite_kind, parsed.suite_name, parsed.arm), ("value", "v2", "skill+rule")
        )
        self.assertIsInstance(parse_run_args(["bogus/v2"], Path("/home/x/.kiro")), str)


class OutsideRecallTest(unittest.TestCase):
    ITEMS = [
        Planted("p1", "a.py", 10, True, ("P1",)),
        Planted("p4", "a.py", 40, False, ("P1",)),
        Planted("p5", "a.py", 60, False, ("P0",)),
    ]

    def verdict(self, lines: list[int], minimum: float) -> tuple[str, str]:
        findings = [Finding("P1", "a.py", n, "x") for n in lines]
        return _planted_verdict("recall_outside", minimum, findings, self.ITEMS, [])

    def test_counts_only_the_items_outside_the_checklist(self) -> None:
        self.assertEqual(self.verdict([10], 0.5)[0], "FAIL", "the in-category hit does not count")
        verdict, evidence = self.verdict([40], 0.5)
        self.assertEqual(verdict, "PASS")
        self.assertIn("1/2", evidence)
        self.assertEqual(self.verdict([40, 60], 1.0)[0], "PASS")
        self.assertEqual(self.verdict([40], 1.0)[0], "FAIL")

    def test_no_outside_items_is_no_evidence(self) -> None:
        verdict, _ = _planted_verdict("recall_outside", 0.5, [], self.ITEMS[:1], [])
        self.assertEqual(verdict, "FAIL")

    def test_the_grader_table_reads_the_mode_from_a_planted_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(
                root / "planted" / "f.json",
                {
                    "items": [
                        {
                            "id": "p4",
                            "file": "a.py",
                            "line": 5,
                            "in_tiger_style_categories": False,
                            "allowed_severities": ["P1"],
                        }
                    ]
                },
            )
            view = make_view([], "FINDING P1 a.py:5 bug\nFINDINGS_DONE\n")
            args: dict[str, object] = {
                "planted": "planted/f.json",
                "mode": "recall_outside",
                "min": 1.0,
            }
            self.assertEqual(GRADERS["g_planted_found"](view, args, root)[0], "PASS")


class ValueSpendTest(unittest.TestCase):
    """The credit guard works on a value ledger and is seen to trip (debugging-discipline #7)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "limits.json").write_text(
            (EVALS_ROOT / "limits.json").read_text(encoding="utf-8"), encoding="utf-8"
        )

    def spend(self, ceiling: str) -> tuple[list[str], int]:
        return run_spend(["value/v2", "--model", MODEL, "--max", ceiling], self.root)

    def test_over_trips_under_does_not_and_a_missing_ledger_stops(self) -> None:
        self.assertEqual(self.spend("1")[1], 1, "no ledger: a guard that cannot measure stops")
        write_ledger(self.root, "value_v2.jsonl", [row("v2-w-py-batch", "skill+rule", credits=0.3)])
        lines, code = self.spend("0.2")
        self.assertEqual(code, 1)
        self.assertIn("over=true", lines[0])
        lines, code = self.spend("0.4")
        self.assertEqual(code, 0)
        self.assertIn("over=false", lines[0])

    def test_unknown_suite_kind_is_a_usage_error(self) -> None:
        argv = ["bogus/v2", "--model", "claude-haiku-5.5", "--max", "1"]
        self.assertEqual(run_spend(argv, self.root)[1], 2)


class CaseFieldTest(unittest.TestCase):
    def test_case_default_has_no_arm_prompts(self) -> None:
        case: Case = make_case()
        self.assertEqual(case.arm_prompts, ())
