"""Goal: `eval report` turns ledger lines into the 7.1 ablation verdict, and advises demote or prune
only on enough cases. Method: synthetic ledgers; assert on the verdict text, the raw cells and the
status line. Each test has a planted case where the verdict must flip, so a report that always says
one thing fails."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any

from lib.cli import EVALS_ROOT
from lib.report import (
    FileReport,
    LedgerRun,
    ReportLimits,
    build_report,
    latest_runs,
    load_report_limits,
    paired_diff,
    parse_report_args,
    pass_pow_k,
    pass_rate,
    read_ledger,
    render_table,
    run_report,
)

LIMITS = ReportLimits(
    resamples=500,
    min_cases_for_ci=8,
    ledger_lines_max=1000,
    demote_words_max=350,
    demote_cases_min=12,
    prune_cases_min=16,
)
SHORT_RULE_WORDS = 120
MODEL = "claude-haiku-5.5"
SUITE = "steering/shell"


def run(case: str, arm: str, passed: bool, **over: Any) -> LedgerRun:
    base = LedgerRun(
        run_id=f"{case}--{arm}--r0",
        case_id=case,
        arm=arm,
        repeat_index=0,
        model_id=MODEL,
        status="ok",
        passed=passed,
        credits=0.01,
    )
    return replace(base, **over)


def ledger_row(case: str, arm: str, passed: bool, **over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "run_id": f"{case}--{arm}--r0--1",
        "case_id": case,
        "arm": arm,
        "repeat_index": 0,
        "model_id": MODEL,
        "status": "ok",
        "case_passed": passed,
        "credits": 0.01,
    }
    row.update(over)
    return row


def grid(outcomes: dict[str, tuple[bool, bool, bool]]) -> list[LedgerRun]:
    """case -> (full, minus-shell, none) pass flags."""
    runs: list[LedgerRun] = []
    for case, (full, minus, none) in outcomes.items():
        runs += [run(case, "full", full), run(case, "minus-shell", minus), run(case, "none", none)]
    return runs


def report_for(runs: list[LedgerRun], words: int | None = SHORT_RULE_WORDS) -> FileReport:
    return build_report(SUITE, MODEL, runs, 3104, words, LIMITS)


class ScoringTest(unittest.TestCase):
    def test_pass_rate_and_pow_k_differ_when_one_run_fails(self) -> None:
        self.assertEqual(pass_rate([True, True, False]), 2 / 3)
        self.assertEqual(pass_pow_k([True, True, False]), 0.0)
        self.assertEqual(pass_pow_k([True, True, True]), 1.0)
        self.assertEqual(pass_pow_k([True]), pass_rate([True]))

    def test_paired_diff_below_min_cases_has_no_interval(self) -> None:
        result = paired_diff([1.0] * 7, [0.0] * 7, 1, resamples=500, min_cases=8)
        self.assertEqual((result.n_cases, result.mean_diff), (7, 1.0))
        self.assertIsNone(result.ci90_low)
        self.assertIsNone(result.ci90_high)

    def test_paired_diff_constant_gap_gives_a_point_interval(self) -> None:
        result = paired_diff([1.0] * 8, [0.0] * 8, 1, resamples=500, min_cases=8)
        self.assertEqual((result.ci90_low, result.ci90_high), (1.0, 1.0))

    def test_paired_diff_mixed_interval_brackets_the_mean_and_is_seeded(self) -> None:
        with_arm = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 0.0, 1.0, 0.0, 1.0]
        without = [0.0, 1.0, 0.0, 1.0, 1.0, 0.0, 0.0, 1.0, 1.0, 1.0]
        first = paired_diff(with_arm, without, 7, resamples=500, min_cases=8)
        again = paired_diff(with_arm, without, 7, resamples=500, min_cases=8)
        self.assertEqual(first, again)
        assert first.ci90_low is not None and first.ci90_high is not None
        self.assertLessEqual(first.ci90_low, first.mean_diff)
        self.assertGreaterEqual(first.ci90_high, first.mean_diff)
        self.assertLess(first.ci90_low, first.ci90_high)

    def test_paired_diff_empty_is_zero_cases(self) -> None:
        self.assertEqual(paired_diff([], [], 1, resamples=10, min_cases=8).n_cases, 0)


class LedgerTest(unittest.TestCase):
    def read(self, lines: list[str]) -> list[LedgerRun] | str:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "l.jsonl"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return read_ledger(path, 100)

    def test_good_lines_parse_and_blank_lines_are_skipped(self) -> None:
        runs = self.read(
            [
                json.dumps(ledger_row("c1", "full", True)),
                "",
                json.dumps(ledger_row("c2", "none", False)),
            ]
        )
        assert not isinstance(runs, str)
        self.assertEqual(
            [(r.case_id, r.arm, r.passed) for r in runs],
            [("c1", "full", True), ("c2", "none", False)],
        )

    def test_bad_json_mistyped_field_and_missing_file_are_reasons_not_skips(self) -> None:
        self.assertIn("line 1", str(self.read(["{nope"])))
        self.assertIn(
            "line 1",
            str(self.read([json.dumps(ledger_row("c1", "full", True, case_passed="yes"))])),
        )
        self.assertIn(
            "line 1",
            str(self.read([json.dumps(ledger_row("c1", "full", True, repeat_index=True))])),
        )
        self.assertIn("cannot read", str(read_ledger(Path("/nonexistent/l.jsonl"), 10)))

    def test_over_the_line_bound_is_a_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "l.jsonl"
            path.write_text("{}\n{}\n{}\n", encoding="utf-8")
            self.assertIn("limit is 2", str(read_ledger(path, 2)))

    def test_later_line_wins_and_other_models_are_dropped(self) -> None:
        old = run("c1", "full", False)
        new = run("c1", "full", True, run_id="later")
        other = run("c1", "full", False, model_id="other-model")
        kept = latest_runs([old, new, other], MODEL)
        self.assertEqual([(r.run_id, r.passed) for r in kept], [("later", True)])


class VerdictTest(unittest.TestCase):
    def cases(self, count: int) -> list[str]:
        return [f"c{i}" for i in range(count)]

    def test_removal_breaking_eight_cases_is_keep(self) -> None:
        report = report_for(grid({c: (True, False, False) for c in self.cases(8)}))
        self.assertTrue(report.verdict.startswith("keep"), report.verdict)
        self.assertEqual(len(report.changed), 8)

    def test_removal_breaking_few_cases_is_watch_with_raw_counts(self) -> None:
        outcomes = {c: (True, True, True) for c in self.cases(3)}
        outcomes["c0"] = (True, False, False)
        report = report_for(grid(outcomes))
        self.assertTrue(report.verdict.startswith("watch"), report.verdict)
        self.assertEqual(report.changed, ("c0",))
        assert report.vs_minus is not None
        self.assertIsNone(report.vs_minus.ci90_low)

    def test_null_with_a_clean_none_arm_is_non_discriminating(self) -> None:
        report = report_for(grid({c: (True, True, True) for c in self.cases(8)}))
        self.assertTrue(report.verdict.startswith("non-discriminating"), report.verdict)

    def test_null_but_none_arm_fails_once_is_watch_not_delete(self) -> None:
        outcomes = {c: (True, True, True) for c in self.cases(8)}
        outcomes["c3"] = (True, True, False)
        report = report_for(grid(outcomes))
        self.assertTrue(report.verdict.startswith("watch"), report.verdict)
        self.assertEqual(report.none_failed, ("c3",))
        self.assertNotIn("delete", report.verdict.replace("not deletable", ""))

    def test_case_failing_in_full_is_broken_and_out_of_the_tally(self) -> None:
        outcomes = {c: (True, False, False) for c in self.cases(8)}
        outcomes["c0"] = (False, False, False)
        report = report_for(grid(outcomes))
        self.assertEqual(report.broken, ("c0",))
        assert report.vs_minus is not None
        self.assertEqual(report.vs_minus.n_cases, 7)

    def test_no_minus_runs_is_incomplete(self) -> None:
        runs = [run(c, "full", True) for c in self.cases(3)]
        self.assertTrue(report_for(runs).verdict.startswith("incomplete"))

    def test_no_case_passes_in_full_is_incomplete(self) -> None:
        report = report_for(grid({c: (False, False, False) for c in self.cases(3)}))
        self.assertIn("no case passes in full", report.verdict)

    def test_unfinished_runs_are_excluded_and_counted(self) -> None:
        runs = grid({c: (True, True, True) for c in self.cases(2)})
        runs.append(run("c0", "none", False, status="contaminated", run_id="x"))
        runs.append(run("c1", "none", False, status="harness_error", run_id="y"))
        report = report_for(runs)
        self.assertEqual(report.excluded_runs, 2)
        self.assertEqual(report.none_failed, ())

    def test_repeats_make_the_sanity_gate_need_every_run(self) -> None:
        runs = [run("c0", "full", True), run("c0", "full", False, repeat_index=1, run_id="r1")]
        report = report_for(runs)
        self.assertEqual(report.broken, ("c0",))


class NullAdviceTest(unittest.TestCase):
    """Goal: a null result becomes prune or demote exactly at the limits.json thresholds, and never
    beats a case the removal changed. Method: grids one case either side of each threshold."""

    def outcomes(self, count: int) -> dict[str, tuple[bool, bool, bool]]:
        return {f"c{i}": (True, True, True) for i in range(count)}

    def test_no_lift_over_sixteen_cases_with_a_failing_none_arm_is_prune_to_merge(self) -> None:
        for count, head in ((16, "prune"), (15, "watch")):
            outcomes = self.outcomes(count)
            outcomes["c3"] = (True, True, False)
            verdict = report_for(grid(outcomes)).verdict
            self.assertEqual(verdict.partition(":")[0], head, (count, verdict))
        merge = report_for(grid({**self.outcomes(16), "c3": (True, True, False)})).verdict
        self.assertIn("merge", merge)
        self.assertIn("one file at a time", merge)

    def test_no_lift_over_sixteen_clean_cases_is_prune_to_cut(self) -> None:
        cut = report_for(grid(self.outcomes(16))).verdict
        self.assertTrue(cut.startswith("prune") and "cut it" in cut, cut)
        under = report_for(grid(self.outcomes(15))).verdict
        self.assertTrue(under.startswith("non-discriminating"), under)

    def test_a_long_rule_with_no_lift_over_twelve_cases_is_demote(self) -> None:
        demoted = report_for(grid(self.outcomes(12)), words=351).verdict
        self.assertTrue(demoted.startswith("demote") and "351 words" in demoted, demoted)
        for count, words in ((12, 350), (11, 351), (12, None)):
            verdict = report_for(grid(self.outcomes(count)), words=words).verdict
            self.assertTrue(verdict.startswith("non-discriminating"), (count, words, verdict))

    def test_demote_beats_watch_when_the_none_arm_fails(self) -> None:
        outcomes = {**self.outcomes(12), "c3": (True, True, False)}
        self.assertTrue(report_for(grid(outcomes), words=400).verdict.startswith("demote"))
        self.assertTrue(report_for(grid(outcomes)).verdict.startswith("watch"))

    def test_a_changed_case_or_keep_beats_prune_and_demote(self) -> None:
        one_changed = {**self.outcomes(16), "c0": (True, False, False)}
        verdict = report_for(grid(one_changed), words=999).verdict
        self.assertTrue(verdict.startswith("watch: removal changed 1"), verdict)
        all_changed = {c: (True, False, False) for c in self.outcomes(16)}
        self.assertTrue(report_for(grid(all_changed), words=999).verdict.startswith("keep"))

    def test_prune_counts_cases_the_minus_arm_ran_not_cases_in_full(self) -> None:
        # 16 pass in full, but only 15 have a minus run: too few to prune
        runs = [r for r in grid(self.outcomes(16)) if (r.case_id, r.arm) != ("c0", "minus-shell")]
        verdict = report_for(runs).verdict
        self.assertTrue(verdict.startswith("non-discriminating"), verdict)

    def test_prune_floor_below_the_demote_floor_is_refused(self) -> None:
        raw = json.loads((EVALS_ROOT / "limits.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "limits.json"
            for key, value in (
                ("PRUNE_CASES_MIN", 11),
                ("DEMOTE_CASES_MIN", 17),
                ("DEMOTE_WORDS_MAX", 0),
            ):
                path.write_text(json.dumps({**raw, key: value}), encoding="utf-8")
                with self.assertRaises(ValueError, msg=key):
                    load_report_limits(path)


class RenderAndCliTest(unittest.TestCase):
    def test_table_shows_raw_cells_and_the_status_line_last(self) -> None:
        report = report_for(grid({"c0": (True, False, True)}))
        lines = render_table(report, LIMITS)
        self.assertTrue(any(line.startswith("c0") and "1/1" in line for line in lines))
        self.assertTrue(lines[-1].startswith("report: suite=steering/shell"))
        self.assertIn("file_bytes=3104 file_words=120", lines[0])
        unread = render_table(report_for(grid({"c0": (True, False, True)}), words=None), LIMITS)
        self.assertIn("file_words=?", unread[0])

    def test_parse_args(self) -> None:
        home = Path("/home/x/.kiro")
        parsed = parse_report_args(["steering/shell", "--model", "m", "--kiro-home", "/k"], home)
        assert not isinstance(parsed, str)
        self.assertEqual((parsed[1], parsed[2]), ("m", Path("/k")))
        first = parse_report_args(["steering/shell"], home)
        assert not isinstance(first, str)
        self.assertEqual(first[0], "steering/shell")
        for bad in (
            [],
            ["skill/x"],
            ["steering/"],
            ["steering/shell", "--model"],
            ["steering/shell", "--nope", "1"],
        ):
            self.assertIsInstance(parse_report_args(bad, home), str, bad)

    def test_run_report_end_to_end_and_error_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, home = Path(tmp) / "evals", Path(tmp) / "kiro"
            (root / "ledger").mkdir(parents=True)
            (home / "steering").mkdir(parents=True)
            (home / "steering" / "shell.md").write_text("word " * 8, encoding="utf-8")
            (root / "limits.json").write_text(
                (EVALS_ROOT / "limits.json").read_text(encoding="utf-8"), encoding="utf-8"
            )
            args = ["steering/shell", "--kiro-home", str(home)]
            missing, code = run_report(args, root, home)
            self.assertEqual(code, 1)
            self.assertIn("no ledger", missing[-1])
            rows = [
                ledger_row("c0", "full", True),
                ledger_row("c0", "minus-shell", False),
                ledger_row("c0", "none", False),
            ]
            (root / "ledger" / "steering_shell.jsonl").write_text(
                "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
            )
            lines, code = run_report(args, root, home)
            self.assertEqual(code, 0)
            self.assertIn("file_bytes=40 file_words=8", lines[0])
            self.assertTrue(lines[-1].startswith("report: "), lines[-1])
            usage, code = run_report(["nope"], root, home)
            self.assertEqual((code, len(usage)), (2, 1))

    def test_no_ledger_directory_reports_no_ledger_and_exits_1(self) -> None:
        # the public copy ships without ledger/: a fresh clone must say so, not crash
        with tempfile.TemporaryDirectory() as tmp:
            root, home = Path(tmp) / "evals", Path(tmp) / "home"
            root.mkdir()
            (home / "steering").mkdir(parents=True)
            shutil.copy(EVALS_ROOT / "limits.json", root / "limits.json")
            lines, code = run_report(["steering/shell", "--kiro-home", str(home)], root, home)
            self.assertEqual(code, 1)
            self.assertEqual(lines[-1], "report: suite=steering/shell no ledger")
            self.assertFalse((root / "ledger").exists(), "a report never creates the ledger")

    def test_real_limits_json_carries_the_report_constants(self) -> None:
        limits = load_report_limits(EVALS_ROOT / "limits.json")
        self.assertEqual((limits.resamples, limits.min_cases_for_ci), (2000, 8))
        advice = (limits.demote_words_max, limits.demote_cases_min, limits.prune_cases_min)
        self.assertEqual(advice, (350, 12, 16))
        self.assertGreater(limits.ledger_lines_max, 0)


class SkillInsteadTest(unittest.TestCase):
    """Goal: a long rule that does work (removal changes a case) but held every case as a skill gets
    the demote advice, exactly at the limits.json floors; a case the skill arm drops keeps it always
    on. Method: grids with a skill-instead arm, one step either side of each floor."""

    def runs(
        self, count: int, *, skill_fails: str = "", no_skill: str = "", minus_fails: int = 0
    ) -> list[LedgerRun]:
        # minus_fails 0: the minus arm fails every case; n: only the first n
        out: list[LedgerRun] = []
        for index in range(count):
            case = f"c{index}"
            minus_passed = minus_fails > 0 and index >= minus_fails
            out += [run(case, "full", True), run(case, "minus-shell", minus_passed)]
            if case != no_skill:
                out.append(run(case, "skill-instead", case != skill_fails))
        return out

    def test_a_long_rule_that_held_as_a_skill_is_demote(self) -> None:
        report = report_for(self.runs(12), words=351)
        self.assertTrue(report.verdict.startswith("demote"), report.verdict)
        self.assertIn("as a skill it held all 12 cases", report.verdict)
        self.assertEqual(report.skill_changed, ())
        assert report.vs_skill is not None
        self.assertEqual(report.vs_skill.n_cases, 12)

    def test_one_changed_case_is_enough_to_ask(self) -> None:
        report = report_for(self.runs(12, minus_fails=1), words=351)
        self.assertEqual(report.changed, ("c0",))
        self.assertIn("as a skill it held all 12 cases", report.verdict)

    def test_each_floor_keeps_the_rule_always_on(self) -> None:
        no_skill_runs = [r for r in self.runs(12) if r.arm != "skill-instead"]
        for label, runs, words in (
            ("short rule", self.runs(12), 350),
            ("unread size", self.runs(12), None),
            ("too few cases", self.runs(11), 351),
            ("a case has no skill run", self.runs(12, no_skill="c0"), 351),
            ("the skill dropped a case", self.runs(12, skill_fails="c3"), 351),
            ("no skill runs", no_skill_runs, 351),
        ):
            with self.subTest(label):
                verdict = report_for(runs, words=words).verdict
                self.assertTrue(verdict.startswith("keep"), verdict)
        dropped = report_for(self.runs(12, skill_fails="c3"), words=351)
        self.assertEqual(dropped.skill_changed, ("c3",))
        self.assertIsNone(report_for(no_skill_runs, words=351).vs_skill)

    def test_render_shows_the_skill_column_and_its_lines(self) -> None:
        lines = render_table(report_for(self.runs(12, skill_fails="c3"), words=351), LIMITS)
        self.assertTrue(lines[1].rstrip().endswith("skill-instead"), lines[1])
        self.assertTrue(any(line.startswith("c3 ") and line.endswith("0/1") for line in lines))
        self.assertTrue(any(line.startswith("full vs skill-instead: n=12") for line in lines))
        self.assertIn("changed as a skill: c3", lines)


if __name__ == "__main__":
    unittest.main()
