"""Goal: every release-1 grader passes on evidence it should accept and FAILs on a planted
failure, and a grader with no evidence FAILs, never passes (design 3.5, debugging-discipline #5).
Method: build events by hand, grade them, assert on the verdict and on the evidence text. The
ledger tests assert the file's lines, not a return code."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path

from lib.dataset import GRADER_ARGS, Assertion
from lib.grade import (
    GRADERS,
    Finding,
    Planted,
    append_run,
    case_passed,
    extracted_python,
    grade_run,
    match_findings,
    parse_findings,
    record_to_json,
)
from lib.run import Event, RunRecord, RunStatus, RunView, Verdict
from tests.helpers_run import call, make_event, make_spec, make_view

LONG_FUNCTION = "def f(x: int) -> int:\n" + "    x += 1\n" * 80 + "    return x\n"
SMALL_FUNCTION = "def f(x: int) -> int:\n    assert x >= 0\n    assert x < 9\n    return x\n"
FISH_LOOP = "for i in 1 2 3; echo $i; end"
BASH_LOOP = "for i in 1 2 3; do echo $i; done"


def verdict(
    grader: str, view: RunView, args: Mapping[str, object] | None = None, root: Path | None = None
) -> Verdict:
    return GRADERS[grader](view, dict(args or {}), root or Path("/nonexistent"))[0]


def shell(command: str) -> Event:
    return call("execute_bash", {"command": command})


def writes(path: str, text: str) -> Event:
    return call("fs_write", {"path": path, "text": text})


def final(text: str) -> RunView:
    return make_view([], final_text=text)


def with_events(*events: Event) -> RunView:
    return make_view(list(events))


class UnitsInNamesTest(unittest.TestCase):
    ARGS = {"names": [{"identifier": "timeout", "suffixes": ["_ms", "_s"]}]}

    def grade(self, code: str) -> Verdict:
        return verdict("g_units_in_names", with_events(writes("m.py", code)), self.ARGS)

    def test_docstring_and_comment_mentions_are_not_identifiers(self) -> None:
        code = (
            'def f(timeout_ms: int) -> int:\n    """Raises on timeout."""\n'
            "    # timeout\n    return timeout_ms\n"
        )
        self.assertEqual(self.grade(code), "PASS")

    def test_seconds_suffix_is_a_unit_when_the_case_lists_it(self) -> None:
        args = {"names": [{"identifier": "timeout", "suffixes": ["_ms", "_s", "_seconds"]}]}
        code = "def f(timeout_seconds: int) -> int:\n    return timeout_seconds\n"
        got = verdict("g_units_in_names", with_events(writes("m.py", code)), args)
        self.assertEqual(got, "PASS")
        self.assertEqual(self.grade(code), "FAIL", "planted twin: the v1 list rejects _seconds")
        bad = "def f(timeout_secs: int) -> int:\n    return timeout_secs\n"
        got_bad = verdict("g_units_in_names", with_events(writes("m.py", bad)), args)
        self.assertEqual(got_bad, "FAIL", "an unlisted spelling still fails")

    def test_unit_less_name_fails_and_absent_name_fails(self) -> None:
        self.assertEqual(self.grade("timeout = 5\n"), "FAIL")
        self.assertEqual(self.grade("timeout_ms = 5\ntimeout = 5\n"), "FAIL")
        self.assertEqual(self.grade("x = 5\n"), "FAIL", "no evidence is FAIL")
        self.assertEqual(self.grade("timeout_s = 1\n"), "PASS")

    def test_attribute_argument_and_keyword_names_count(self) -> None:
        self.assertEqual(self.grade("def f(self):\n    return self.timeout\n"), "FAIL")
        self.assertEqual(self.grade("g(timeout=3)\n"), "FAIL")
        self.assertEqual(self.grade("g(timeout_ms=3)\n"), "PASS")


class GraderTableTest(unittest.TestCase):
    def test_every_release_one_grader_is_built(self) -> None:
        self.assertEqual(set(GRADERS), set(GRADER_ARGS))

    def test_pass_and_planted_failure_per_grader(self) -> None:
        for grader, args, good, bad in [*self.tool_rows(), *self.code_rows()]:
            with self.subTest(grader=grader):
                self.assertEqual(verdict(grader, good, args), "PASS")
                self.assertEqual(verdict(grader, bad, args), "FAIL")

    def tool_rows(self) -> list[tuple[str, dict[str, object], RunView, RunView]]:
        skill = [make_event("skill_read", "tiger-style")]
        return [
            ("g_skill_read", {"skill": "tiger-style"}, make_view(skill), make_view([])),
            ("g_skill_not_read", {"skill": "tiger-style"}, make_view([]), make_view(skill)),
            ("g_tool_used", {"tool": "execute_bash"}, with_events(shell("ls")), with_events()),
            ("g_tool_not_used", {"tool": "execute_bash"}, with_events(), with_events(shell("ls"))),
            (
                "g_tool_args_match",
                {"tool": "execute_bash", "arg_regex": "ls", "expect": "present"},
                with_events(shell("ls")),
                with_events(shell("pwd")),
            ),
            (
                "g_calls_max",
                {"tool": None, "max": 1},
                with_events(shell("ls")),
                with_events(shell("a"), shell("b")),
            ),
            (
                "g_text_matches",
                {"regex": "^hi", "scope": "final_text"},
                final("hi there"),
                final("nope"),
            ),
            (
                "g_text_absent",
                {"regex": "bad", "scope": "final_text"},
                final("fine"),
                final("so bad"),
            ),
            ("g_fish_parses", {}, with_events(shell(FISH_LOOP)), with_events(shell(BASH_LOOP))),
            (
                "g_no_heredoc",
                {},
                with_events(shell("echo hi")),
                with_events(shell("cat <<'EOF'\nx\nEOF")),
            ),
            (
                "g_commit_message_clean",
                {"banned_regex": ["DO-NOT-SHIP"]},
                with_events(shell('git commit -m "fix: x"')),
                with_events(shell('git commit -m "x\n\nDO-NOT-SHIP: Claude"')),
            ),
        ]

    def code_rows(self) -> list[tuple[str, dict[str, object], RunView, RunView]]:
        return [
            (
                "g_function_length_max",
                {"max_lines": 70},
                with_events(writes("a.py", SMALL_FUNCTION)),
                with_events(writes("a.py", LONG_FUNCTION)),
            ),
            (
                "g_assertions_per_function_min",
                {"min": 2},
                with_events(writes("a.py", SMALL_FUNCTION)),
                with_events(writes("a.py", LONG_FUNCTION)),
            ),
            (
                "g_loops_bounded",
                {},
                with_events(writes("a.py", "i = 0\nwhile i < 3:\n    i += 1\n")),
                with_events(writes("a.py", "while True:\n    pass\n")),
            ),
            (
                "g_units_in_names",
                {"names": [{"identifier": "timeout", "suffixes": ["_ms", "_s"]}]},
                with_events(writes("a.py", "timeout_ms = 5\n")),
                with_events(writes("a.py", "timeout = 5\n")),
            ),
            ("g_no_outside_read", {}, make_view([]), make_view([], status="contaminated")),
            ("g_stderr_empty", {}, make_view([]), make_view([], stderr_text="[denied] x")),
        ]

    def test_no_evidence_is_fail_not_pass(self) -> None:
        empty = with_events()
        self.assertEqual(verdict("g_fish_parses", empty), "FAIL")
        self.assertEqual(verdict("g_no_heredoc", empty), "FAIL")
        self.assertEqual(verdict("g_commit_message_clean", empty, {"banned_regex": ["x"]}), "FAIL")
        absent = {"tool": "execute_bash", "arg_regex": "x", "expect": "absent"}
        self.assertEqual(verdict("g_tool_args_match", empty, absent), "FAIL", "zero calls, absent")
        self.assertEqual(
            verdict("g_text_absent", final(""), {"regex": "x", "scope": "final_text"}), "FAIL"
        )
        self.assertEqual(
            verdict("g_text_matches", final("  \n"), {"regex": ".", "scope": "final_text"}), "FAIL"
        )
        for grader, args in (
            ("g_function_length_max", {"max_lines": 70}),
            ("g_loops_bounded", {}),
            ("g_assertions_per_function_min", {"min": 2}),
            ("g_pyright_clean", {}),
            ("g_units_in_names", {"names": [{"identifier": "t", "suffixes": ["_s"]}]}),
        ):
            self.assertEqual(verdict(grader, empty, args), "FAIL", grader)

    def test_tool_used_counts_denied_attempts_and_min_count(self) -> None:
        denied = Event(0, "tool_call", "execute_bash", "{}", "denied", None, "c", ())
        view = make_view([denied])
        self.assertEqual(verdict("g_tool_used", view, {"tool": "execute_bash"}), "PASS")
        self.assertEqual(
            verdict("g_tool_used", view, {"tool": "execute_bash", "min_count": 2}), "FAIL"
        )

    def test_fish_parse_asserts_stderr_not_just_status(self) -> None:
        self.assertEqual(verdict("g_fish_parses", with_events(shell("echo (")), {}), "FAIL")
        self.assertEqual(
            verdict("g_fish_parses", with_events(shell(FISH_LOOP), shell(BASH_LOOP))), "FAIL"
        )

    def test_scopes(self) -> None:
        view = make_view(
            [writes("a.py", "x = 1  # was removed")], final_text="\nThis is an Elephant task\nmore"
        )
        first = {"regex": "Elephant", "scope": "first_line"}
        self.assertEqual(verdict("g_text_matches", view, first), "PASS")
        late = {"regex": "more", "scope": "first_line"}
        self.assertEqual(verdict("g_text_matches", view, late), "FAIL")
        self.assertEqual(
            verdict("g_text_absent", view, {"regex": "was removed", "scope": "write_inputs"}),
            "FAIL",
        )


@unittest.skipUnless(shutil.which("pyright"), "pyright is not installed")
class PyrightTest(unittest.TestCase):
    def test_typed_passes_untyped_fails(self) -> None:
        clean = with_events(writes("a.py", "def f(x: int) -> int:\n    return x\n"))
        loose = with_events(writes("a.py", "def f(x):\n    return x\n"))
        self.assertEqual(verdict("g_pyright_clean", clean), "PASS")
        failing = GRADERS["g_pyright_clean"](loose, {}, Path("/x"))
        self.assertEqual(failing[0], "FAIL")
        self.assertIn("error", failing[1])


class ExtractionTest(unittest.TestCase):
    def test_first_write_per_file_wins_and_fence_is_the_fallback(self) -> None:
        view = with_events(
            writes("a.py", "first = 1\n"), writes("a.py", "second = 2\n"), writes("b.md", "x")
        )
        self.assertEqual(extracted_python(view), ["first = 1\n"])
        fenced = final("text\n```python\nx = 1\n```\n```fish\necho hi\n```\n```\ny = 2\n```")
        self.assertEqual(extracted_python(fenced), ["x = 1\n", "y = 2\n"])

    def test_syntax_error_is_fail_with_evidence(self) -> None:
        result = GRADERS["g_function_length_max"](
            with_events(writes("a.py", "def (:\n")), {"max_lines": 5}, Path("/x")
        )
        self.assertEqual(result[0], "FAIL")
        self.assertIn("does not parse", result[1])

    def test_function_length_skips_docstring_and_decorators(self) -> None:
        body = "    x = 1\n    return x\n"
        doc = '    """one\n    two\n    three"""\n'
        code = "@a\n@b\ndef f() -> int:\n" + doc + body
        grade = GRADERS["g_function_length_max"]
        self.assertEqual(
            grade(with_events(writes("a.py", code)), {"max_lines": 3}, Path("/x"))[0], "PASS"
        )
        result = grade(with_events(writes("a.py", code)), {"max_lines": 2}, Path("/x"))
        self.assertEqual(result[0], "FAIL")
        self.assertIn("f is 3 lines, max 2", result[1])

    def test_nested_function_asserts_do_not_count_for_the_outer(self) -> None:
        code = (
            "def outer() -> None:\n    def inner() -> None:\n"
            "        assert 1\n        assert 2\n    inner()\n"
        )
        result = GRADERS["g_assertions_per_function_min"](
            with_events(writes("a.py", code)), {"min": 2}, Path("/x")
        )
        self.assertEqual(result[0], "FAIL")
        self.assertIn("outer", result[1])


class FindingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "planted").mkdir()
        items = [
            {
                "id": "p1",
                "file": "src/a.py",
                "line": 10,
                "category": "unbounded_loop",
                "in_tiger_style_categories": True,
                "allowed_severities": ["P0", "P1"],
            },
            {
                "id": "p2",
                "file": "src/a.py",
                "line": 40,
                "category": "naming",
                "in_tiger_style_categories": False,
                "allowed_severities": ["P2"],
            },
        ]
        (self.root / "planted" / "fx.json").write_text(
            json.dumps({"fixture": "fx", "items": items})
        )
        self.cwd = self.root / "cwd"
        (self.cwd / "src").mkdir(parents=True)
        (self.cwd / "src" / "a.py").write_text("\n".join(f"line{n}" for n in range(60)))

    def view(self, text: str) -> RunView:
        return make_view([], final_text=text, cwd=str(self.cwd))

    def grade(self, text: str, **args: object) -> tuple[Verdict, str]:
        full = {"planted": "planted/fx.json", **args}
        return GRADERS["g_planted_found"](self.view(text), full, self.root)

    def test_recall_precision_severity(self) -> None:
        text = "FINDING P0 src/a.py:11 unbounded\nFINDING P1 src/a.py:41 naming\nFINDINGS_DONE\n"
        recall = self.grade(text, mode="recall", min=1.0)
        self.assertEqual(recall[0], "PASS")
        self.assertIn("in-category 1/1", recall[1])
        self.assertIn("outside 1/1", recall[1])
        self.assertEqual(self.grade(text, mode="precision", min=1.0)[0], "PASS")
        self.assertEqual(
            self.grade(text, mode="severity", min=1.0)[0], "FAIL", "P1 is not allowed on p2"
        )
        self.assertEqual(self.grade(text, mode="severity", min=0.5)[0], "PASS")

    def test_precision_and_severity_evidence_carry_raw_counts(self) -> None:
        text = "FINDING P0 src/a.py:10 a\nFINDING P0 src/a.py:12 b\nFINDINGS_DONE\n"
        self.assertTrue(self.grade(text, mode="precision", min=0.1)[1].endswith("; 1/2"))
        self.assertTrue(self.grade(text, mode="severity", min=0.1)[1].endswith("; 1/1"))

    def test_precision_denominator_is_findings_not_items(self) -> None:
        text = (
            "FINDING P0 src/a.py:10 a\nFINDING P2 src/a.py:40 b\nFINDING P2 src/a.py:55 c\n"
            "FINDINGS_DONE\n"
        )
        verdict, evidence = self.grade(text, mode="precision", min=0.9)
        self.assertEqual(verdict, "FAIL", "2 of 3 findings matched: 0.67 < 0.9")
        self.assertTrue(evidence.startswith("precision 0.67"), evidence)

    def write_extras(self, extras: list[dict[str, object]]) -> None:
        path = self.root / "planted" / "fx.json"
        data = json.loads(path.read_text())
        data["acceptable_extras"] = extras
        path.write_text(json.dumps(data))

    def extra(self, ident: str, line: int) -> dict[str, object]:
        return {"id": ident, "file": "src/a.py", "line": line, "note": "real issue"}

    def test_acceptable_extras_leave_the_precision_denominator(self) -> None:
        text = (
            "FINDING P0 src/a.py:10 a\nFINDING P2 src/a.py:20 x\nFINDING P2 src/a.py:25 y\n"
            "FINDINGS_DONE\n"
        )
        self.assertEqual(self.grade(text, mode="precision", min=0.6)[0], "FAIL", "v1: 1/3")
        self.write_extras([self.extra("x1", 20), self.extra("x2", 25)])
        verdict, evidence = self.grade(text, mode="precision", min=0.6)
        self.assertEqual(verdict, "PASS", "v2: 1/1")
        self.assertIn("2 acceptable extras excluded", evidence)
        self.assertTrue(evidence.endswith("; 1/1"), evidence)

    def test_extras_are_one_to_one_and_unlisted_findings_still_count_against(self) -> None:
        self.write_extras([self.extra("x1", 20)])
        text = (
            "FINDING P0 src/a.py:10 a\nFINDING P2 src/a.py:20 x\nFINDING P2 src/a.py:21 dup\n"
            "FINDING P2 src/a.py:50 unlisted\nFINDINGS_DONE\n"
        )
        verdict, evidence = self.grade(text, mode="precision", min=0.5)
        self.assertEqual(verdict, "FAIL", "1 of 3 counted findings matched")
        self.assertTrue(evidence.endswith("; 1/3"), evidence)

    def test_extras_never_steal_a_planted_item_or_change_recall(self) -> None:
        self.write_extras([self.extra("x1", 12)])  # sits next to planted p1 at line 10
        text = "FINDING P0 src/a.py:12 near\nFINDINGS_DONE\n"
        recall = self.grade(text, mode="recall", min=0.0)
        self.assertIn("in-category 1/1", recall[1], "planted pass runs first")
        self.assertIn("outside 0/1", recall[1])
        self.assertTrue(self.grade(text, mode="precision", min=0.0)[1].endswith("; 1/1"))

    def test_a_bad_extras_entry_is_unknown_not_a_pass(self) -> None:
        self.write_extras([{"id": "x1", "file": "src/a.py", "line": "20"}])
        text = "FINDING P0 src/a.py:10 a\nFINDINGS_DONE\n"
        self.assertEqual(self.grade(text, mode="precision", min=0.1)[0], "UNKNOWN")

    def test_two_findings_near_one_item_match_once(self) -> None:
        text = "FINDING P0 src/a.py:10 a\nFINDING P0 src/a.py:12 b\nFINDINGS_DONE\n"
        self.assertEqual(
            self.grade(text, mode="precision", min=0.6)[0], "FAIL", "second is a false claim"
        )
        self.assertEqual(self.grade(text, mode="recall", min=0.5)[0], "PASS")

    def test_no_done_line_or_malformed_finding_is_no_parse(self) -> None:
        self.assertEqual(
            self.grade("FINDING P0 src/a.py:10 x\n", mode="recall", min=0.0)[0], "FAIL"
        )
        self.assertEqual(
            self.grade("FINDING P9 src/a.py:10 x\nFINDINGS_DONE\n", mode="recall", min=0.0)[0],
            "FAIL",
        )
        self.assertEqual(self.grade("FINDINGS_DONE\nextra\n", mode="recall", min=0.0)[0], "FAIL")

    def test_out_of_tolerance_and_wrong_file_do_not_match(self) -> None:
        near = Planted("p", "src/a.py", 10, True, ("P0",))
        findings = [Finding("P0", "src/a.py", 14, "x"), Finding("P0", "src/b.py", 10, "x")]
        self.assertEqual(match_findings(findings, [near]), {})
        self.assertEqual(list(match_findings([Finding("P0", "src/a.py", 13, "x")], [near])), [0])

    def test_clean_control_quiet(self) -> None:
        quiet = self.view("FINDING P3 src/a.py:2 nit\nFINDINGS_DONE")
        loud = self.view("FINDING P1 src/a.py:2 bad\nFINDINGS_DONE")
        self.assertEqual(GRADERS["g_clean_control_quiet"](quiet, {}, self.root)[0], "PASS")
        self.assertEqual(GRADERS["g_clean_control_quiet"](loud, {}, self.root)[0], "FAIL")
        self.assertEqual(GRADERS["g_clean_control_quiet"](self.view(""), {}, self.root)[0], "FAIL")

    def test_citation_valid(self) -> None:
        good = self.view("FINDING P2 src/a.py:60 x\nFINDINGS_DONE")
        beyond = self.view("FINDING P2 src/a.py:61 x\nFINDINGS_DONE")
        missing = self.view("FINDING P2 src/zzz.py:1 x\nFINDINGS_DONE")
        escape = self.view("FINDING P2 ../../etc/passwd:1 x\nFINDINGS_DONE")
        empty = self.view("FINDINGS_DONE")
        verdicts = [
            GRADERS["g_citation_valid"](v, {}, self.root)[0]
            for v in (good, beyond, missing, escape, empty)
        ]
        self.assertEqual(verdicts, ["PASS", "FAIL", "FAIL", "FAIL", "FAIL"])

    def test_parse_findings_shape(self) -> None:
        found = parse_findings("noise\nFINDING P2 a/b.py:3 some text\nFINDINGS_DONE\n")
        self.assertEqual(found, [Finding("P2", "a/b.py", 3, "some text")])


class GradeRunTest(unittest.TestCase):
    def assertions(self) -> tuple[Assertion, ...]:
        return (
            Assertion("used", "g_tool_used", {"tool": "execute_bash"}, True),
            Assertion("soft", "g_tool_used", {"tool": "read_file"}, False),
            Assertion("h", "human", {"question": "is the voice terse?"}, False),
        )

    def test_hard_pass_ignores_soft_and_human_but_adds_the_implicit_grade(self) -> None:
        view = with_events(shell("ls"))
        grades = grade_run(view, self.assertions(), Path("/x"))
        self.assertEqual([g.assertion_id for g in grades], ["used", "soft", "h", "no_outside_read"])
        self.assertEqual([g.verdict for g in grades], ["PASS", "FAIL", "UNKNOWN", "PASS"])
        self.assertTrue(case_passed(grades, self.assertions(), "ok"))
        self.assertTrue(case_passed(grades, self.assertions(), "denied_only"))

    def test_unfinished_runs_grade_unknown_and_never_pass(self) -> None:
        statuses: tuple[RunStatus, ...] = ("harness_error", "timeout", "budget_abort")
        for status in statuses:
            view = make_view([shell("ls")], status=status)
            grades = grade_run(view, self.assertions(), Path("/x"))
            self.assertEqual(
                {g.verdict for g in grades if g.assertion_id != "no_outside_read"}, {"UNKNOWN"}
            )
            self.assertFalse(case_passed(grades, self.assertions(), status))

    def test_unknown_hard_assertion_does_not_pass(self) -> None:
        hard_human_free = (Assertion("used", "g_tool_used", {"tool": "execute_bash"}, True),)
        grades = grade_run(with_events(shell("ls")), hard_human_free, Path("/x"))
        flipped = [
            g if g.assertion_id != "used" else type(g)(g.assertion_id, "UNKNOWN", g.grader, "")
            for g in grades
        ]
        self.assertFalse(case_passed(flipped, hard_human_free, "ok"))

    def test_contaminated_run_cannot_pass(self) -> None:
        view = make_view([shell("ls")], status="contaminated")
        grades = grade_run(view, self.assertions(), Path("/x"))
        self.assertEqual(grades[-1].verdict, "FAIL")
        self.assertFalse(case_passed(grades, self.assertions(), "contaminated"))


def sample_record(run_id: str = "r1", status: RunStatus = "ok") -> RunRecord:
    spec = make_spec(run_id=run_id)
    return RunRecord(
        run_id=spec.run_id,
        case_id=spec.case_id,
        arm=spec.arm,
        repeat_index=0,
        engine="v3",
        model_id=spec.model_id,
        cli_version="3.2.1",
        home_sha="abc",
        case_sha="c0de",
        status=status,
        wall_ms=12,
        credits=0.003,
        context_pct_last=1.5,
        tool_call_count=1,
        denied_count=1,
        skills_read=("x",),
        session_id="sess_1",
        stream_path="/tmp/x",
        grades=(),
        case_passed=True,
        note="",
    )


class LedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "ledger" / "suite.jsonl"

    def test_append_adds_one_line_and_never_touches_earlier_ones(self) -> None:
        self.assertIsNone(append_run(self.path, sample_record("r1")))
        first_line = self.path.read_text().splitlines()[0]
        self.assertIsNone(append_run(self.path, sample_record("r2", "timeout")))
        lines = self.path.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], first_line)
        self.assertEqual([json.loads(line)["run_id"] for line in lines], ["r1", "r2"])

    def test_line_carries_arm_model_credits_and_version(self) -> None:
        row = record_to_json(sample_record())
        self.assertEqual(
            (row["arm"], row["model_id"], row["credits"], row["cli_version"]),
            ("full", "claude-haiku-5.5", 0.003, "3.2.1"),
        )
        self.assertEqual(row["engine"], "v3")

    def test_write_failure_is_a_reason_not_a_crash(self) -> None:
        blocker = Path(self.tmp.name) / "file"
        blocker.write_text("x")
        self.assertIn(
            "ledger write failed", str(append_run(blocker / "sub" / "l.jsonl", sample_record()))
        )

    def test_no_update_api_exists(self) -> None:
        import lib.grade as grade

        self.assertFalse(
            [n for n in dir(grade) if n.startswith(("update_", "delete_", "rewrite_"))]
        )


if __name__ == "__main__":
    unittest.main()
