"""Goal: `eval report` turns ledger lines into the 7.1 ablation verdict, and advises demote or prune
only on enough cases. Method: synthetic ledgers; assert on the verdict text, the raw cells and the
status line. Each test has a planted case where the verdict must flip, so a report that always says
one thing fails."""

from __future__ import annotations

import itertools
import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import Any

from lib.cli import EVALS_ROOT
from lib.dataset import case_sha
from lib.report import (
    FileReport,
    LedgerRun,
    ReportLimits,
    build_report,
    current_versions,
    effect_sign,
    latest_runs,
    load_report_limits,
    paired_diff,
    parse_report_args,
    pass_rate,
    read_ledger,
    render_table,
    run_report,
    sign_flip_p,
)

LIMITS = ReportLimits(
    min_cases_for_test=8,
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
        case_sha="v1",
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


def ones(count: int, value: int) -> list[Fraction]:
    return [Fraction(value)] * count


def brute_p(diffs: list[Fraction]) -> Fraction:
    """Every sign pattern spelled out: the definition sign_flip_p must match."""
    observed = abs(sum(diffs, Fraction(0)))
    patterns = list(itertools.product((1, -1), repeat=len(diffs)))
    extreme = sum(
        1
        for signs in patterns
        if abs(sum(s * d for s, d in zip(signs, diffs, strict=True))) >= observed
    )
    return Fraction(extreme, len(patterns))


class ScoringTest(unittest.TestCase):
    def test_pass_rate_is_exact(self) -> None:
        self.assertEqual(pass_rate([True, True, False]), Fraction(2, 3))
        self.assertEqual(pass_rate([True]), 1)

    def test_sign_flip_p_at_one_repeat_is_the_exact_sign_test(self) -> None:
        # (cases the rule helped, cases it harmed) -> p; ties carry no sign and don't count
        for helped, harmed, ties, p in (
            (6, 0, 0, Fraction(1, 32)),
            (6, 0, 2, Fraction(1, 32)),
            (6, 1, 1, Fraction(1, 8)),
            (4, 0, 4, Fraction(1, 8)),
            (4, 1, 3, Fraction(3, 8)),
            (8, 0, 0, Fraction(1, 128)),
            (8, 1, 0, Fraction(20, 512)),
            (0, 0, 8, Fraction(1)),
        ):
            diffs = ones(helped, 1) + ones(harmed, -1) + ones(ties, 0)
            self.assertEqual(sign_flip_p(diffs), p, (helped, harmed, ties))

    def test_sign_flip_p_matches_every_sign_pattern_on_repeat_rates(self) -> None:
        third = Fraction(1, 3)
        for diffs in (
            [third, 2 * third, -third, Fraction(1), 2 * third, Fraction(0), -2 * third],
            [Fraction(1, 2), Fraction(1, 2), Fraction(-1), Fraction(1, 2)],
            [third] * 9,
        ):
            self.assertEqual(sign_flip_p(diffs), brute_p(diffs), diffs)

    def test_paired_diff_counts_both_ways(self) -> None:
        result = paired_diff(
            [Fraction(1), Fraction(1), Fraction(0), Fraction(1)],
            [Fraction(0), Fraction(1), Fraction(1), Fraction(1)],
        )
        self.assertEqual((result.n_cases, result.helped, result.harmed), (4, 1, 1))
        self.assertEqual((result.mean_diff, result.p_two_sided), (0.0, 1.0))

    def test_paired_diff_empty_is_zero_cases(self) -> None:
        self.assertEqual(paired_diff([], []).n_cases, 0)

    def test_effect_sign_needs_the_case_floor_and_a_small_p(self) -> None:
        def sign(helped: int, harmed: int, ties: int) -> int:
            return effect_sign(
                paired_diff(
                    ones(helped, 1) + ones(harmed, 0) + ones(ties, 1),
                    ones(helped, 0) + ones(harmed, 1) + ones(ties, 1),
                ),
                LIMITS,
            )

        self.assertEqual(sign(8, 0, 0), 1)
        self.assertEqual(sign(0, 8, 0), -1)
        self.assertEqual(sign(7, 0, 0), 0, "n < 8 is no verdict whatever p says")
        self.assertEqual(sign(6, 0, 2), 1, "p = 1/32")
        self.assertEqual(sign(6, 1, 1), 0, "p = 1/8: one case the other way turns keep to watch")
        self.assertEqual(sign(5, 0, 3), 0, "p = 1/16 is over 0.05")
        self.assertEqual(effect_sign(None, LIMITS), 0)


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

    def test_case_sha_is_optional_but_never_blank_or_mistyped(self) -> None:
        old = self.read([json.dumps(ledger_row("c1", "full", True))])
        new = self.read([json.dumps(ledger_row("c1", "full", True, case_sha="abc"))])
        assert not isinstance(old, str) and not isinstance(new, str)
        self.assertEqual((old[0].case_sha, new[0].case_sha), (None, "abc"))
        for bad in ("", 5):
            line = json.dumps(ledger_row("c1", "full", True, case_sha=bad))
            self.assertIn("line 1", str(self.read([line])), bad)

    def test_a_rerun_of_another_case_version_does_not_replace_the_run(self) -> None:
        v1 = run("c1", "full", False)
        v2 = run("c1", "full", True, run_id="later", case_sha="v2")
        self.assertEqual(len(latest_runs([v1, v2], MODEL)), 2)

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
        self.assertIn("p=0.008 two-sided", report.verdict)
        self.assertNotIn("better without", report.verdict)
        self.assertEqual(len(report.helped), 8)

    def test_a_rule_that_costs_cases_is_harm(self) -> None:
        report = report_for(grid({c: (False, True, True) for c in self.cases(8)}))
        self.assertTrue(report.verdict.startswith("harm"), report.verdict)
        self.assertIn("better without the rule: c0, c1", report.verdict)
        self.assertEqual((report.broken, len(report.harmed)), ((), 8))

    def test_one_case_the_other_way_turns_six_wins_from_keep_to_watch(self) -> None:
        # the orchestration suite: six cases helped, and a survey case that passes only without
        # the rule. A one-sided gate dropped that case as broken in full and called it a keep.
        outcomes = {c: (True, False, False) for c in self.cases(6)}
        outcomes |= {"t0": (True, True, True), "t1": (True, True, True)}
        kept = report_for(grid(outcomes)).verdict
        self.assertTrue(kept.startswith("keep: removal breaks 6 case(s), p=0.031"), kept)
        outcomes["t1"] = (False, True, True)
        report = report_for(grid(outcomes))
        self.assertEqual((report.broken, report.harmed), ((), ("t1",)))
        self.assertTrue(
            report.verdict.startswith("watch: removal changed 7 case(s), 6 worse and 1 better"),
            report.verdict,
        )
        self.assertIn("p=0.125", report.verdict)

    def test_keep_names_the_cases_better_without_the_rule(self) -> None:
        outcomes = {c: (True, False, False) for c in self.cases(8)}
        outcomes["t0"] = (False, True, True)
        verdict = report_for(grid(outcomes)).verdict
        self.assertTrue(verdict.startswith("keep: removal breaks 8 case(s), p=0.039"), verdict)
        self.assertTrue(verdict.endswith("better without the rule: t0"), verdict)

    def test_removal_breaking_few_cases_is_watch_with_raw_counts(self) -> None:
        outcomes = {c: (True, True, True) for c in self.cases(3)}
        outcomes["c0"] = (True, False, False)
        report = report_for(grid(outcomes))
        self.assertTrue(report.verdict.startswith("watch"), report.verdict)
        self.assertEqual(report.helped, ("c0",))
        assert report.vs_minus is not None
        self.assertEqual((report.vs_minus.n_cases, report.vs_minus.helped), (3, 1))

    def test_a_case_only_better_without_the_rule_is_watch_not_null(self) -> None:
        outcomes = {c: (True, True, True) for c in self.cases(8)}
        outcomes["c0"] = (False, True, True)
        report = report_for(grid(outcomes))
        self.assertEqual((report.helped, report.harmed), ((), ("c0",)))
        self.assertTrue(
            report.verdict.startswith("watch: removal changed 1 case(s), 0 worse and 1 better"),
            report.verdict,
        )

    def test_five_wins_and_no_loss_is_still_watch_at_p_0_0625(self) -> None:
        # five cases one way is 2 of 32 sign patterns: a keep needs six
        outcomes = {c: (True, True, True) for c in self.cases(8)}
        for case in ("c0", "c1", "c2", "c3", "c4"):
            outcomes[case] = (True, False, False)
        report = report_for(grid(outcomes))
        self.assertTrue(report.verdict.startswith("watch"), report.verdict)
        self.assertIn("p=0.062", report.verdict)

    def test_null_with_a_clean_none_arm_is_non_discriminating(self) -> None:
        report = report_for(grid({c: (True, True, True) for c in self.cases(8)}))
        self.assertTrue(report.verdict.startswith("non-discriminating"), report.verdict)

    def test_null_but_none_arm_fails_once_is_watch_not_delete(self) -> None:
        outcomes = {c: (True, True, True) for c in self.cases(8)}
        outcomes["c3"] = (True, True, False)
        report = report_for(grid(outcomes))
        self.assertTrue(report.verdict.startswith("watch"), report.verdict)
        self.assertEqual(report.none_worse, ("c3",))
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

    def test_every_case_broken_in_every_arm_is_incomplete(self) -> None:
        report = report_for(grid({c: (False, False, False) for c in self.cases(3)}))
        self.assertIn("incomplete: no case to score", report.verdict)
        self.assertEqual(report.broken, ("c0", "c1", "c2"))

    def test_unfinished_runs_are_excluded_and_counted(self) -> None:
        runs = grid({c: (True, True, True) for c in self.cases(2)})
        runs.append(run("c0", "none", False, status="contaminated", run_id="x"))
        runs.append(run("c1", "none", False, status="harness_error", run_id="y"))
        report = report_for(runs)
        self.assertEqual(report.excluded_runs, 2)
        self.assertEqual(report.none_worse, ())

    def test_a_case_full_fails_only_some_of_the_time_is_scored_at_its_rate(self) -> None:
        runs = [run("c0", "full", True), run("c0", "full", False, repeat_index=1, run_id="r1")]
        runs += [run("c0", "minus-shell", True), run("c0", "minus-shell", True, repeat_index=1)]
        report = report_for(runs)
        self.assertEqual((report.broken, report.harmed), ((), ("c0",)))
        assert report.vs_minus is not None
        self.assertEqual(report.vs_minus.mean_diff, -0.5)

    def test_a_case_only_another_arm_passes_is_not_broken(self) -> None:
        report = report_for(grid({"c0": (False, False, True)}))
        self.assertEqual((report.broken, report.none_worse), ((), ()))
        assert report.vs_none is not None
        self.assertEqual(report.vs_none.harmed, 1)


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
            self.assertIn("versions=unchecked", lines[-1], "no cases.json beside the ledger")
            usage, code = run_report(["nope"], root, home)
            self.assertEqual((code, len(usage)), (2, 1))

    def test_cases_json_sets_older_case_versions_aside(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, home = Path(tmp) / "evals", Path(tmp) / "home"
            (root / "ledger").mkdir(parents=True)
            (root / "steering" / "shell").mkdir(parents=True)
            (home / "steering").mkdir(parents=True)
            shutil.copy(EVALS_ROOT / "limits.json", root / "limits.json")
            entry: dict[str, object] = {"id": "c0", "prompt": "list the files"}
            cases_path = root / "steering" / "shell" / "cases.json"
            cases_path.write_text(json.dumps([entry]), encoding="utf-8")
            now = case_sha(entry, root, 256)
            rows = [
                ledger_row("c0", "full", False, case_sha="older"),
                ledger_row("c0", "full", False, run_id="unversioned"),
                ledger_row("c0", "full", True, case_sha=now, run_id="r-now"),
                ledger_row("c0", "minus-shell", False, case_sha=now),
            ]
            (root / "ledger" / "steering_shell.jsonl").write_text(
                "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
            )
            args = ["steering/shell", "--home", str(home), "--model", MODEL]
            lines, code = run_report(args, root, home)
            self.assertEqual(code, 0, lines)
            self.assertIn("older case versions set aside: 2", lines)
            self.assertTrue(any(line.startswith("c0 ") and "1/1" in line for line in lines), lines)
            self.assertIn("versions=checked", lines[-1])
            cases_path.write_text("{nope", encoding="utf-8")
            broken, code = run_report(args, root, home)
            self.assertEqual(
                (code, broken[-1]), (1, "report: suite=steering/shell unreadable cases")
            )

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
        self.assertEqual(limits.min_cases_for_test, 8)
        advice = (limits.demote_words_max, limits.demote_cases_min, limits.prune_cases_min)
        self.assertEqual(advice, (350, 12, 16))
        self.assertGreater(limits.ledger_lines_max, 0)


class CaseVersionTest(unittest.TestCase):
    def test_case_sha_follows_the_case_and_its_fixture_not_key_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "fx").mkdir()
            (root / "fx" / "a.txt").write_text("one", encoding="utf-8")
            entry: dict[str, object] = {"id": "c0", "prompt": "p", "fixture": "fx"}
            first = case_sha(entry, root, 8)
            self.assertEqual(case_sha({"fixture": "fx", "prompt": "p", "id": "c0"}, root, 8), first)
            self.assertNotEqual(case_sha({**entry, "prompt": "q"}, root, 8), first)
            (root / "fx" / "a.txt").write_text("two", encoding="utf-8")
            self.assertNotEqual(case_sha(entry, root, 8), first, "a fixture edit is a new version")

    def test_current_versions_keeps_only_runs_of_each_case_as_it_is_now(self) -> None:
        runs = [
            run("c0", "full", True),
            run("c0", "full", True, case_sha="v0"),
            run("c0", "full", True, case_sha=None),
            run("gone", "full", True),  # a case no longer in cases.json
            run("gone", "full", True, case_sha=None),  # ... run before versioning
        ]
        kept, aside = current_versions(runs, {"c0": "v1"})
        self.assertEqual(([r.case_sha for r in kept], aside), (["v1"], 4))


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
        self.assertEqual(report.skill_worse, ())
        assert report.vs_skill is not None
        self.assertEqual(report.vs_skill.n_cases, 12)

    def test_one_changed_case_is_enough_to_ask(self) -> None:
        report = report_for(self.runs(12, minus_fails=1), words=351)
        self.assertEqual(report.helped, ("c0",))
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
        self.assertEqual(dropped.skill_worse, ("c3",))
        self.assertIsNone(report_for(no_skill_runs, words=351).vs_skill)

    def test_render_shows_the_skill_column_and_its_lines(self) -> None:
        lines = render_table(report_for(self.runs(12, skill_fails="c3"), words=351), LIMITS)
        self.assertTrue(lines[1].rstrip().endswith("skill-instead"), lines[1])
        self.assertTrue(any(line.startswith("c3 ") and line.endswith("0/1") for line in lines))
        self.assertTrue(any(line.startswith("full vs skill-instead: n=12") for line in lines))
        self.assertIn("worse as a skill: c3", lines)


if __name__ == "__main__":
    unittest.main()
