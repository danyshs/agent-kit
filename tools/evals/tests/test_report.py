"""Goal: `eval report` turns ledger lines into the 7.1 ablation verdict without ever saying delete.
Method: synthetic ledgers; assert on the verdict text, the raw cells and the status line. Each
test has a planted case where the verdict must flip, so a report that always says one thing
fails."""

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

LIMITS = ReportLimits(resamples=500, min_cases_for_ci=8, ledger_lines_max=1000)
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


def report_for(runs: list[LedgerRun]) -> FileReport:
    return build_report(SUITE, MODEL, runs, 3104, LIMITS)


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


class RenderAndCliTest(unittest.TestCase):
    def test_table_shows_raw_cells_and_the_status_line_last(self) -> None:
        report = report_for(grid({"c0": (True, False, True)}))
        lines = render_table(report, LIMITS)
        self.assertTrue(any(line.startswith("c0") and "1/1" in line for line in lines))
        self.assertTrue(lines[-1].startswith("report: suite=steering/shell"))
        self.assertIn("file_bytes=3104", lines[0])

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
            (home / "steering" / "shell.md").write_text("x" * 40, encoding="utf-8")
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
            self.assertIn("file_bytes=40", lines[0])
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
        self.assertGreater(limits.ledger_lines_max, 0)


if __name__ == "__main__":
    unittest.main()
