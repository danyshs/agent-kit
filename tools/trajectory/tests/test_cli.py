"""Stage 3: report and coverage commands, CLI argument validation, exit codes."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers import make_coverage, make_data
from traj.cli import (
    main,
    parse_coverage_args,
    parse_report_args,
    run_coverage,
    run_report,
)
from traj.store import write_coverage, write_session


class ParseReportArgsTest(unittest.TestCase):
    def test_neither_fleet_nor_session_is_error(self) -> None:
        self.assertIsInstance(parse_report_args([]), str)

    def test_both_fleet_and_session_is_error(self) -> None:
        self.assertIsInstance(
            parse_report_args(["--fleet", "--session", "aaaaaaaa-0000-4000-8000-123456789abc"]),
            str,
        )

    def test_fleet_ok(self) -> None:
        opts = parse_report_args(["--fleet"])
        self.assertIsInstance(opts, dict)
        self.assertIn("--fleet", opts)

    def test_session_ok(self) -> None:
        opts = parse_report_args(["--session", "aaaaaaaa-0000-4000-8000-123456789abc"])
        self.assertIsInstance(opts, dict)
        assert isinstance(opts, dict)
        self.assertEqual(opts["--session"], "aaaaaaaa-0000-4000-8000-123456789abc")

    def test_session_bad_uuid_is_error(self) -> None:
        self.assertIsInstance(parse_report_args(["--session", "not-a-uuid"]), str)

    def test_bad_since_date_is_error(self) -> None:
        self.assertIsInstance(parse_report_args(["--fleet", "--since", "not-a-date"]), str)
        self.assertIsInstance(parse_report_args(["--fleet", "--since", "2026-13-01"]), str)

    def test_valid_since_date(self) -> None:
        opts = parse_report_args(["--fleet", "--since", "2026-11-07"])
        self.assertIsInstance(opts, dict)

    def test_bad_workspace_is_error(self) -> None:
        # workspace must match NAME_RE; a space is invalid
        self.assertIsInstance(parse_report_args(["--fleet", "--workspace", "bad name"]), str)

    def test_valid_workspace(self) -> None:
        opts = parse_report_args(["--fleet", "--workspace", "myrepo"])
        self.assertIsInstance(opts, dict)

    def test_claude_harness_is_accepted(self) -> None:
        self.assertIsInstance(parse_report_args(["--fleet", "--harness", "claude"]), dict)
        self.assertIsInstance(parse_report_args(["--fleet", "--harness", "codex"]), str)

    def test_bad_detectors_is_error(self) -> None:
        self.assertIsInstance(parse_report_args(["--fleet", "--detectors", "some"]), str)

    def test_detectors_all(self) -> None:
        opts = parse_report_args(["--fleet", "--detectors", "all"])
        self.assertIsInstance(opts, dict)

    def test_unknown_flag_is_error(self) -> None:
        self.assertIsInstance(parse_report_args(["--fleet", "--unknown"]), str)

    def test_repeated_flag_is_error(self) -> None:
        self.assertIsInstance(parse_report_args(["--fleet", "--stamp", "a", "--stamp", "b"]), str)


class ParseCoverageArgsTest(unittest.TestCase):
    def test_no_args_ok(self) -> None:
        opts = parse_coverage_args([])
        self.assertIsInstance(opts, dict)

    def test_claude_harness_is_accepted(self) -> None:
        self.assertIsInstance(parse_coverage_args(["--harness", "claude"]), dict)
        self.assertIsInstance(parse_coverage_args(["--harness", "codex"]), str)

    def test_valid_harness(self) -> None:
        opts = parse_coverage_args(["--harness", "kiro"])
        self.assertIsInstance(opts, dict)


class ReportCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self._events = Path(self._tmp) / "events"
        self._state = self._events.parent

    def _write_session_and_coverage(self) -> str:
        data = make_data()
        write_session(self._events, data)
        cov = make_coverage()
        write_coverage(self._state, cov)
        return data.session.session_id

    def test_fleet_exit_0(self) -> None:
        self._write_session_and_coverage()
        _, code = run_report({"--fleet": "1", "--events-dir": str(self._events)})
        self.assertEqual(code, 0)

    def test_fleet_has_status_line(self) -> None:
        self._write_session_and_coverage()
        lines, _ = run_report({"--fleet": "1", "--events-dir": str(self._events)})
        self.assertTrue(lines[-1].startswith("traj:"))

    def test_session_found(self) -> None:
        sid = self._write_session_and_coverage()
        lines, code = run_report({"--session": sid, "--events-dir": str(self._events)})
        self.assertEqual(code, 0)
        text = "\n".join(lines)
        self.assertIn(sid, text)

    def test_session_not_found_exit_1(self) -> None:
        self._write_session_and_coverage()
        _, code = run_report(
            {
                "--session": "ffffffff-ffff-4fff-8fff-ffffffffffff",
                "--events-dir": str(self._events),
            }
        )
        self.assertEqual(code, 1)

    def test_out_flag_writes_file_and_prints_status_only(self) -> None:
        self._write_session_and_coverage()
        out = Path(self._tmp) / "fleet.md"
        lines, code = run_report(
            {"--fleet": "1", "--events-dir": str(self._events), "--out": str(out)}
        )
        self.assertEqual(code, 0)
        # only the status line to stdout
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("traj:"))
        # the full report is in the file
        content = out.read_text()
        self.assertIn("depth-0 credits as logged", content)

    def test_findings_never_change_exit_code(self) -> None:
        """Findings (stage 4) must never make report exit 1; stage 3 is always 0."""
        self._write_session_and_coverage()
        _, code = run_report({"--fleet": "1", "--events-dir": str(self._events)})
        self.assertEqual(code, 0)

    def test_no_coverage_json_still_runs(self) -> None:
        """report must run even when build has never run (no coverage.json)."""
        data = make_data()
        write_session(self._events, data)
        # deliberately no write_coverage
        _, code = run_report({"--fleet": "1", "--events-dir": str(self._events)})
        self.assertEqual(code, 0)


class CoverageCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self._events = Path(self._tmp) / "events"
        self._state = self._events.parent

    def test_coverage_exit_0(self) -> None:
        cov = make_coverage()
        write_coverage(self._state, cov)
        _, code = run_coverage({"--events-dir": str(self._events)})
        self.assertEqual(code, 0)

    def test_coverage_missing_json_exit_1(self) -> None:
        _, code = run_coverage({"--events-dir": str(self._events)})
        self.assertEqual(code, 1)

    def test_coverage_status_line_last(self) -> None:
        cov = make_coverage()
        write_coverage(self._state, cov)
        lines, _ = run_coverage({"--events-dir": str(self._events)})
        self.assertTrue(lines[-1].startswith("traj:"))


class MainCommandDispatchTest(unittest.TestCase):
    def test_no_command_exit_2(self) -> None:
        self.assertEqual(main([]), 2)

    def test_unknown_command_exit_2(self) -> None:
        self.assertEqual(main(["doctor"]), 2)

    def test_report_no_fleet_or_session_exit_2(self) -> None:
        self.assertEqual(main(["report"]), 2)

    def test_report_both_fleet_and_session_exit_2(self) -> None:
        self.assertEqual(
            main(
                [
                    "report",
                    "--fleet",
                    "--session",
                    "aaaaaaaa-0000-4000-8000-123456789abc",
                ]
            ),
            2,
        )

    def test_coverage_exit_1_no_build(self) -> None:
        # running coverage with a non-existent events dir returns exit 1
        self.assertEqual(main(["coverage", "--events-dir", "/nonexistent/path"]), 1)
