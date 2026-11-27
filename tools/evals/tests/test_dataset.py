"""Goal: every validation rule in design section 4 and 3.5 fires on a planted bad input, and a
good input loads. Method: build a throwaway tree, write one cases.json, load it, assert on the
rejection reason text (a rule that cannot fail is broken). Real ~/.kiro is never read here."""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path

from lib.cli import run_static
from lib.dataset import (
    AS_SKILL_FILE,
    GRADER_ARGS,
    Arm,
    Case,
    Dataset,
    Limits,
    World,
    classify_coverage,
    collect_targets,
    load_cases,
    load_dataset,
    load_limits,
    load_rules,
    parse_arm,
    parse_frontmatter,
)
from tests.helpers import (
    assertion,
    build_tree,
    rule,
    skill_case,
    steering_case,
    world_and_limits,
    write_json,
)


class TreeTest(unittest.TestCase):
    """Base: a fresh tree per test; helpers to write suites and load them."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.kiro_home, self.root = build_tree(Path(self._tmp.name))
        self.world: World
        self.limits: Limits
        self.world, self.limits = world_and_limits(self.kiro_home, self.root)

    def write_steering(
        self, cases: Sequence[object], rules: Sequence[object] | None = None
    ) -> Path:
        suite = self.root / "steering" / "rule-a"
        write_json(suite / "rules.json", [rule()] if rules is None else rules)
        write_json(suite / "cases.json", cases)
        return suite / "cases.json"

    def load_steering(self, cases: Sequence[object]) -> tuple[tuple[Case, ...], list[str]]:
        path = self.write_steering(cases)
        loaded, rejections = load_cases(
            path, "steering", frozenset({"r1"}), self.world, self.limits
        )
        return loaded, [r.reason for r in rejections]

    def load_skill(self, cases: Sequence[object]) -> tuple[tuple[Case, ...], list[str]]:
        path = self.root / "skills" / "alpha" / "cases.json"
        write_json(path, cases)
        loaded, rejections = load_cases(path, "skill", frozenset(), self.world, self.limits)
        return loaded, [r.reason for r in rejections]

    def dataset(self) -> Dataset:
        return load_dataset(self.world, self.limits)


class FrontmatterTest(unittest.TestCase):
    def test_scalar_and_folded_block(self) -> None:
        # folded `>` descriptions are how ponytail and tiger-style write theirs
        text = "---\nname: x\ndescription: >\n  one\n  two\nlicense: MIT\n---\nbody\n"
        self.assertEqual(
            parse_frontmatter(text, 40), {"name": "x", "description": "one two", "license": "MIT"}
        )

    def test_errors_name_the_problem(self) -> None:
        cases = {
            "no opening": "name: x\n---\n",
            "no closing": "---\nname: x\n",
            "garbage line": "---\nnot a pair\n---\n",
            "indent without key": "---\n  stray\n---\n",
        }
        for label, text in cases.items():
            with self.subTest(label):
                self.assertIsInstance(parse_frontmatter(text, 40), str)

    def test_line_limit_is_a_boundary(self) -> None:
        body = "".join(f"k{i}: v\n" for i in range(5))
        text = f"---\n{body}---\n"
        # scan covers the lines after the opening fence, closing fence included: 5 keys + 1
        self.assertIsInstance(parse_frontmatter(text, 6), dict)
        self.assertIsInstance(parse_frontmatter(text, 5), str)


class LimitsTest(unittest.TestCase):
    def test_real_limits_load_and_are_positive(self) -> None:
        from lib.cli import EVALS_ROOT

        limits = load_limits(EVALS_ROOT / "limits.json")
        self.assertEqual(limits.cases_per_suite_max, 60)
        self.assertEqual(limits.skill_bytes_max, 24391)

    def test_bad_limit_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "limits.json"
            for label, value in {"zero": 0, "negative": -3, "bool": True, "text": "9"}.items():
                with self.subTest(label):
                    write_json(path, {"MAX_CASES_PER_SUITE": value})
                    with self.assertRaises(ValueError):
                        load_limits(path)


class WorldTest(TreeTest):
    def test_targets_come_from_disk(self) -> None:
        self.assertEqual(self.world.skill_names, frozenset({"alpha"}))
        self.assertEqual(self.world.steering_names, frozenset({"rule-a", "rule-b"}))
        self.assertEqual(self.world.always_on_names, frozenset({"rule-a"}))
        kinds = [t.kind for t in collect_targets(self.world)]
        self.assertEqual(kinds.count("tool_group"), len(self.world.tool_groups))

    def test_shell_is_trusted_as_run_command_on_v3(self) -> None:
        self.assertEqual(self.world.trust_names["execute_bash"], "run_command")
        self.assertLessEqual(set(self.world.trust_names), self.world.known_tools)

    def test_unreadable_steering_file_stops_discovery_with_value_error(self) -> None:
        (self.kiro_home / "steering" / "rule-b.md").write_bytes(b"\xff\xfe not utf-8")
        with self.assertRaisesRegex(ValueError, "cannot read"):
            world_and_limits(self.kiro_home, self.root)

    def test_arm_parsing(self) -> None:
        arm = parse_arm("minus-rule-a", self.world)
        self.assertIsInstance(arm, Arm)
        assert isinstance(arm, Arm)
        self.assertEqual((arm.kind, arm.minus_file), ("minus", "rule-a"))
        for text in ("minus-rule-b", "minus-nope", "bogus"):  # rule-b is manual, not always-on
            with self.subTest(text):
                self.assertIsInstance(parse_arm(text, self.world), str)


GOOD_ARGS: dict[str, dict[str, object]] = {
    "g_skill_read": {"skill": "alpha"},
    "g_skill_not_read": {"skill": "alpha"},
    "g_tool_used": {"tool": "read_file", "min_count": 2, "tool_group": "file_read_search"},
    "g_tool_not_used": {"tool": "execute_bash", "tool_group": "shell"},
    "g_tool_args_match": {"tool": "execute_bash", "arg_regex": "--repo", "expect": "present"},
    "g_calls_max": {"tool": None, "max": 3},
    "g_text_matches": {"regex": "^FINDING", "scope": "final_text"},
    "g_text_absent": {"regex": "bash -c", "scope": "write_inputs"},
    "g_fish_parses": {},
    "g_no_heredoc": {},
    "g_commit_message_clean": {"banned_regex": ["DO-NOT-SHIP"]},
    "g_planted_found": {"planted": "planted/fx.json", "mode": "recall", "min": 0.6},
    "g_clean_control_quiet": {},
    "g_citation_valid": {},
    "g_function_length_max": {"max_lines": 70},
    "g_assertions_per_function_min": {"min": 2},
    "g_loops_bounded": {},
    "g_units_in_names": {"names": [{"identifier": "timeout", "suffixes": ["_ms", "_s"]}]},
    "g_pyright_clean": {},
    "g_no_outside_read": {},
    "g_hidden_tests_pass": {"tests_dir": "hidden/task", "module": "solution"},
}


class CaseAcceptTest(TreeTest):
    def test_valid_steering_and_skill_cases_load(self) -> None:
        cases, reasons = self.load_steering([steering_case(), steering_case("c2", "no_fire")])
        self.assertEqual(reasons, [])
        self.assertEqual([c.id for c in cases], ["c1", "c2"])
        self.assertEqual(cases[0].arms[0].kind, "full")
        cases, reasons = self.load_skill([skill_case(fixture="skills/alpha/fixtures")])
        self.assertEqual((len(cases), reasons), (1, []))

    def test_user_turns_and_minus_arm_load(self) -> None:
        case = steering_case(
            prompt=None,
            user_turns=["one", "two"],
            arms=["full", "minus-rule-a"],
            tags=["regression"],
        )
        del case["prompt"]
        cases, reasons = self.load_steering([case])
        self.assertEqual(reasons, [])
        self.assertEqual(cases[0].user_turns, ("one", "two"))
        self.assertEqual(cases[0].arms[1].minus_file, "rule-a")

    def test_every_grader_accepts_a_matching_example(self) -> None:
        # a grader missing from GOOD_ARGS would silently go unexercised
        self.assertEqual(len(GRADER_ARGS), 22)
        self.assertEqual(set(GOOD_ARGS), set(GRADER_ARGS) - {"g_stderr_empty"})
        for grader, args in GOOD_ARGS.items():
            with self.subTest(grader):
                _, reasons = self.load_steering(
                    [steering_case(assertions=[assertion(grader, args)])]
                )
                self.assertEqual(reasons, [])

    def test_good_args_are_exact_schemas(self) -> None:
        # dropping any required arg, or adding one, must reject (schema is exact)
        for grader, args in GOOD_ARGS.items():
            required = [n for n, spec in GRADER_ARGS[grader].items() if spec.required]
            with self.subTest(grader):
                for name in required:
                    dropped = {k: v for k, v in args.items() if k != name}
                    _, reasons = self.load_steering(
                        [steering_case(assertions=[assertion(grader, dropped)])]
                    )
                    self.assertIn("missing arg", reasons[0])
                extra = {**args, "bogus": 1}
                _, reasons = self.load_steering(
                    [steering_case(assertions=[assertion(grader, extra)])]
                )
                self.assertIn("unknown args", reasons[0])


REJECT_CASES: list[tuple[str, dict[str, object], str]] = [
    ("unknown key", steering_case(extra=1), "unknown keys"),
    ("missing key", {k: v for k, v in steering_case().items() if k != "role"}, "missing keys"),
    ("bad id", steering_case(case_id="Bad ID"), "must match"),
    ("bad role", steering_case(role="sometimes"), "role one of"),
    ("steering in wrong suite", steering_case(target="rule-b"), "steering case must sit"),
    ("unknown rule_id", steering_case(rule_id="nope"), "names no Rule"),
    ("no prompt", {k: v for k, v in steering_case().items() if k != "prompt"}, "exactly one"),
    ("both prompt and turns", steering_case(user_turns=["a"]), "exactly one"),
    ("empty prompt", steering_case(prompt="  "), "prompt must be non-empty"),
    ("prompt over limit", steering_case(prompt="x" * 8193), "prompt must be non-empty"),
    ("turns over limit", steering_case(prompt=None, user_turns=["a"] * 21), "user_turns"),
    ("unknown tool", steering_case(trust_tools=["telepathy"]), "trust_tools"),
    ("v2 tool name", steering_case(trust_tools=["read"]), "trust_tools"),
    ("known but untrustable tool", steering_case(trust_tools=["web_fetch"]), "trust_tools"),
    ("duplicate tool", steering_case(trust_tools=["read_file", "read_file"]), "trust_tools"),
    ("steering arms lack full", steering_case(arms=["none", "minus-rule-a"]), "steering arms"),
    ("steering single arm", steering_case(arms=["full"]), "steering arms"),
    ("steering arm with", steering_case(arms=["full", "with"]), "steering arms"),
    ("minus manual file", steering_case(arms=["full", "minus-rule-b"]), "not an always-on"),
    ("duplicate arm", steering_case(arms=["full", "full"]), "unique"),
    ("skill-instead without full", steering_case(arms=["none", "skill-instead"]), "steering arms"),
    ("unknown extra skill", steering_case(extra_skills=["ghost"]), "extra_skills"),
    ("unknown extra steering", steering_case(extra_steering=["ghost"]), "extra_steering"),
    ("repeats too many", steering_case(repeats=6), "repeats"),
    ("repeats bool", steering_case(repeats=True), "repeats"),
    ("both tags", steering_case(tags=["capability", "regression"]), "tags"),
    ("bad tier", steering_case(tier="static"), "tier"),
    ("fixture escapes root", steering_case(fixture="../outside"), "fixture"),
    ("fixture in planted", steering_case(fixture="planted/fx.json"), "planted/ or hidden/"),
    ("fixture missing", steering_case(fixture="skills/alpha/nope"), "fixture"),
    ("no assertions", steering_case(assertions=[]), "non-empty list"),
    ("too many assertions", steering_case(assertions=[assertion()] * 13), "limit is 12"),
    ("duplicate assertion id", steering_case(assertions=[assertion(), assertion()]), "duplicate"),
    ("unknown grader", steering_case(assertions=[assertion("g_magic", {})]), "unknown grader"),
    ("static-only grader", steering_case(assertions=[assertion("g_stderr_empty", {})]), "static"),
    (
        "human must be soft",
        steering_case(assertions=[assertion("human", {"question": "Is the voice terse?"})]),
        "hard = false",
    ),
    (
        "only soft assertions",
        steering_case(
            assertions=[assertion("human", {"question": "Is the voice terse?"}, hard=False)]
        ),
        "at least one hard",
    ),
    (
        "banned vague phrase",
        steering_case(
            assertions=[
                assertion(),
                assertion(
                    "human", {"question": "Output looks good?"}, hard=False, assertion_id="h"
                ),
            ]
        ),
        "banned vague phrase",
    ),
    (
        "args unknown key",
        steering_case(assertions=[assertion(args={"tool": "read_file", "x": 1})]),
        "unknown args",
    ),
    ("args missing key", steering_case(assertions=[assertion(args={})]), "missing arg"),
    ("args wrong type", steering_case(assertions=[assertion(args={"tool": 7})]), "arg 'tool'"),
    ("args unknown tool", steering_case(assertions=[assertion(args={"tool": "x"})]), "known tool"),
    (
        "args min_count zero",
        steering_case(assertions=[assertion(args={"tool": "read_file", "min_count": 0})]),
        "integer >= 1",
    ),
    (
        "args min_count bool",
        steering_case(assertions=[assertion(args={"tool": "read_file", "min_count": True})]),
        "integer >= 1",
    ),
    (
        "args unknown tool_group",
        steering_case(assertions=[assertion(args={"tool": "read_file", "tool_group": "nope"})]),
        "known tool_group",
    ),
    (
        "args bad regex",
        steering_case(
            assertions=[assertion("g_text_matches", {"regex": "(", "scope": "final_text"})]
        ),
        "invalid regex",
    ),
    (
        "args bad enum",
        steering_case(assertions=[assertion("g_text_matches", {"regex": "a", "scope": "all"})]),
        "must be one of",
    ),
    (
        "args empty regex list",
        steering_case(assertions=[assertion("g_commit_message_clean", {"banned_regex": []})]),
        "regex strings",
    ),
    (
        "args fraction over 1",
        steering_case(
            assertions=[
                assertion(
                    "g_planted_found", {"planted": "planted/fx.json", "mode": "recall", "min": 1.5}
                )
            ]
        ),
        "[0, 1]",
    ),
    (
        "args planted outside planted/",
        steering_case(
            assertions=[
                assertion(
                    "g_planted_found",
                    {"planted": "limits.json", "mode": "recall", "min": 0.6},
                )
            ]
        ),
        "not under planted/",
    ),
    (
        "args units shape",
        steering_case(assertions=[assertion("g_units_in_names", {"names": [{"identifier": "t"}]})]),
        "identifier, suffixes",
    ),
    (
        "args unknown skill",
        steering_case(assertions=[assertion("g_skill_read", {"skill": "ghost"})]),
        "known skill",
    ),
    (
        "args tests_dir missing",
        steering_case(assertions=[assertion("g_hidden_tests_pass", {"tests_dir": "hidden/none"})]),
        "existing directory",
    ),
    # json lists/objects are unhashable: must reject, not raise TypeError
    ("target is a list", steering_case(target=["rule-a"]), "target must be"),
    (
        "args skill is a list",
        steering_case(assertions=[assertion("g_skill_read", {"skill": []})]),
        "known skill",
    ),
    (
        "args skill is an object",
        steering_case(assertions=[assertion("g_skill_read", {"skill": {}})]),
        "known skill",
    ),
    (
        "args tool is a list",
        steering_case(assertions=[assertion(args={"tool": []})]),
        "known tool",
    ),
    (
        "args tool is an object",
        steering_case(assertions=[assertion(args={"tool": {"a": 1}})]),
        "known tool",
    ),
    (
        "args tool_group is a list",
        steering_case(assertions=[assertion(args={"tool": "read_file", "tool_group": []})]),
        "known tool_group",
    ),
    (
        "args tool_or_null is a list",
        steering_case(assertions=[assertion("g_calls_max", {"tool": [], "max": 3})]),
        "known tool",
    ),
    (
        "args tool_or_null is an object",
        steering_case(assertions=[assertion("g_calls_max", {"tool": {}, "max": 3})]),
        "known tool",
    ),
]


class CaseRejectTest(TreeTest):
    def test_each_bad_case_is_rejected_with_its_reason(self) -> None:
        self.assertGreater(len(REJECT_CASES), 40)
        for label, case, expected in REJECT_CASES:
            with self.subTest(label):
                cases, reasons = self.load_steering([case])
                self.assertEqual(cases, ())
                self.assertEqual(len(reasons), 1, reasons)
                self.assertIn(expected, reasons[0])

    def test_unhashable_args_through_run_static_leave_stderr_clean(self) -> None:
        # whole-run check: one malformed file is a rejection line, never a traceback
        bad = assertion("g_skill_read", {"skill": []})
        self.write_steering([steering_case(assertions=[bad])])
        lines, code = run_static(self.kiro_home, self.root)
        self.assertEqual(code, 1)
        self.assertTrue(any(line.startswith("rejected ") for line in lines))
        self.assertIn("rejections=1", lines[-1])

    def test_skill_case_rules(self) -> None:
        bad = {
            "skill without with arm": (skill_case(arms=["without", "none"]), "skill arms"),
            "skill with full arm": (skill_case(arms=["with", "full"]), "skill arms"),
            "rule_id on skill": (skill_case(rule_id="r1"), "steering cases only"),
            "wrong suite": (skill_case(target="beta"), "skill case must sit"),
        }
        for label, (case, expected) in bad.items():
            with self.subTest(label):
                cases, reasons = self.load_skill([case])
                self.assertEqual(cases, ())
                self.assertIn(expected, reasons[0])

    def test_one_bad_case_rejects_the_whole_file(self) -> None:
        cases, reasons = self.load_steering([steering_case("good"), steering_case("bad", role="x")])
        self.assertEqual(cases, ())
        self.assertEqual(len(reasons), 1)

    def test_file_level_failures(self) -> None:
        path = self.write_steering([])
        for label, text, expected in (
            ("empty list", "[]", "non-empty list"),
            ("not json", "{oops", "invalid json"),
            ("object", "{}", "non-empty list"),
        ):
            with self.subTest(label):
                path.write_text(text, encoding="utf-8")
                cases, rejections = load_cases(
                    path, "steering", frozenset({"r1"}), self.world, self.limits
                )
                self.assertEqual(cases, ())
                self.assertIn(expected, rejections[0].reason)

    def test_duplicate_id_in_file_and_case_count_limit(self) -> None:
        _, reasons = self.load_steering([steering_case("dup"), steering_case("dup")])
        self.assertIn("duplicate case id", reasons[0])
        many = [steering_case(f"c{i}") for i in range(61)]
        _, reasons = self.load_steering(many)
        self.assertIn("limit is 60", reasons[0])

    def test_fixture_size_boundary(self) -> None:
        big = self.root / "skills" / "alpha" / "fixtures" / "big.bin"
        big.write_bytes(b"x" * (self.limits.fixture_bytes_max + 1))
        _, reasons = self.load_skill([skill_case(fixture="skills/alpha/fixtures")])
        self.assertIn("exceeds", reasons[0])


class RulesAndDatasetTest(TreeTest):
    def test_rules_validation(self) -> None:
        path = self.root / "steering" / "rule-a" / "rules.json"
        bad: dict[str, list[object]] = {
            "dup id": [rule(), rule()],
            "wrong source": [{**rule(), "source_file": "rule-b"}],
            "unknown grader": [rule(grader="g_magic")],
            "extra key": [{**rule(), "x": "y"}],
            "empty field": [{**rule(), "observable": " "}],
            "bad id": [rule("Bad Id")],
        }
        for label, rules in bad.items():
            with self.subTest(label):
                write_json(path, rules)
                loaded, rejections = load_rules(path, self.world, self.limits)
                self.assertEqual((loaded, len(rejections)), ((), 1))
        write_json(path, [rule(), rule("r2", "human")])
        loaded, rejections = load_rules(path, self.world, self.limits)
        self.assertEqual((len(loaded), rejections), (2, []))

    def test_duplicate_case_id_across_suites_rejects_the_later_file(self) -> None:
        self.write_steering([steering_case("same")])
        write_json(self.root / "skills" / "alpha" / "cases.json", [skill_case("same")])
        data = self.dataset()
        self.assertEqual([c.id for c in data.cases], ["same"])
        self.assertIn("already used", data.rejections[0].reason)
        self.assertEqual(data.rejected_suites, frozenset({"steering/rule-a"}))

    def test_suite_dir_naming_no_target_is_rejected(self) -> None:
        (self.root / "skills" / "ghost").mkdir()
        data = self.dataset()
        self.assertIn("names no existing skill", data.rejections[0].reason)


class CoverageTest(TreeTest):
    def rows(self) -> dict[tuple[str, str], str]:
        rows = classify_coverage(collect_targets(self.world), self.dataset())
        return {(r.target.kind, r.target.name): r.status for r in rows}

    def test_no_suites_means_everything_uncovered(self) -> None:
        statuses = set(self.rows().values())
        self.assertEqual(statuses, {"uncovered"})

    def test_fire_alone_is_not_covered_fire_and_no_fire_is(self) -> None:
        self.write_steering([steering_case("c1", "fire")])
        self.assertEqual(self.rows()[("steering", "rule-a")], "uncovered")
        self.write_steering([steering_case("c1", "fire"), steering_case("c2", "no_fire")])
        self.assertEqual(self.rows()[("steering", "rule-a")], "covered")

    def test_pressure_does_not_substitute_for_no_fire(self) -> None:
        self.write_steering([steering_case("c1", "fire"), steering_case("c2", "pressure")])
        self.assertEqual(self.rows()[("steering", "rule-a")], "uncovered")

    def test_human_only_rules_report_human_only(self) -> None:
        write_json(self.root / "steering" / "rule-a" / "rules.json", [rule(grader="human")])
        self.assertEqual(self.rows()[("steering", "rule-a")], "uncovered (human only)")
        write_json(
            self.root / "steering" / "rule-a" / "rules.json",
            [rule(grader="human"), rule("r2", "g_tool_used")],
        )
        self.assertEqual(self.rows()[("steering", "rule-a")], "uncovered")

    def test_rejected_suite_is_uncovered_even_with_good_cases(self) -> None:
        self.write_steering(
            [
                steering_case("c1", "fire"),
                steering_case("c2", "no_fire"),
                steering_case("c3", role="x"),
            ]
        )
        self.assertEqual(self.rows()[("steering", "rule-a")], "uncovered")

    def test_skill_coverage(self) -> None:
        write_json(
            self.root / "skills" / "alpha" / "cases.json",
            [skill_case("s1", "fire"), skill_case("s2", "no_fire")],
        )
        self.assertEqual(self.rows()[("skill", "alpha")], "covered")

    def test_tool_group_covered_only_by_named_fire_and_no_fire_cases(self) -> None:
        group = assertion(args={"tool": "execute_bash", "tool_group": "shell"})
        self.write_steering([steering_case("c1", "fire", assertions=[group])])
        self.assertEqual(self.rows()[("tool_group", "shell")], "uncovered")
        self.write_steering(
            [
                steering_case("c1", "fire", assertions=[group]),
                steering_case("c2", "no_fire", assertions=[group]),
            ]
        )
        self.assertEqual(self.rows()[("tool_group", "shell")], "covered")
        self.assertEqual(self.rows()[("tool_group", "network")], "uncovered")


class RuleAsSkillTest(TreeTest):
    """Goal: a skill-instead arm loads only with a well-formed as_skill.json beside its cases, and
    the description reaches only the cases that use the arm. Method: one suite, a sidecar per
    subtest; read the dataset's rejections."""

    def load_with(self, sidecar: object | None) -> Dataset:
        cases = [steering_case(arms=["full", "skill-instead"]), steering_case("c2")]
        suite = self.write_steering(cases).parent
        (suite / AS_SKILL_FILE).unlink(missing_ok=True)
        if sidecar is not None:
            write_json(suite / AS_SKILL_FILE, sidecar)
        return self.dataset()

    def test_a_good_sidecar_reaches_only_the_skill_instead_cases(self) -> None:
        data = self.load_with({"description": "Use when writing a status line."})
        self.assertEqual(data.rejections, ())
        by_id = {case.id: case for case in data.cases}
        self.assertEqual(by_id["c1"].rule_skill_description, "Use when writing a status line.")
        self.assertEqual(by_id["c1"].arms[1], Arm("skill-instead", None))
        self.assertIsNone(by_id["c2"].rule_skill_description)

    def test_the_description_bound_is_inclusive(self) -> None:
        at_max = "x" * self.limits.description_chars_max
        self.assertEqual(self.load_with({"description": at_max}).rejections, ())

    def test_a_missing_or_bad_sidecar_rejects_the_suite(self) -> None:
        too_long = "x" * (self.limits.description_chars_max + 1)
        bad: dict[str, tuple[object | None, str]] = {
            "missing": (None, f"needs {AS_SKILL_FILE}"),
            "not an object": (["Use when"], "must be"),
            "extra key": ({"description": "Use when", "name": "x"}, "must be"),
            "empty": ({"description": ""}, "must be"),
            "not text": ({"description": 7}, "must be"),
            "too long": ({"description": too_long}, "chars >"),
            "two lines": ({"description": "one\ntwo"}, "one line"),
            "a quote": ({"description": 'say "hi"'}, "one line"),
            "a backslash": ({"description": "a\\b"}, "one line"),
        }
        for label, (sidecar, expected) in bad.items():
            with self.subTest(label):
                data = self.load_with(sidecar)
                self.assertEqual(data.cases, ())
                self.assertIn(expected, " ".join(r.reason for r in data.rejections))

    def test_a_sidecar_that_is_not_json_rejects_the_suite(self) -> None:
        suite = self.write_steering([steering_case(arms=["full", "skill-instead"])]).parent
        (suite / AS_SKILL_FILE).write_text("{", encoding="utf-8")
        data = self.dataset()
        self.assertEqual(data.cases, ())
        self.assertIn(f"needs {AS_SKILL_FILE}", data.rejections[0].reason)

    def test_a_suite_without_the_arm_needs_no_sidecar(self) -> None:
        self.write_steering([steering_case()])
        data = self.dataset()
        self.assertEqual(data.rejections, ())
        self.assertIsNone(data.cases[0].rule_skill_description)

    def test_skill_instead_fits_only_an_always_on_rule(self) -> None:
        path = self.root / "steering" / "rule-b" / "cases.json"
        write_json(path, [steering_case(target="rule-b", arms=["full", "skill-instead"])])
        _, rejections = load_cases(path, "steering", frozenset({"r1"}), self.world, self.limits)
        self.assertIn("fits only an always-on", " ".join(r.reason for r in rejections))


if __name__ == "__main__":
    unittest.main()
