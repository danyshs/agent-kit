"""Goal: the stream parser, session-log join, contamination check, multi-turn aggregation and
budget rules do what the design says, against the committed v3 contract fixtures.
Method: replay recorded streams through a fake kiro-cli; assert on parsed values and on the
RunRecord, never on exit codes. A crafted stream proves the contamination check fires."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from lib.dataset import Arm, Assertion, Case
from lib.home import HomeDir
from lib.run import (
    Invoker,
    InvokeResult,
    LogCall,
    RunLimits,
    RunRecord,
    RunSpec,
    RunView,
    Tier,
    TierLimits,
    Turn,
    apply_log,
    build_argv,
    check_budget,
    derive_skill_events,
    load_run_limits,
    mark_contaminated,
    outside_paths,
    parse_stream,
    pick_status,
    plan_runs,
    read_session_log,
    reindex,
    run_case,
    worst_case,
)
from tests.helpers_run import (
    DENIED,
    DENIED_LOG,
    DENIED_SESSION,
    SHELL_OK,
    SHELL_OK_CALL,
    SHELL_OK_CREDITS,
    SHELL_OK_SESSION,
    SKILL_LOAD,
    call,
    fake_kiro,
    fixture_text,
    make_event,
    make_spec,
)

LIMITS = RunLimits(
    stream_bytes_max=4_194_304,
    harness_errors_max=3,
    home_files_max=64,
    user_turns_max=20,
    probe_credits_max=0.2,
    probe_wall_ms_max=120000,
    tiers={"smoke": TierLimits(1, 0.1, 60000, 5.0), "standard": TierLimits(3, 1.0, 60000, 10.0)},
)
SHELL_CALLS = [(SHELL_OK_CALL, "execute_bash", "approved")]


def parsed(name: str) -> Turn:
    result = parse_stream(fixture_text(name))
    assert isinstance(result, Turn), result
    return result


class ParseStreamTest(unittest.TestCase):
    def test_shell_ok_fixture(self) -> None:
        turn = parsed(SHELL_OK)
        self.assertEqual(turn.session_id, SHELL_OK_SESSION)
        self.assertEqual(turn.final_text, "hi")
        assert turn.credits is not None
        self.assertAlmostEqual(turn.credits, SHELL_OK_CREDITS)
        self.assertIsNotNone(turn.context_pct)
        kinds = [e.kind for e in turn.events]
        self.assertEqual(kinds.count("tool_call"), 1)
        self.assertEqual(kinds.count("tool_result"), 1, "only the terminal update is a result")
        self.assertEqual(kinds[-1], "run_end")
        first = next(e for e in turn.events if e.kind == "tool_call")
        self.assertEqual(first.call_id, SHELL_OK_CALL)
        self.assertIn('"command": "echo hi"', first.args_json or "")

    def test_toolcallid_joins_call_and_result(self) -> None:
        turn = parsed(DENIED)
        calls = {e.call_id for e in turn.events if e.kind == "tool_call"}
        results = {e.call_id for e in turn.events if e.kind == "tool_result"}
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls, results)

    def test_bad_shapes_are_reasons_not_skips(self) -> None:
        good = fixture_text(SHELL_OK).splitlines()
        self.assertIsInstance(parse_stream("not json\n"), str)
        self.assertIsInstance(parse_stream("\n".join(good[1:])), str, "no runStarted")
        self.assertIsInstance(parse_stream("\n".join(good[:-1])), str, "no runFinished")
        v2 = good[0].replace('"v3"', '"v2"')
        self.assertIn("engine", str(parse_stream("\n".join([v2, *good[1:]]))))
        unknown = json.dumps(
            {"type": "sessionUpdate", "data": {"update": {"sessionUpdate": "new_thing"}}}
        )
        self.assertIn("new_thing", str(parse_stream("\n".join([good[0], unknown, *good[1:]]))))
        error = json.dumps(
            {"type": "runError", "data": {"stage": "auth", "message": "Not logged in"}}
        )
        self.assertIn("Not logged in", str(parse_stream("\n".join([good[0], error]))))

    def test_non_success_finish_is_a_reason(self) -> None:
        lines = fixture_text(SHELL_OK).splitlines()
        lines[-1] = lines[-1].replace('"status":"success"', '"status":"error"')
        self.assertIn("status", str(parse_stream("\n".join(lines))))


class SessionLogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        log_dir = self.home / "sessions" / "0123456789abcdef" / DENIED_SESSION
        log_dir.mkdir(parents=True)
        (log_dir / "messages.jsonl").write_text(fixture_text(DENIED_LOG), encoding="utf-8")

    def test_names_and_denials_come_from_the_log(self) -> None:
        calls = read_session_log(self.home, DENIED_SESSION, 4_194_304)
        assert isinstance(calls, dict)
        joined = apply_log(parsed(DENIED).events, calls)
        assert not isinstance(joined, str)
        names = [e.name for e in joined if e.kind == "tool_call"]
        self.assertEqual(names, ["read_file", "read_file", "execute_bash", "fs_write"])
        denied = [e.name for e in joined if e.kind == "tool_result" and e.status == "denied"]
        self.assertEqual(denied, ["read_file", "execute_bash", "fs_write"])
        done = [e.status for e in joined if e.kind == "tool_result" and e.name == "read_file"]
        self.assertEqual(done, ["denied", "completed"])

    def test_missing_or_ambiguous_log_is_a_reason(self) -> None:
        self.assertIsInstance(read_session_log(self.home, "sess_00000000-0000", 4_194_304), str)
        self.assertIsInstance(read_session_log(self.home, DENIED_SESSION, 10), str)

    def test_call_missing_from_log_is_a_reason(self) -> None:
        self.assertIsInstance(apply_log(parsed(DENIED).events, {}), str)

    def test_skill_events_are_derived_after_the_join(self) -> None:
        events = parsed(SKILL_LOAD).events
        calls = {
            e.call_id: LogCall("read_file", "approved")
            for e in events
            if e.kind == "tool_call" and e.name is None and e.call_id
        }
        joined = apply_log(events, calls)
        assert not isinstance(joined, str)
        derived = derive_skill_events(joined)
        self.assertEqual([e.name for e in derived if e.kind == "skill_read"], ["tiger-style"])
        refs = [e.name for e in derived if e.kind == "skill_ref_read"]
        self.assertEqual(
            refs, ["tiger-style"] * 3, "three reference reads under skills/<name>/references"
        )

    def test_skill_load_by_id_shape_is_derived_and_junk_is_not(self) -> None:
        load = call("disclose_context", {"id": "skill::tiger-style"})
        aliased = apply_log((load,), {load.call_id or "": LogCall("load_resource", "approved")})
        assert not isinstance(aliased, str)
        self.assertEqual(aliased[0].name, "disclose_context", "the stream's ACP name wins")
        self.assertEqual(aliased[0].status, "approved", "the log still supplies the status")

        def names(args: dict[str, object]) -> list[str | None]:
            derived = derive_skill_events((call("disclose_context", args),))
            return [e.name for e in derived if e.kind == "skill_read"]

        self.assertEqual(names({"id": "skill::tiger-style"}), ["tiger-style"])
        self.assertEqual(names({"name": "tiger-style"}), ["tiger-style"])
        self.assertEqual(names({"id": "steering::x"}), [], "only skill:: ids are skills")
        self.assertEqual(names({"id": "skill::"}), [], "planted: an empty skill name")
        self.assertEqual(names({}), [])
        self.assertEqual(names({"name": 5}), [], "a mistyped name is ignored, not a crash")


class ContaminationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.cwd, self.home = base / "cwd", base / "home"
        (self.cwd / "src").mkdir(parents=True)
        (self.home / "skills" / "x").mkdir(parents=True)
        self.alias = base / "alias"
        self.alias.symlink_to(self.cwd)

    def flagged(self, *paths: str) -> list[str]:
        events = tuple(call("read_file", paths=(p,)) for p in paths)
        return outside_paths(events, str(self.cwd), str(self.home))

    def test_inside_cwd_and_home_is_clean(self) -> None:
        inside = [str(self.cwd / "a.py"), "src/b.py", str(self.home / "skills" / "x" / "SKILL.md")]
        self.assertEqual(self.flagged(*inside), [])

    def test_real_home_read_is_flagged(self) -> None:
        real = os.path.expanduser("~/.kiro/skills/tiger-style/SKILL.md")
        self.assertEqual(self.flagged(real), [real])

    def test_prefix_sibling_is_outside(self) -> None:
        sibling = str(self.cwd) + "-evil/a.py"
        self.assertEqual(self.flagged(sibling), [sibling])
        self.assertEqual(self.flagged("../escape.py"), ["../escape.py"])

    def test_realpath_applies_to_both_sides(self) -> None:
        # event path through a symlink alias of the cwd: inside only if the event side is resolved
        via_alias = str(self.alias / "a.py")
        self.assertEqual(self.flagged(via_alias), [])
        # cwd given as the alias, event path real: inside only if the cwd side is resolved
        events = (call("read_file", paths=(str(self.cwd / "a.py"),)),)
        self.assertEqual(outside_paths(events, str(self.alias), str(self.home)), [])

    def test_shell_text_and_pathless_calls_are_never_contamination(self) -> None:
        events = (call("execute_bash", {"command": "cat /etc/passwd"}),)
        self.assertFalse(mark_contaminated(events, str(self.cwd), str(self.home)))

    def test_recorded_outside_read_fixture_fires(self) -> None:
        events = parsed(DENIED).events
        self.assertTrue(mark_contaminated(events, "/tmp/evp3/cwd", "/tmp/evp3/kh"))
        self.assertEqual(
            outside_paths(events, "/tmp/evp3/cwd", "/tmp/evp3/kh"), ["/tmp/evp3/outside.txt"]
        )

    def test_crafted_stream_marks_the_run_contaminated(self) -> None:
        # a without-arm model peeks at the real skill: stream says read_file outside cwd and home
        real = os.path.expanduser("~/.kiro/skills/tiger-style/SKILL.md")
        lines = fixture_text(SHELL_OK).splitlines()
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
        stream = "\n".join([lines[0], json.dumps(peek), *lines[1:]])
        calls = [("toolu_peek", "read_file", "approved"), *SHELL_CALLS]
        record, view = self.run_stream(stream, calls)
        self.assertEqual(record.status, "contaminated")
        self.assertEqual(view.status, "contaminated")

    def run_stream(
        self, stream: str, calls: list[tuple[str, str, str]]
    ) -> tuple[RunRecord, RunView]:
        invoke = fake_kiro(stream, SHELL_OK_SESSION, calls)
        home = Path(self.tmp.name) / "run-home"
        home.mkdir()
        return run_case(
            make_spec(),
            make_case(),
            HomeDir(str(home), "abc"),
            self.cwd,
            LIMITS,
            Path(self.tmp.name) / "runs",
            invoke,
        )


def make_case(**over: object) -> Case:
    base = Case(
        id="c1",
        target="rule-a",
        target_kind="steering",
        rule_id="r1",
        role="fire",
        prompt="do the thing",
        user_turns=(),
        arm_prompts=(),
        fixture=None,
        trust_tools=(),
        arms=(Arm("full", None),),
        extra_skills=(),
        extra_steering=(),
        repeats=None,
        tags=(),
        tier=None,
        assertions=(Assertion("a1", "g_tool_used", {"tool": "execute_bash"}, True),),
        sha="c1sha",
    )
    return replace(base, **over)


class RunCaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        (self.base / "cwd").mkdir()
        (self.base / "home").mkdir()

    def run_it(
        self, case: Case, invoke: Invoker, spec: RunSpec | None = None
    ) -> tuple[RunRecord, RunView]:
        return run_case(
            spec or make_spec(),
            case,
            HomeDir(str(self.base / "home"), "abc"),
            self.base / "cwd",
            LIMITS,
            self.base / "runs",
            invoke,
        )

    def test_single_turn_record(self) -> None:
        seen: list[list[str]] = []
        record, view = self.run_it(
            make_case(), fake_kiro(fixture_text(SHELL_OK), SHELL_OK_SESSION, SHELL_CALLS, seen)
        )
        self.assertEqual(record.status, "ok")
        self.assertEqual((record.tool_call_count, record.denied_count), (1, 0))
        assert record.credits is not None
        self.assertAlmostEqual(record.credits, SHELL_OK_CREDITS)
        self.assertEqual(
            (record.session_id, record.cli_version, record.model_id),
            (SHELL_OK_SESSION, "3.2.1", "claude-haiku-5.5"),
        )
        self.assertEqual(view.final_text, "hi")
        self.assertIn("--v3", seen[0])
        self.assertNotIn("--resume-id", seen[0])
        self.assertTrue((self.base / "runs" / "c1--full--r0--1.t0.jsonl").is_file())

    def test_two_turns_sum_credits_and_wall_and_resume(self) -> None:
        seen: list[list[str]] = []
        invoke = fake_kiro(fixture_text(SHELL_OK), SHELL_OK_SESSION, SHELL_CALLS, seen, wall_ms=250)
        record, view = self.run_it(make_case(prompt=None, user_turns=("one", "two")), invoke)
        assert record.credits is not None
        self.assertAlmostEqual(record.credits, 2 * SHELL_OK_CREDITS)
        self.assertEqual(record.wall_ms, 500)
        self.assertEqual(record.tool_call_count, 2)
        self.assertIn("--resume-id", seen[1])
        self.assertEqual(seen[1][seen[1].index("--resume-id") + 1], SHELL_OK_SESSION)
        self.assertEqual([e.index for e in view.events], list(range(len(view.events))))

    def test_over_credit_budget_aborts_after_the_turn(self) -> None:
        seen: list[list[str]] = []
        invoke = fake_kiro(fixture_text(SHELL_OK), SHELL_OK_SESSION, SHELL_CALLS, seen)
        record, _ = self.run_it(
            make_case(prompt=None, user_turns=("one", "two")), invoke, make_spec(credits_max=0.001)
        )
        self.assertEqual(record.status, "budget_abort")
        self.assertEqual(
            len([a for a in seen if a[1] == "chat"]), 1, "no second turn after the abort"
        )

    def test_over_wall_budget_aborts(self) -> None:
        invoke = fake_kiro(fixture_text(SHELL_OK), SHELL_OK_SESSION, SHELL_CALLS, wall_ms=5000)
        record, _ = self.run_it(make_case(), invoke, make_spec(wall_ms_max=1000))
        self.assertEqual(record.status, "budget_abort")

    def test_harness_errors_are_recorded_with_a_note(self) -> None:
        bad_stream = fake_kiro("garbage\n", SHELL_OK_SESSION, SHELL_CALLS)
        record, _ = self.run_it(make_case(), bad_stream)
        self.assertEqual(record.status, "harness_error")
        self.assertIn("not a json object", record.note)
        no_log = fake_kiro(fixture_text(SHELL_OK), SHELL_OK_SESSION, [])
        record, _ = self.run_it(make_case(), no_log)
        self.assertEqual(record.status, "harness_error", "a call missing from the session log")
        v2 = fixture_text(SHELL_OK).replace('"engine":"v3"', '"engine":"v2"')
        record, _ = self.run_it(make_case(), fake_kiro(v2, SHELL_OK_SESSION, SHELL_CALLS))
        self.assertEqual(record.status, "harness_error")

    def test_exit_code_alone_is_never_success(self) -> None:
        def nonzero(
            argv: list[str], env: Mapping[str, str], cwd: Path, timeout_s: float
        ) -> InvokeResult:
            return InvokeResult(fixture_text(SHELL_OK), "", 1, False, 10)

        record, _ = self.run_it(make_case(), nonzero)
        self.assertEqual(record.status, "harness_error")
        self.assertIn("exit code 1", record.note)

    def test_timeout_records_null_credits(self) -> None:
        def slow(
            argv: list[str], env: Mapping[str, str], cwd: Path, timeout_s: float
        ) -> InvokeResult:
            return InvokeResult("", "", None, True, 120000)

        record, _ = self.run_it(make_case(), slow)
        self.assertEqual((record.status, record.credits), ("timeout", None))


class StatusTest(unittest.TestCase):
    def test_precedence(self) -> None:
        denied_call = (
            call("execute_bash"),
            make_event("tool_result", "execute_bash", status="denied"),
        )
        self.assertEqual(pick_status("harness_error", denied_call, True), "harness_error")
        self.assertEqual(pick_status("timeout", denied_call, True), "timeout")
        self.assertEqual(pick_status("budget_abort", denied_call, True), "budget_abort")
        self.assertEqual(pick_status("ok", denied_call, True), "contaminated")
        self.assertEqual(pick_status("ok", denied_call, False), "denied_only")
        self.assertEqual(pick_status("ok", (), False), "ok", "no tool calls is ok, not denied_only")

    def test_one_approved_call_is_not_denied_only(self) -> None:
        mixed = (
            call("read_file"),
            make_event("tool_result", "read_file", status="completed"),
            call("execute_bash"),
            make_event("tool_result", "execute_bash", status="denied"),
        )
        self.assertEqual(pick_status("ok", mixed, False), "ok")

    def test_reindex_is_dense(self) -> None:
        self.assertEqual([e.index for e in reindex([call("a"), call("b"), call("c")])], [0, 1, 2])


class PlanTest(unittest.TestCase):
    def cases(self) -> list[Case]:
        return [make_case(arms=(Arm("full", None), Arm("none", None)))]

    def plan(
        self, tier: Tier = "smoke", *, probe: bool = False, arm: str | None = None
    ) -> list[RunSpec]:
        return plan_runs(
            self.cases(),
            tier,
            "claude-haiku-5.5",
            "3.2.1",
            LIMITS,
            trust_names={"read_file": "read_file"},
            stamp_s=7,
            probe=probe,
            arm_filter=arm,
        )

    def test_case_filter_is_a_glob_and_an_exact_id_still_works(self) -> None:
        cases = [make_case(id=name) for name in ("trig-a-dev-1", "trig-a-held-1", "rev-x")]

        def ids(pattern: str) -> list[str]:
            specs = plan_runs(
                cases,
                "smoke",
                "claude-haiku-5.5",
                "3.2.1",
                LIMITS,
                trust_names={"read_file": "read_file"},
                stamp_s=7,
                probe=False,
                arm_filter=None,
                case_filter=pattern,
            )
            return sorted({s.case_id for s in specs})

        self.assertEqual(ids("trig-*-dev-*"), ["trig-a-dev-1"])
        self.assertEqual(ids("rev-x"), ["rev-x"])
        self.assertEqual(ids("nothing-*"), [])

    def test_runs_are_arms_times_repeats(self) -> None:
        self.assertEqual(len(self.plan("smoke")), 2)
        self.assertEqual(len(self.plan("standard")), 6)
        self.assertEqual([s.arm.kind for s in self.plan(arm="none")], ["none"])

    def test_probe_runs_use_the_hard_floor(self) -> None:
        specs = self.plan(probe=True)
        self.assertEqual({(s.credits_max, s.wall_ms_max) for s in specs}, {(0.2, 120000)})

    def test_probe_is_smoke_only(self) -> None:
        ceiling = worst_case(self.plan())
        self.assertIsNone(check_budget(ceiling, "smoke", LIMITS, probe=True))
        self.assertIn(
            "smoke",
            str(check_budget(worst_case(self.plan("standard")), "standard", LIMITS, probe=True)),
        )

    def test_set_limits_pass_and_suite_cap_refuses(self) -> None:
        ceiling = worst_case(self.plan("standard"))
        self.assertIsNone(check_budget(ceiling, "standard", LIMITS, probe=False))
        tight = replace(LIMITS, tiers={**LIMITS.tiers, "standard": TierLimits(3, 1.0, 60000, 2.0)})
        self.assertIn(
            "SUITE_CREDITS_MAX", str(check_budget(ceiling, "standard", tight, probe=False))
        )

    def test_real_limits_json_loads_with_every_tier_capped(self) -> None:
        from lib.cli import EVALS_ROOT

        limits = load_run_limits(EVALS_ROOT / "limits.json")
        self.assertEqual(limits.probe_credits_max, 0.2)
        self.assertEqual(limits.probe_wall_ms_max, 120000)
        self.assertEqual(limits.tiers["standard"], TierLimits(3, 0.26, 300000, 30.0))
        claude = load_run_limits(EVALS_ROOT / "limits.json", "claude")
        self.assertEqual(claude.tiers["standard"], TierLimits(3, 0.30, 300000, 30.0))


class ArgvTest(unittest.TestCase):
    def test_command_shape(self) -> None:
        argv = build_argv("claude-haiku-5.5", ("read_file", "run_command"), "hello", None)
        self.assertEqual(argv[:3], ["kiro-cli", "chat", "--no-interactive"])
        self.assertIn("--v3", argv)
        self.assertIn("--trust-tools=read_file,run_command", argv)
        self.assertEqual(argv[-1], "hello")
        self.assertIn("--trust-tools=", build_argv("m", (), "hello", None), "empty trusts nothing")

    def test_resume_and_dash_prompt(self) -> None:
        argv = build_argv("m", (), "-rf", SHELL_OK_SESSION)
        self.assertEqual(argv[argv.index("--resume-id") + 1], SHELL_OK_SESSION)
        self.assertEqual(argv[-2:], ["--", "-rf"], "a dash prompt is never read as a flag")


if __name__ == "__main__":
    unittest.main()
