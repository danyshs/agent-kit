"""Stage 3: nearest_rank, sum_known, render_fleet, render_session, render_coverage."""

from __future__ import annotations

import unittest
from dataclasses import replace

from tests.helpers import CANARY, make_coverage, make_data, make_session_line, make_step, make_turn
from traj.report import (
    nearest_rank,
    render_coverage,
    render_fleet,
    render_session,
    status_line,
    sum_known,
)
from traj.schema import SessionData


class NearestRankTest(unittest.TestCase):
    def test_empty_returns_none(self) -> None:
        self.assertIsNone(nearest_rank([], 50.0))

    def test_single_value(self) -> None:
        self.assertEqual(nearest_rank([7.0], 50.0), 7.0)
        self.assertEqual(nearest_rank([7.0], 100.0), 7.0)

    def test_two_values_p50(self) -> None:
        # ceil(50/100 * 2) - 1 = ceil(1.0) - 1 = 0
        self.assertEqual(nearest_rank([3.0, 9.0], 50.0), 3.0)

    def test_two_values_p100(self) -> None:
        # ceil(100/100 * 2) - 1 = 2 - 1 = 1
        self.assertEqual(nearest_rank([3.0, 9.0], 100.0), 9.0)

    def test_ten_values_p50(self) -> None:
        # ceil(50/100 * 10) - 1 = 5 - 1 = 4 -> 5th element (0-indexed 4) of sorted
        vals = [float(x) for x in range(1, 11)]  # 1..10
        self.assertEqual(nearest_rank(vals, 50.0), 5.0)

    def test_ten_values_p90(self) -> None:
        # ceil(90/100 * 10) - 1 = 9 - 1 = 8 -> 0-indexed 8 = value 9
        vals = [float(x) for x in range(1, 11)]
        self.assertEqual(nearest_rank(vals, 90.0), 9.0)

    def test_ten_values_max(self) -> None:
        vals = [float(x) for x in range(1, 11)]
        self.assertEqual(nearest_rank(vals, 100.0), 10.0)

    def test_unsorted_input_still_sorts(self) -> None:
        self.assertEqual(nearest_rank([9.0, 1.0, 5.0], 100.0), 9.0)


class SumKnownTest(unittest.TestCase):
    def test_all_known(self) -> None:
        total, nulls = sum_known([1.0, 2.0, 3.0])
        self.assertAlmostEqual(total, 6.0)
        self.assertEqual(nulls, 0)

    def test_all_null(self) -> None:
        total, nulls = sum_known([None, None])
        self.assertAlmostEqual(total, 0.0)
        self.assertEqual(nulls, 2)

    def test_mixed(self) -> None:
        total, nulls = sum_known([1.0, None, 3.0, None])
        self.assertAlmostEqual(total, 4.0)
        self.assertEqual(nulls, 2)

    def test_empty(self) -> None:
        total, nulls = sum_known([])
        self.assertAlmostEqual(total, 0.0)
        self.assertEqual(nulls, 0)


class RenderCoverageTest(unittest.TestCase):
    def test_rows_sum_to_seen(self) -> None:
        cov = make_coverage(seen=5, scanned_from_raw=3, not_built_yet=2)
        lines = render_coverage(cov)
        seen_row = next(line for line in lines if line.startswith("| seen |"))
        self.assertIn("| 5 |", seen_row)

    def test_every_skip_reason_has_row(self) -> None:
        from traj.limits import SKIP_REASONS

        cov = make_coverage()
        lines = render_coverage(cov)
        text = "\n".join(lines)
        for reason in SKIP_REASONS:
            self.assertIn(reason, text)

    def test_status_line_format(self) -> None:
        cov = make_coverage()
        line = status_line(cov)
        self.assertTrue(line.startswith("traj: sessions="))
        self.assertIn("built=", line)
        self.assertIn("skipped=", line)
        self.assertIn("malformed=", line)
        self.assertIn("unknown=", line)


class RenderFleetTest(unittest.TestCase):
    def _two_session_fleet(self) -> list[SessionData]:
        """Two sessions: one with credits, one with null credits."""
        d1 = make_data()
        # turn 0 has credits=0.25; replace to give it a bigger value
        t0 = replace(d1.turns[0], credits=10.0)
        s1 = replace(d1.session, session_id="aaaaaaaa-0000-4000-8000-000000000001")
        d1 = replace(d1, session=s1, turns=(t0, d1.turns[1]))

        t0b = make_turn(0, 0, 1, 0, None)
        t0b = replace(t0b, credits=None)  # no usage
        s2 = make_session_line(1, 1)
        s2 = replace(s2, session_id="bbbbbbbb-0000-4000-8000-000000000002")
        step = make_step(0, 0, "read", "ok")
        d2 = SessionData(s2, (t0b,), (step,))
        return [d1, d2]

    def test_fleet_has_credits_col_header(self) -> None:
        datas = self._two_session_fleet()
        cov = make_coverage(seen=2, scanned_from_raw=2)
        lines = render_fleet(datas, cov, "2026-11-14 10:00 +0000", {})
        text = "\n".join(lines)
        self.assertIn("depth-0 credits as logged", text)

    def test_fleet_na_turn_shows_na(self) -> None:
        """A turn with no credits appears in the n/a count."""
        datas = self._two_session_fleet()
        cov = make_coverage(seen=2, scanned_from_raw=2)
        lines = render_fleet(datas, cov, "stamp", {})
        text = "\n".join(lines)
        # the n/a row in the cost table should show 1
        self.assertIn("n/a (no usage) | 1", text)

    def test_fleet_no_cost_per_outcome(self) -> None:
        datas = self._two_session_fleet()
        cov = make_coverage(seen=2, scanned_from_raw=2)
        lines = render_fleet(datas, cov, "stamp", {})
        text = "\n".join(lines)
        self.assertNotIn("cost per outcome", text.lower())

    def test_fleet_status_line_is_last(self) -> None:
        datas = self._two_session_fleet()
        cov = make_coverage(seen=2, scanned_from_raw=2)
        lines = render_fleet(datas, cov, "stamp", {})
        self.assertTrue(lines[-1].startswith("traj:"))

    def test_fleet_data_quality_banner(self) -> None:
        """When coverage has data-quality problems the fleet report shows the banner."""
        # force unknown events over the threshold by injecting large counts
        # quality threshold is 1% of events_total
        cov = make_coverage(
            seen=1,
            scanned_from_raw=1,
            unknown_events=100,
            events_used=50,
            events_total=200,
        )
        # coverage must be valid first; adjust to make it pass coverage_problem but
        # trigger check_quality via high unknown%
        from traj.report import coverage_problem

        self.assertIsNone(coverage_problem(cov))
        lines = render_fleet([], cov, "stamp", {})
        text = "\n".join(lines)
        self.assertIn("DATA QUALITY", text)

    def test_fleet_canary_absent_from_output(self) -> None:
        """The canary string must never appear in any fleet report output."""
        datas = self._two_session_fleet()
        cov = make_coverage(seen=2, scanned_from_raw=2)
        lines = render_fleet(datas, cov, "stamp", {})
        text = "\n".join(lines)
        self.assertNotIn(CANARY, text)

    def test_fleet_filters_shown_in_header(self) -> None:
        cov = make_coverage()
        lines = render_fleet([], cov, "stamp", {"since": "2026-11-07"})
        text = "\n".join(lines)
        self.assertIn("filters:", text)
        self.assertIn("since", text)

    def test_top_turns_credits_col_header(self) -> None:
        """Top turns table header must say 'depth-0 credits as logged'."""
        d1 = make_data()
        t0 = replace(d1.turns[0], credits=42.0)
        d1 = replace(d1, turns=(t0, d1.turns[1]))
        cov = make_coverage()
        lines = render_fleet([d1], cov, "stamp", {})
        text = "\n".join(lines)
        self.assertIn("depth-0 credits as logged", text)


class RenderSessionTest(unittest.TestCase):
    def test_session_has_credits_col(self) -> None:
        data = make_data()
        lines = render_session(data, [], "stamp")
        text = "\n".join(lines)
        self.assertIn("depth-0 credits as logged", text)

    def test_session_na_turn(self) -> None:
        """Depth-1 turn has no credits; must show n/a."""
        data = make_data()
        lines = render_session(data, [], "stamp")
        text = "\n".join(lines)
        self.assertIn("n/a", text)

    def test_session_findings_none_when_empty(self) -> None:
        data = make_data()
        lines = render_session(data, [], "stamp")
        text = "\n".join(lines)
        self.assertIn("*(none)*", text)

    def test_session_canary_absent(self) -> None:
        data = make_data()
        lines = render_session(data, [], "stamp")
        self.assertNotIn(CANARY, "\n".join(lines))
