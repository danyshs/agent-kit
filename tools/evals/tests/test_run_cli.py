"""Goal: `eval run` plans, refuses over budget, runs each spec, grades, appends exactly one ledger
line per run, enforces the probe's TOTAL credit cap, and cleans its /tmp scratch.
Method: a throwaway dataset root and a fake kiro-cli replaying a recorded stream; assert on the
ledger file's lines and the printed status line, not on exit codes alone."""

from __future__ import annotations

import contextlib
import io
import json
import re
import tempfile
import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from unittest import mock

from lib import isolation_probe
from lib.cli import EVALS_ROOT, main
from lib.run import CLAUDE_ENGINE, Invoker, InvokeResult, load_run_limits
from lib.run_cli import RUN_USAGE, RunArgs, parse_run_args, run_suite
from tests.helpers import assertion, rule, steering_case, write_json
from tests.helpers_run import (
    SHELL_OK,
    SHELL_OK_CALL,
    SHELL_OK_SESSION,
    fake_kiro,
    fixture_text,
)
from tests.test_dataset import TreeTest

CALLS = [(SHELL_OK_CALL, "execute_bash", "approved")]
HOME = Path("/home/x/.kiro")


def scratch_dirs() -> set[str]:
    return {p.name for p in Path("/tmp").glob("ev*-*") if p.name.startswith(("evhome-", "evcwd-"))}


class ParseRunArgsTest(unittest.TestCase):
    def test_defaults_and_flags(self) -> None:
        parsed = parse_run_args(["steering/shell"], HOME)
        assert isinstance(parsed, RunArgs)
        self.assertEqual(
            (parsed.suite_kind, parsed.suite_name, parsed.tier), ("steering", "shell", "smoke")
        )
        self.assertEqual((parsed.probe, parsed.arm, parsed.kiro_home), (False, None, HOME))
        full = parse_run_args(
            ["skill/tiger-style", "--probe-run", "--arm", "with", "--model", "m"], HOME
        )
        assert isinstance(full, RunArgs)
        self.assertEqual((full.probe, full.arm, full.model_id), (True, "with", "m"))

    def test_bad_args_are_usage(self) -> None:
        for argv in (
            [],
            ["shell"],
            ["bogus/shell"],
            ["steering/shell", "--tier", "deep"],
            ["steering/shell", "--arm"],
            ["steering/shell", "--nope"],
        ):
            self.assertIsInstance(parse_run_args(argv, HOME), str, argv)


class RunSuiteTest(TreeTest):
    def setUp(self) -> None:
        super().setUp()
        suite = self.root / "steering" / "rule-a"
        write_json(suite / "rules.json", [rule("r1", "g_tool_used")])
        write_json(
            suite / "cases.json",
            [steering_case("c1", assertions=[assertion(args={"tool": "execute_bash"})])],
        )
        self.ledger = self.root / "ledger" / "steering_rule-a.jsonl"
        self.before = scratch_dirs()

    def set_limits(self, **over: object) -> None:
        path = self.root / "limits.json"
        data = json.loads(path.read_text())
        data.update(over)
        path.write_text(json.dumps(data))

    def args(self, **over: object) -> RunArgs:
        base = RunArgs(
            "steering", "rule-a", "smoke", "claude-haiku-5.5", None, True, self.kiro_home
        )
        return replace(base, **over)

    def go(self, args: RunArgs, invoke: Invoker | None = None) -> tuple[list[str], int]:
        invoke = invoke or fake_kiro(fixture_text(SHELL_OK), SHELL_OK_SESSION, CALLS)
        return run_suite(args, self.root, invoke, stamp_s=42)

    def ledger_rows(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.ledger.read_text().splitlines()]

    def test_a_null_tier_cap_fails_to_load(self) -> None:
        # every tier ships with numbers: a null cap is a broken limits file, not "unset"
        full = {
            "repeats": 1,
            "RUN_CREDITS_MAX": 0.1,
            "RUN_WALL_MS_MAX": 1000,
            "SUITE_CREDITS_MAX": 1.0,
        }
        for key in ("RUN_CREDITS_MAX", "RUN_WALL_MS_MAX", "SUITE_CREDITS_MAX"):
            self.set_limits(tiers={"smoke": full, "standard": {**full, key: None}})
            with self.assertRaisesRegex(ValueError, f"tiers.standard.{key} must be set"):
                load_run_limits(self.root / "limits.json")
            self.assertFalse(self.ledger.exists(), "nothing ran")

    def test_claude_suite_refused_when_the_isolation_probe_leaks(self) -> None:
        # goal: a Claude suite whose probe reads the home canary starts no case; the plan line
        # already counts the probe's attempts
        real = json.loads((EVALS_ROOT / "limits.json").read_text(encoding="utf-8"))
        self.set_limits(claude=real["claude"])
        calls: list[list[str]] = []

        def invoke(argv: list[str], env: Mapping[str, str], cwd: Path, t: float) -> InvokeResult:
            if argv[1:] == ["--version"]:
                return InvokeResult("2.1.326 (Claude Code)\n", "", 0, False, 1)
            calls.append(argv)
            path = re.search(r"cat (\S+)", argv[-1])
            assert path, "the probe prompt names its canary"
            return InvokeResult(Path(path.group(1)).read_text(encoding="utf-8"), "", 0, False, 1)

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(isolation_probe, "CANARY_PARENT", Path(tmp)):
                args = self.args(engine=CLAUDE_ENGINE, model_id="claude-haiku-5-5", probe=False)
                lines, code = self.go(args, invoke)
        self.assertTrue(lines[0].startswith("plan: engine=claude runs=4 "), lines[0])
        self.assertTrue(lines[-1].startswith("run: refused: isolation probe: LEAK"), lines[-1])
        self.assertEqual((code, len(calls)), (1, 1))
        self.assertFalse(self.ledger.exists(), "no case ran")

    def test_probe_run_appends_one_line_per_run_and_cleans_scratch(self) -> None:
        lines, code = self.go(self.args())
        rows = self.ledger_rows()
        self.assertEqual([r["arm"] for r in rows], ["full", "none"])
        self.assertTrue(all(r["credits"] is not None and r["status"] == "ok" for r in rows))
        self.assertTrue(all(r["case_passed"] is True for r in rows))
        self.assertTrue(lines[0].startswith("plan: engine=v3 runs=2 credits_max_total=0.4"))
        self.assertIn("runs=2 passed=2 failed=0 aborted=0", lines[-1])
        self.assertEqual(code, 0)
        self.assertEqual(scratch_dirs(), self.before, "throwaway KIRO_HOME and cwd are deleted")

    def test_skill_instead_run_finds_the_rule_as_a_skill_in_its_home(self) -> None:
        # goal: the description in as_skill.json reaches the built home of a real run
        suite = self.root / "steering" / "rule-a"
        case = steering_case("c1", arms=["full", "skill-instead"])
        case["assertions"] = [assertion(args={"tool": "execute_bash"})]
        write_json(suite / "cases.json", [case])
        write_json(suite / "as_skill.json", {"description": "Use when the rule applies."})
        replay = fake_kiro(fixture_text(SHELL_OK), SHELL_OK_SESSION, CALLS)
        seen: list[str] = []

        def invoke(argv: list[str], env: Mapping[str, str], cwd: Path, t: float) -> InvokeResult:
            skill = Path(env.get("KIRO_HOME", "/nonexistent")) / "skills" / "rule-a" / "SKILL.md"
            if argv[1] != "--version":
                seen.append(skill.read_text(encoding="utf-8") if skill.is_file() else "")
            return replay(argv, env, cwd, t)

        lines, code = self.go(self.args(), invoke)
        self.assertEqual(code, 0, lines)
        self.assertEqual([r["arm"] for r in self.ledger_rows()], ["full", "skill-instead"])
        self.assertEqual(seen[0], "", "the full arm loads the rule as a rule")
        self.assertIn('description: "Use when the rule applies."', seen[1])

    def test_arm_filter_runs_one_arm(self) -> None:
        self.go(self.args(arm="full"))
        self.assertEqual([r["arm"] for r in self.ledger_rows()], ["full"])

    def test_case_and_repeats_flags_narrow_and_repeat(self) -> None:
        self.set_limits(PROBE_RUN_CREDITS_MAX=5)
        self.go(self.args(arm="full", repeats=3))
        rows = self.ledger_rows()
        self.assertEqual([r["repeat_index"] for r in rows], [0, 1, 2])
        lines, _ = self.go(self.args(case_id="no-such-case"))
        self.assertTrue(lines[0].startswith("plan: engine=v3 runs=0"), "unknown case plans nothing")

    def test_parse_rejects_bad_repeats(self) -> None:
        home = Path("/tmp/x")
        self.assertIsInstance(parse_run_args(["steering/a", "--repeats", "9"], home), str)
        for digits in ("²", "٣"):  # str.isdigit() accepts these, int() rejects or misreads them
            self.assertEqual(parse_run_args(["steering/a", "--repeats", digits], home), RUN_USAGE)
        parsed = parse_run_args(["steering/a", "--case", "c", "--repeats", "3"], home)
        self.assertIsInstance(parsed, RunArgs)
        assert isinstance(parsed, RunArgs)
        self.assertEqual((parsed.case_id, parsed.repeats), ("c", 3))

    def test_model_cap_overrides_tier_cap_and_suite_guard(self) -> None:
        tier = {
            "repeats": 1,
            "RUN_CREDITS_MAX": 0.13,
            "RUN_WALL_MS_MAX": 120000,
            "SUITE_CREDITS_MAX": 0.2,
        }
        self.set_limits(
            tiers={"smoke": tier, "standard": tier}, MODEL_RUN_CREDITS_MAX={"big-model": 3.0}
        )
        lines, _ = self.go(self.args(probe=False, model_id="claude-haiku-5.5"))
        self.assertIn("over SUITE_CREDITS_MAX", lines[-1], "flat cap: 2 x 0.13 > 0.2")
        lines, _ = self.go(self.args(probe=False, model_id="big-model"))
        self.assertTrue(
            lines[0].startswith("plan: engine=v3 runs=2 credits_max_total=6.0"), lines[0]
        )
        self.set_limits(MODEL_RUN_CREDITS_MAX={"big-model": -1})
        with self.assertRaises(ValueError):
            self.go(self.args(probe=False, model_id="big-model"))

    def test_probe_cap_is_a_total_across_arms(self) -> None:
        # each run alone (0.0031 credits) is under the cap; the two together are not
        self.set_limits(PROBE_RUN_CREDITS_MAX=0.005)
        lines, code = self.go(self.args())
        rows = self.ledger_rows()
        self.assertEqual([r["status"] for r in rows], ["ok", "budget_abort"])
        self.assertIn("runs=2 passed=1 failed=0 aborted=1", lines[-1])
        self.assertEqual(code, 1)

    def test_probe_cap_spent_starts_no_further_run(self) -> None:
        self.set_limits(PROBE_RUN_CREDITS_MAX=0.001)
        lines, code = self.go(self.args())
        rows = self.ledger_rows()
        self.assertEqual(len(rows), 1, "the second arm never starts once the total is spent")
        self.assertEqual(rows[0]["status"], "budget_abort")
        self.assertIn("runs=1 passed=0 failed=0 aborted=1", lines[-1])
        self.assertIn("probe total spent", "\n".join(lines))
        self.assertEqual(code, 1)

    def test_harness_errors_abort_the_suite(self) -> None:
        self.set_limits(CONSECUTIVE_HARNESS_ERRORS_MAX=1)

        def broken(argv: list[str], env: object, cwd: Path, timeout_s: float) -> InvokeResult:
            if argv[1] == "--version":
                return InvokeResult("kiro-cli 3.2.1", "", 0, False, 1)
            return InvokeResult("garbage", "", 0, False, 5)

        lines, code = self.go(self.args(), broken)
        self.assertEqual(len(self.ledger_rows()), 1)
        self.assertIn("suite aborted", "\n".join(lines))
        self.assertEqual(code, 1)

    def test_contaminated_run_is_recorded_and_its_stream_deleted(self) -> None:
        real = str(Path("~/.kiro/skills/tiger-style/SKILL.md").expanduser())
        peek = {
            "type": "sessionUpdate",
            "data": {
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "toolu_peek",
                    "kind": "read",
                    "locations": [{"path": real}],
                    "rawInput": {"path": real},
                }
            },
        }
        stream_lines = fixture_text(SHELL_OK).splitlines()
        stream = "\n".join([stream_lines[0], json.dumps(peek), *stream_lines[1:]])
        calls = [("toolu_peek", "read_file", "approved"), *CALLS]
        lines, _ = self.go(self.args(arm="none"), fake_kiro(stream, SHELL_OK_SESSION, calls))
        self.assertEqual(self.ledger_rows()[0]["status"], "contaminated")
        self.assertEqual(self.ledger_rows()[0]["case_passed"], False)
        self.assertEqual(list((self.root / "runs").glob("*.jsonl")), [], "stream deleted (3.6)")
        self.assertIn("runs=1 passed=0 failed=1 aborted=0", lines[-1])

    def test_rejected_suite_runs_nothing(self) -> None:
        write_json(self.root / "steering" / "rule-a" / "cases.json", [{"id": "bad"}])
        lines, code = self.go(self.args())
        self.assertIn("run: suite has no valid cases", lines[-1])
        self.assertEqual(code, 1)
        self.assertFalse(self.ledger.exists())


class SkillSuiteDryRunTest(unittest.TestCase):
    """The real tiger-style suite through run_suite with a fake kiro-cli: no model, no credits."""

    def test_glob_plans_review_cases_and_grades_into_a_skill_ledger(self) -> None:
        import shutil
        import tempfile

        from lib.cli import DEFAULT_KIRO_HOME, EVALS_ROOT

        if not (DEFAULT_KIRO_HOME / "skills" / "tiger-style" / "SKILL.md").is_file():
            self.skipTest("tiger-style skill is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("limits.json", "trust_names.json", "tool_groups.json", "known_tools.json"):
                shutil.copy(EVALS_ROOT / name, root / name)
            shutil.copy(EVALS_ROOT / "lint_baseline.json", root / "lint_baseline.json")
            shutil.copytree(EVALS_ROOT / "skills", root / "skills")
            shutil.copytree(EVALS_ROOT / "planted", root / "planted")
            seen_diff: list[bool] = []
            inner = fake_kiro(fixture_text(SHELL_OK), SHELL_OK_SESSION, CALLS)

            def invoke(
                argv: list[str], env: Mapping[str, str], cwd: Path, t: float
            ) -> InvokeResult:
                if argv[1] != "--version":
                    seen_diff.append((cwd / "change.diff").is_file())
                return inner(argv, env, cwd, t)

            args = RunArgs(
                "skill",
                "tiger-style",
                "smoke",
                "claude-haiku-5.5",
                None,
                False,
                DEFAULT_KIRO_HOME,
                case_id="rev-*",
            )
            before = scratch_dirs()
            lines, _ = run_suite(args, root, invoke, stamp_s=42)
            ledger = root / "ledger" / "skill_tiger-style.jsonl"
            rows = [json.loads(line) for line in ledger.read_text().splitlines()]
            self.assertTrue(
                lines[0].startswith("plan: engine=v3 runs=8 credits_max_total=1.04"), lines[0]
            )
            self.assertEqual(len(rows), 8)
            self.assertEqual(sorted({r["arm"] for r in rows}), ["with", "without"])
            self.assertEqual(seen_diff, [True] * 8, "the fixture is copied into every cwd")
            recall = [g for g in rows[0]["grades"] if g["assertion_id"] == "recall"]
            self.assertTrue(recall and recall[0]["evidence"].startswith("no parse"))
            self.assertEqual(scratch_dirs(), before, "scratch homes and cwds are deleted")
            wrong = replace(args, case_id="nothing-*")
            self.assertEqual(len(run_suite(wrong, root, invoke, stamp_s=43)[0]), 2 + 0, "no runs")


class MainRunTest(unittest.TestCase):
    def test_usage_error_goes_to_stderr(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main(["run"])
        self.assertEqual(code, 2)
        self.assertIn("usage: eval run", err.getvalue())


if __name__ == "__main__":
    unittest.main()
