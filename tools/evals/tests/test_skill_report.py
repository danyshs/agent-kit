"""Goal: `eval spend` stops a paid loop and `eval report skill/<name>` prints raw counts, never
the word delete (design 3.8, 6, 7.2 step 4). Method: hand-built ledger lines where the answer is
known; each test has a planted twin that must give a different answer."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lib.cli import EVALS_ROOT
from lib.report import ReportLimits, run_spend, total_credits
from lib.run import CLAUDE_DEFAULT_MODEL
from lib.skill_report import (
    SkillRun,
    activation_counts,
    activation_verdict,
    component_of,
    latest,
    read_skill_ledger,
    review_totals,
    run_skill_report,
    verdict_for,
    write_totals,
)

LIMITS = ReportLimits(
    resamples=300,
    min_cases_for_ci=8,
    ledger_lines_max=1000,
    demote_words_max=350,
    demote_cases_min=12,
    prune_cases_min=16,
)
MODEL = CLAUDE_DEFAULT_MODEL


def row(case: str, arm: str, passed: bool = True, **over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "run_id": f"{case}--{arm}--r0--1",
        "case_id": case,
        "arm": arm,
        "repeat_index": 0,
        "model_id": MODEL,
        "status": "ok",
        "case_passed": passed,
        "credits": 0.05,
        "skills_read": [],
        "grades": [],
    }
    base.update(over)
    return base


def grade(ident: str, verdict: str, evidence: str) -> dict[str, str]:
    return {"assertion_id": ident, "verdict": verdict, "evidence": evidence}


def write_ledger(root: Path, name: str, rows: list[dict[str, object]]) -> None:
    (root / "ledger").mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(r) for r in rows) + "\n"
    (root / "ledger" / name).write_text(text, encoding="utf-8")


def read(rows: list[dict[str, object]]) -> list[SkillRun]:
    with tempfile.TemporaryDirectory() as tmp:
        write_ledger(Path(tmp), "x.jsonl", rows)
        result = read_skill_ledger(Path(tmp) / "ledger" / "x.jsonl", 100)
    assert not isinstance(result, str), result
    return result


class SpendTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "limits.json").write_text(
            (EVALS_ROOT / "limits.json").read_text(encoding="utf-8"), encoding="utf-8"
        )
        # a rerun of one cell: the guard must count both lines
        write_ledger(
            self.root,
            "skill_demo.jsonl",
            [row("a", "with", credits=0.30), row("a", "with", credits=0.20), row("b", "with")],
        )
        self.addCleanup(self.tmp.cleanup)

    def spend(self, ceiling: str, suite: str = "skill/demo") -> tuple[list[str], int]:
        return run_spend([suite, "--model", MODEL, "--max", ceiling], self.root)

    def test_total_counts_every_line_including_reruns(self) -> None:
        total = total_credits(self.root / "ledger" / "skill_demo.jsonl", MODEL, 100)
        self.assertAlmostEqual(float(total), 0.55)

    def test_over_trips_and_under_does_not(self) -> None:
        lines, code = self.spend("0.5")
        self.assertEqual(code, 1)
        self.assertIn("over=true", lines[0])
        lines, code = self.spend("0.6")
        self.assertEqual(code, 0)
        self.assertIn("over=false", lines[0])

    def test_exactly_at_the_ceiling_is_not_over(self) -> None:
        self.assertEqual(self.spend("0.55")[1], 0, "planted: >= instead of > would trip here")

    def test_unreadable_inputs_fail_closed(self) -> None:
        self.assertEqual(self.spend("1", "skill/missing")[1], 1)
        self.assertEqual(self.spend("abc")[1], 1)
        self.assertEqual(self.spend("-1")[1], 1)
        self.assertEqual(self.spend("nan")[1], 1)
        self.assertEqual(run_spend(["skill/demo"], self.root)[1], 2)
        (self.root / "ledger" / "skill_bad.jsonl").write_text("{not json\n", encoding="utf-8")
        self.assertEqual(self.spend("1", "skill/bad")[1], 1)


class ComponentTest(unittest.TestCase):
    def test_every_prefix_maps_and_unknown_asserts(self) -> None:
        expected = {
            "trig-pos-dev-01": "trig-pos-dev",
            "trig-neg-held-08": "trig-neg-held",
            "rev-py-planted": "rev-fire",
            "rev-py-clean": "rev-clean",
            "wr-py-02": "wr-py",
            "wr-fish-01": "wr-other",
        }
        for case, component in expected.items():
            self.assertEqual(component_of(case), component)
        with self.assertRaises(AssertionError):
            component_of("zzz-1")


class CountsTest(unittest.TestCase):
    def test_activation_counts_loaded_over_finished(self) -> None:
        runs = read(
            [
                row("trig-pos-dev-01", "with", skills_read=["tiger-style"]),
                row("trig-pos-dev-02", "with"),
                row("trig-neg-dev-01", "with", skills_read=["tiger-style"]),
                row("trig-neg-dev-02", "with", status="timeout"),
            ]
        )
        counts = activation_counts(runs, "with")
        self.assertEqual(counts["trig-pos-dev"], (1, 2))
        self.assertEqual(counts["trig-neg-dev"], (1, 1), "a timeout is not a finished run")
        self.assertEqual(counts["trig-pos-held"], (0, 0))

    def test_activation_verdict_flips_with_the_split(self) -> None:
        separated = {"trig-pos-dev": (4, 4), "trig-pos-held": (3, 4), "trig-neg-dev": (0, 4)}
        separated["trig-neg-held"] = (0, 4)
        self.assertEqual(activation_verdict(separated), "watch")
        flat = {"trig-pos-dev": (0, 4), "trig-pos-held": (0, 4), "trig-neg-dev": (0, 4)}
        flat["trig-neg-held"] = (0, 4)
        self.assertEqual(activation_verdict(flat), "non-discriminating")

    def test_review_totals_parse_evidence_counts(self) -> None:
        good = row(
            "rev-py-planted",
            "with",
            grades=[
                grade("recall", "PASS", "recall 0.60 (min 0.6); in-category 3/3, outside 0/2"),
                grade("precision", "PASS", "precision 0.60 (min 0.6); 3/5"),
                grade("severity", "PASS", "severity 1.00 (min 1.0); 3/3"),
            ],
        )
        clean = row(
            "rev-py-clean", "with", grades=[grade("clean_quiet", "FAIL", "2 P0/P1 findings of 4")]
        )
        unparsed = row(
            "rev-fish-planted", "with", grades=[grade("recall", "FAIL", "no parse: FINDINGS_DONE")]
        )
        totals = review_totals(read([good, clean, unparsed]), "with")
        self.assertEqual((totals.in_found, totals.in_total), (3, 3))
        self.assertEqual((totals.out_found, totals.out_total), (0, 2))
        self.assertEqual((totals.prec_matched, totals.prec_findings), (3, 5))
        self.assertEqual((totals.sev_ok, totals.sev_matched), (3, 3))
        self.assertEqual((totals.loud_clean, totals.parse_failures), (2, 1))
        self.assertEqual(review_totals(read([good]), "without").in_total, 0, "other arm is empty")

    def test_write_totals_per_grader(self) -> None:
        grades = [grade("length", "PASS", "ok"), grade("loops", "FAIL", "while")]
        totals = write_totals(read([row("wr-py-01", "with", grades=grades)]), "with")
        self.assertEqual(
            (totals["length"], totals["loops"], totals["units"]), ((1, 1), (0, 1), (0, 1))
        )

    def test_later_ledger_line_replaces_an_earlier_one(self) -> None:
        runs = latest(read([row("a", "with", False), row("a", "with", True)]))
        self.assertEqual([r.passed for r in runs], [True])


class VerdictTest(unittest.TestCase):
    def flags(self, values: list[float]) -> dict[str, float]:
        return {f"c{i}": v for i, v in enumerate(values)}

    def test_keep_needs_eight_cases_and_an_interval_above_zero(self) -> None:
        eight = self.flags([1.0] * 8)
        zeros = self.flags([0.0] * 8)
        self.assertEqual(verdict_for(eight, zeros, LIMITS), "keep")
        seven = self.flags([1.0] * 7)
        self.assertEqual(verdict_for(seven, self.flags([0.0] * 7), LIMITS), "watch", "n < 8")

    def test_without_arm_never_failing_is_non_discriminating(self) -> None:
        ones = self.flags([1.0] * 8)
        self.assertEqual(verdict_for(ones, ones, LIMITS), "non-discriminating")

    def test_no_pairs_is_watch_and_no_verdict_says_delete(self) -> None:
        self.assertEqual(verdict_for({}, {}, LIMITS), "watch")
        self.assertEqual(verdict_for({"a": 0.0}, {"a": 1.0}, LIMITS), "non-discriminating")


class ReportCliTest(unittest.TestCase):
    def test_end_to_end_and_error_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "limits.json").write_text(
                (EVALS_ROOT / "limits.json").read_text(encoding="utf-8"), encoding="utf-8"
            )
            missing, code = run_skill_report(["skill/tiger-style"], root)
            self.assertEqual(code, 1)
            self.assertIn("no ledger", missing[-1])
            write_ledger(
                root,
                "skill_tiger-style.jsonl",
                [
                    row("trig-pos-dev-01", "with", skills_read=["tiger-style"], credits=0.25),
                    row("trig-pos-dev-01", "with", skills_read=["tiger-style"]),
                    row("rev-py-clean", "with"),
                ],
            )
            lines, code = run_skill_report(["skill/tiger-style"], root)
            self.assertEqual(code, 0)
            text = "\n".join(lines)
            self.assertIn("trig-pos-dev=1/1", text, "the rerun replaced the first line")
            self.assertIn("credits_latest_runs=0.1000 credits_all_lines=0.3500", text)
            self.assertTrue(lines[-1].startswith("report: suite=skill/tiger-style"), lines[-1])
            self.assertNotIn("delete", "\n".join(lines).lower())
            self.assertEqual(run_skill_report(["steering/x"], root)[1], 2)
            self.assertEqual(run_skill_report(["skill/x", "--bad", "1"], root)[1], 2)


if __name__ == "__main__":
    unittest.main()
