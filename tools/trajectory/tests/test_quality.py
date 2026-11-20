"""The quality gate trips on a planted bad count and stays quiet one below."""

from __future__ import annotations

import unittest
from dataclasses import replace

from tests.helpers import make_coverage
from traj.limits import SKIP_REASONS
from traj.quality import check_quality
from traj.report import coverage_problem


class QualityTest(unittest.TestCase):
    def test_clean_coverage_has_no_reasons(self) -> None:
        self.assertEqual(check_quality(make_coverage()), [])

    def test_malformed_threshold_edge(self) -> None:
        base = make_coverage(events_used=1000, events_total=2000)
        self.assertEqual(check_quality(replace(base, malformed_events=10)), [])  # exactly 1 percent
        reasons = check_quality(replace(base, malformed_events=11))
        self.assertEqual(len(reasons), 1)
        self.assertIn("malformed", reasons[0])

    def test_unknown_threshold_edge(self) -> None:
        base = make_coverage(events_used=1000, events_total=2000)
        self.assertEqual(check_quality(replace(base, unknown_events=20)), [])
        self.assertEqual(len(check_quality(replace(base, unknown_events=21))), 1)

    def test_any_unknown_format_session_trips(self) -> None:
        base = make_coverage()
        skipped = tuple((r, 1 if r == "unknown format" else 0) for r in SKIP_REASONS)
        cov = replace(base, seen=1, scanned_from_raw=0, skipped=skipped)
        self.assertIsNone(coverage_problem(cov))
        reasons = check_quality(cov)
        self.assertEqual(len(reasons), 1)
        self.assertIn("unknown format", reasons[0])

    def test_other_skips_and_findings_are_not_quality_errors(self) -> None:
        base = make_coverage()
        skipped = tuple((r, 2 if r != "unknown format" else 0) for r in SKIP_REASONS)
        cov = replace(base, seen=2 * (len(SKIP_REASONS) - 1) + 1, skipped=skipped)
        self.assertIsNone(coverage_problem(cov))
        self.assertEqual(check_quality(cov), [])

    def test_unsound_coverage_is_refused(self) -> None:
        with self.assertRaises(AssertionError):
            check_quality(replace(make_coverage(), seen=99))


if __name__ == "__main__":
    unittest.main()
