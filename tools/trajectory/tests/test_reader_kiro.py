"""Kiro reader: discovery, turn and step rules, status table, canary."""

from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest import mock

from tests.helpers import kiro_call, kiro_event, kiro_result, write_kiro_session
from traj.limits import FILE_BYTES_MAX, LINES_PER_FILE_MAX
from traj.reader_kiro import find_kiro_sessions, read_kiro_session
from traj.schema import SessionData, Skip, check_session, encode_session

FIXTURES = Path(__file__).parent / "fixtures" / "kiro"
SID = "99999999-9999-4999-8999-999999999999"
U = "10000000-0000-4000-8000-000000000"
BASIC = "11111111-1111-4111-8111-111111111111"
SUBEXEC = "22222222-2222-4222-8222-222222222222"
MALFORMED = "66666666-6666-4666-8666-666666666666"
NEITHER = "88888888-8888-4888-8888-888888888888"


def read_dir(root: Path, session_id: str = SID) -> SessionData:
    refs = find_kiro_sessions(root, 100)
    assert isinstance(refs, list)
    ref = next(r for r in refs if r.session_id == session_id)
    assert ref.skip_reason == "", ref.skip_reason
    data = read_kiro_session(ref, LINES_PER_FILE_MAX, FILE_BYTES_MAX)
    assert isinstance(data, SessionData), data
    assert check_session(data) is None, check_session(data)
    return data


def one(
    events: Sequence[dict[str, object] | str],
    meta: dict[str, object] | None = None,
    subs: dict[str, Sequence[dict[str, object]]] | None = None,
) -> SessionData:
    with tempfile.TemporaryDirectory() as tmp:
        write_kiro_session(Path(tmp), SID, events, meta, subs)
        return read_dir(Path(tmp))


def status_of(call: dict[str, object], result: dict[str, object] | None) -> str:
    events = [kiro_event("turn_start", 0, executionId="e1"), call]
    if result is not None:
        events.append(result)
    return one(events).steps[0].status


def without_mtime(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        raw = json.loads(line)
        raw.pop("source_mtime_ms", None)
        out.append(json.dumps(raw, sort_keys=True))
    return out


class FixtureTest(unittest.TestCase):
    def fixture(self, case: str, session_id: str) -> SessionData:
        refs = find_kiro_sessions(FIXTURES / case, 100)
        assert isinstance(refs, list)
        ref = next(r for r in refs if r.session_id == session_id)
        data = read_kiro_session(ref, LINES_PER_FILE_MAX, FILE_BYTES_MAX)
        assert isinstance(data, SessionData), data
        return data

    def check_expected(self, case: str, session_id: str) -> None:
        expected = (FIXTURES / case / "expected" / f"{session_id}.jsonl").read_text().splitlines()
        got = encode_session(self.fixture(case, session_id))
        self.assertEqual(without_mtime(got), without_mtime(expected))

    def test_basic_matches_the_hand_checked_output(self) -> None:
        self.check_expected("basic", BASIC)

    def test_subexec_matches_the_hand_checked_output(self) -> None:
        self.check_expected("subexec", SUBEXEC)

    def test_the_canary_appears_in_no_output_line(self) -> None:
        for case, sid in (
            ("basic", BASIC),
            ("subexec", SUBEXEC),
            ("malformed", MALFORMED),
            ("user_neither", NEITHER),
        ):
            text = "\n".join(encode_session(self.fixture(case, sid)))
            self.assertNotIn("CANARY", text, case)
            self.assertNotIn("DEADBEEF", text, case)

    def test_subexec_rules(self) -> None:
        data = self.fixture("subexec", SUBEXEC)
        depth0 = [t for t in data.turns if t.depth == 0]
        depth1 = [t for t in data.turns if t.depth == 1]
        self.assertEqual((len(depth0), len(depth1)), (2, 2))
        self.assertEqual([t.steps for t in depth0], [1, 1])  # sub calls never join the parent
        self.assertEqual([t.steps for t in depth1], [2, 2])
        # sub files by first event time: 4444 (10:00:02, unknown parent) before 3333 (10:00:03)
        self.assertEqual([t.parent_turn_index for t in depth1], [None, 0])
        self.assertIsNone(depth0[1].stop_reason)  # a call-only execution is still a turn
        self.assertEqual(depth0[0].credits, 2.0)
        self.assertIsNone(depth0[0].model_calls)  # no requestIds: null, not malformed
        self.assertIsNone(depth0[1].credits)  # empty promptTurnSummaries
        self.assertEqual(depth0[1].model_calls, 1)
        self.assertTrue(all(t.credits is None for t in depth1))
        self.assertEqual(depth0[0].permission_wait_ms, 700)  # only the first consent round
        self.assertEqual(data.session.malformed_events, 1)  # one sub event names another file
        self.assertEqual(data.session.tree, "workflow")

    def test_user_events_with_neither_key_never_bound_a_turn(self) -> None:
        data = self.fixture("user_neither", NEITHER)
        self.assertEqual([t.depth for t in data.turns], [0, 0])
        self.assertEqual(data.session.malformed_events, 0)
        self.assertEqual(data.session.unknown_events, 0)
        self.assertEqual((data.session.events_total, data.session.events_used), (11, 6))

    def test_malformed_unknown_and_junk_lines(self) -> None:
        data = self.fixture("malformed", MALFORMED)
        self.assertEqual(data.session.malformed_events, 3)  # no id, empty id, result no success
        self.assertEqual(data.session.unknown_events, 1)
        self.assertEqual(data.session.parse_errors, 1)
        self.assertEqual([s.status for s in data.steps], ["unpaired"])
        self.assertEqual(data.session.unpaired_calls, 1)

    def test_discovery_reasons_on_the_fixtures(self) -> None:
        for case, sid, reason in (
            ("bad_version", "55555555-5555-4555-8555-555555555555", "unknown format"),
            ("no_session_json", "77777777-7777-4777-8777-777777777777", "no session.json"),
            ("basic", BASIC, ""),
        ):
            refs = find_kiro_sessions(FIXTURES / case, 100)
            assert isinstance(refs, list)
            self.assertEqual({r.session_id: r.skip_reason for r in refs}, {sid: reason}, case)


class StatusTableTest(unittest.TestCase):
    def test_every_row(self) -> None:
        call = kiro_call("c1", "read_file", 10, path="/a")
        shell = kiro_call("c1", "execute_bash", 10, command="x")
        denied = kiro_call("c1", "read_file", 10, "denied", path="/a")
        failed = kiro_call("c1", "read_file", 10, "failed")
        rows = [
            ("denied, no result", denied, None, "denied"),
            ("denied, result present", denied, kiro_result("c1", 20, success=False), "denied"),
            ("cancelled", kiro_call("c1", "x", 10, "cancelled"), None, "cancelled"),
            ("no result", call, None, "unpaired"),
            ("failed, no result", failed, None, "unpaired"),
            ("success false", call, kiro_result("c1", 20, success=False), "error"),
            ("failed call", failed, kiro_result("c1", 20), "error"),
            ("shell exit 1", shell, kiro_result("c1", 20, content="x\nExit Code: 1"), "error"),
            ("shell exit 0", shell, kiro_result("c1", 20, content="x\nExit Code: 0"), "ok"),
            ("shell no exit code", shell, kiro_result("c1", 20, content="x"), "ok"),
            ("non-shell exit text", call, kiro_result("c1", 20, content="Exit Code: 1"), "ok"),
            ("approved", call, kiro_result("c1", 20), "ok"),
            ("completed", kiro_call("c1", "x", 10, "completed"), kiro_result("c1", 20), "ok"),
        ]
        for label, step_call, result, want in rows:
            self.assertEqual(status_of(step_call, result), want, label)

    def test_exit_code_is_kept_for_shell_steps_only(self) -> None:
        data = one(
            [
                kiro_call("c1", "execute_bash", 10, command="x"),
                kiro_result("c1", 20, content="Exit Code: 3"),
                kiro_call("c2", "read_file", 30, path="/a"),
                kiro_result("c2", 40, content="Exit Code: 3"),
            ]
        )
        self.assertEqual([s.exit_code for s in data.steps], [3, None])

    def test_duplicate_call_ids_count_once_and_results_pair_by_id(self) -> None:
        data = one(
            [
                kiro_call("c1", "read_file", 10, path="/a"),
                kiro_call("c1", "read_file", 11, path="/a"),
                kiro_result("zz", 12),  # no call: ignored
                kiro_result("c1", 20),
                kiro_result("c1", 30, success=False),  # second result: ignored
            ]
        )
        self.assertEqual([s.status for s in data.steps], ["ok"])
        self.assertEqual(data.session.malformed_events, 0)

    def test_a_denied_call_with_no_result_counts_as_unpaired_call(self) -> None:
        data = one(
            [
                kiro_call("c1", "read_file", 10, "denied", path="/a"),
                kiro_call("c2", "read_file", 20, path="/b"),
            ]
        )
        self.assertEqual(data.session.unpaired_calls, 2)
        self.assertEqual([s.status for s in data.steps], ["denied", "unpaired"])

    def test_tool_names_classes_and_null_shape(self) -> None:
        data = one(
            [
                kiro_call("c1", "execute_bash", 10, command="x"),
                kiro_call("c2", "mcp__thing", 20),
                kiro_call("c3", "bad name!", 30),
                kiro_call("c4", "str_replace", 40, path="/a"),
            ]
        )
        self.assertEqual(
            [(s.tool, s.tool_class) for s in data.steps],
            [
                ("execute_bash", "shell"),
                ("mcp__thing", "other"),
                ("(other)", "other"),
                ("str_replace", "write"),
            ],
        )
        self.assertIsNone(data.steps[0].shape)  # shapes arrive in stage 5


class TimingTest(unittest.TestCase):
    def test_permission_wait_pairs_first_pending_with_first_later_resolved(self) -> None:
        data = one(
            [
                kiro_event("pending_interaction", 100, toolCallId="c1"),
                kiro_event("pending_interaction", 150, toolCallId="c1"),
                kiro_event("interaction_resolved", 300, toolCallId="c1"),
                kiro_event("interaction_resolved", 900, toolCallId="c1"),
                kiro_call("c1", "read_file", 310, path="/a"),
                kiro_result("c1", 320),
                kiro_event("pending_interaction", 400, toolCallId="c2"),  # never resolved
                kiro_call("c2", "read_file", 410, path="/b"),
                kiro_result("c2", 420),
                kiro_event("interaction_resolved", 500, toolCallId="c3"),  # no pending
                kiro_call("c3", "read_file", 510, path="/c"),
                kiro_result("c3", 520),
            ]
        )
        self.assertEqual([s.permission_wait_ms for s in data.steps], [200, None, None])
        self.assertEqual(data.turns[0].permission_wait_ms, 200)

    def test_a_resolved_before_its_pending_is_not_a_pair(self) -> None:
        data = one(
            [
                kiro_event("interaction_resolved", 100, toolCallId="c1"),
                kiro_event("pending_interaction", 200, toolCallId="c1"),
                kiro_call("c1", "read_file", 300, path="/a"),
                kiro_result("c1", 310),
            ]
        )
        self.assertIsNone(data.steps[0].permission_wait_ms)

    def test_duration_subtracts_a_wait_that_began_after_the_call_event(self) -> None:
        data = one(
            [
                kiro_call("c1", "execute_bash", 1_000, command="x"),
                kiro_event("pending_interaction", 1_100, toolCallId="c1"),
                kiro_event("interaction_resolved", 1_600, toolCallId="c1"),
                kiro_result("c1", 2_000),
            ]
        )
        self.assertEqual(data.steps[0].permission_wait_ms, 500)
        self.assertEqual(data.steps[0].duration_ms, 500)

    def test_a_wait_before_the_call_event_is_not_subtracted(self) -> None:
        # live Kiro logs write the call event at result time, after the prompt
        data = one(
            [
                kiro_event("pending_interaction", 100, toolCallId="c1"),
                kiro_event("interaction_resolved", 600, toolCallId="c1"),
                kiro_call("c1", "execute_bash", 610, command="x"),
                kiro_result("c1", 650),
            ]
        )
        self.assertEqual(data.steps[0].duration_ms, 40)
        self.assertEqual(data.session.negative_durations, 0)

    def test_a_result_before_its_call_floors_at_zero_and_counts(self) -> None:
        data = one([kiro_call("c1", "read_file", 500, path="/a"), kiro_result("c1", 400)])
        self.assertEqual(data.steps[0].duration_ms, 0)
        self.assertEqual(data.session.negative_durations, 1)

    def test_bad_timestamps_give_null_times(self) -> None:
        call = kiro_call("c1", "read_file", 10, path="/a")
        call["timestamp"] = "not a time"
        data = one([call, kiro_result("c1", 20)])
        self.assertIsNone(data.steps[0].started_ms)
        self.assertIsNone(data.steps[0].duration_ms)
        self.assertEqual(data.steps[0].status, "ok")


class TurnTest(unittest.TestCase):
    def test_turn_start_and_end_set_times_and_stop_reason(self) -> None:
        data = one(
            [
                kiro_call("c1", "read_file", 5, path="/a"),
                kiro_event("turn_start", 10, executionId="e1"),
                kiro_event("turn_end", 90, executionId="e1", stopReason="cancelled"),
            ]
        )
        turn = data.turns[0]
        assert turn.started_ms is not None and turn.ended_ms is not None
        self.assertEqual(turn.ended_ms - turn.started_ms, 80)
        self.assertEqual(turn.stop_reason, "cancelled")

    def test_an_execution_without_turn_start_starts_at_its_first_event(self) -> None:
        data = one([kiro_call("c1", "read_file", 100, path="/a"), kiro_result("c1", 150)])
        turn = data.turns[0]
        assert turn.started_ms is not None and turn.ended_ms is not None
        self.assertEqual(turn.ended_ms - turn.started_ms, 50)
        self.assertIsNone(turn.stop_reason)

    def test_a_turn_end_alone_creates_no_turn(self) -> None:
        data = one([kiro_event("turn_end", 10, executionId="e9", stopReason="end_turn")])
        self.assertEqual(data.turns, ())

    def test_a_turn_with_no_steps_is_kept(self) -> None:
        data = one(
            [
                kiro_event("turn_start", 10, executionId="e1"),
                kiro_event("turn_end", 20, executionId="e1", stopReason="end_turn"),
            ]
        )
        self.assertEqual((len(data.turns), len(data.steps)), (1, 0))

    def test_executions_are_turns_in_order_of_first_appearance(self) -> None:
        data = one(
            [
                kiro_event("turn_start", 10, executionId="b"),
                kiro_event("turn_start", 20, executionId="a"),
                kiro_event("turn_start", 30, executionId="b"),
            ]
        )
        self.assertEqual(len(data.turns), 2)
        self.assertNotEqual(data.turns[0].turn_key, data.turns[1].turn_key)

    def test_credits_null_versus_zero_and_bad_values(self) -> None:
        def credits(summaries: object) -> float | None:
            data = one(
                [
                    kiro_event("turn_start", 0, executionId="e1"),
                    kiro_event("usage_summary", 5, executionId="e1", promptTurnSummaries=summaries),
                ]
            )
            return data.turns[0].credits

        self.assertEqual(credits([{"usage": 0}]), 0.0)  # a reported zero is not n/a
        self.assertIsNone(credits([]))
        self.assertIsNone(credits([{"unit": "credit"}]))
        self.assertIsNone(credits([{"usage": True}, {"usage": "1"}, {"usage": -1}]))
        self.assertEqual(credits([{"usage": 1}, {"usage": 2.5}]), 3.5)
        self.assertIsInstance(credits([{"usage": 1}]), float)

    def test_model_calls_sum_and_null_when_a_summary_lacks_request_ids(self) -> None:
        both = one(
            [
                kiro_event(
                    "usage_summary", 5, executionId="e1", promptTurnSummaries=[], requestIds=["a"]
                ),
                kiro_event(
                    "usage_summary",
                    6,
                    executionId="e1",
                    promptTurnSummaries=[],
                    requestIds=["b", "c"],
                ),
            ]
        )
        self.assertEqual(both.turns[0].model_calls, 3)
        mixed = one(
            [
                kiro_event(
                    "usage_summary", 5, executionId="e1", promptTurnSummaries=[], requestIds=["a"]
                ),
                kiro_event("usage_summary", 6, executionId="e1", promptTurnSummaries=[]),
            ]
        )
        self.assertIsNone(mixed.turns[0].model_calls)
        self.assertEqual(mixed.session.malformed_events, 0)

    def test_target_hash_takes_the_first_non_empty_string(self) -> None:
        data = one(
            [
                kiro_call("c1", "read_file", 10, path=["a"], file_path="/x"),
                kiro_call("c2", "read_file", 20, path="/x"),
                kiro_call("c3", "execute_bash", 30, path="/x"),
                kiro_call("c4", "read_file", 40),
            ]
        )
        hashes = [s.target_hash for s in data.steps]
        self.assertEqual(hashes[0], hashes[1])
        self.assertIsNotNone(hashes[0])
        self.assertIsNone(hashes[2])  # shell steps have none
        self.assertIsNone(hashes[3])


class DiscoveryTest(unittest.TestCase):
    def listing(self, root: Path) -> dict[str, str]:
        refs = find_kiro_sessions(root, 100)
        assert isinstance(refs, list)
        return {r.session_id: r.skip_reason for r in refs}

    def test_skip_reasons_in_precedence_order(self) -> None:
        events = [kiro_event("turn_start", 0, executionId="e1")]
        base: dict[str, object] = {
            "schemaVersion": "1.0.0",
            "dataModelVersion": 1,
            "agentMode": "vibe",
            "workspacePaths": ["/home/dev/proj"],
        }
        cases: dict[str, tuple[dict[str, object], str]] = {
            U + "001": ({**base, "workspacePaths": ["/tmp/x"]}, "eval traffic"),
            U + "002": ({**base, "workspacePaths": []}, "no cwd"),
            U + "003": ({**base, "schemaVersion": "2.0.0"}, "unknown format"),
            U + "004": ({**base, "dataModelVersion": 2}, "unknown format"),
            U + "005": (base, ""),
            U + "006": (
                {**base, "schemaVersion": "9.9.9", "workspacePaths": ["/tmp/x"]},
                "eval traffic",
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for sid, (meta, _) in cases.items():
                write_kiro_session(root / sid[-1], sid, events, meta)
            found: dict[str, str] = {}
            for sid in cases:
                found.update(self.listing(root / sid[-1]))
        self.assertEqual(found, {sid: reason for sid, (_, reason) in cases.items()})

    def test_unreadable_missing_and_headless_sessions(self) -> None:
        events = [kiro_event("turn_start", 0, executionId="e1")]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bad = write_kiro_session(root / "a", "20000000-0000-4000-8000-000000000001", events)
            (bad / "session.json").write_text("{not json")
            notobj = write_kiro_session(root / "b", "20000000-0000-4000-8000-000000000002", events)
            (notobj / "session.json").write_text("[1]")
            nomsg = write_kiro_session(root / "c", "20000000-0000-4000-8000-000000000003", events)
            (nomsg / "messages.jsonl").unlink()
            nometa = write_kiro_session(root / "d", "20000000-0000-4000-8000-000000000004", events)
            (nometa / "session.json").unlink()  # with a messages.jsonl: still never read
            cli = root / "cli"
            cli.mkdir()
            (cli / "30000000-0000-4000-8000-000000000001.jsonl").write_text("{}\n")
            (cli / "x.history").write_text("")
            found: dict[str, str] = {}
            for sub in "abcd":
                found.update(self.listing(root / sub))
            self.assertEqual(
                found,
                {
                    "20000000-0000-4000-8000-000000000001": "unreadable",
                    "20000000-0000-4000-8000-000000000002": "unreadable",
                    "20000000-0000-4000-8000-000000000003": "no messages file",
                    "20000000-0000-4000-8000-000000000004": "no session.json",
                },
            )
            refs = find_kiro_sessions(root, 100)  # the whole store, cli included
            assert isinstance(refs, list)
            self.assertEqual(len(refs), 1)  # a, b, c, d are not 16-hex-style groups here
            headless = [r for r in refs if r.skip_reason == "headless tree not read in v1"]
            self.assertEqual(len(headless), 1)  # the .history file is not a session

    def test_bounds_are_reasons_not_exceptions(self) -> None:
        events = [kiro_event("turn_start", 0, executionId="e1")]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_kiro_session(root, SID, events, subs={"s1": [], "s2": []})
            with mock.patch("traj.reader_kiro.SUB_FILES_MAX", 1):
                self.assertEqual(self.listing(root), {SID: "over bound"})
            self.assertEqual(find_kiro_sessions(root / "nope", 5), "nope is not a directory")
            write_kiro_session(root, SID[:-1] + "8", events)
            self.assertEqual(find_kiro_sessions(root, 1), "over 1 sessions")

    def test_a_read_over_the_line_or_byte_bound_is_a_skip(self) -> None:
        events = [kiro_event("turn_start", i, executionId=f"e{i}") for i in range(5)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_kiro_session(root, SID, events)
            refs = find_kiro_sessions(root, 5)
            assert isinstance(refs, list)
            lines = read_kiro_session(refs[0], 4, FILE_BYTES_MAX)
            size = read_kiro_session(refs[0], LINES_PER_FILE_MAX, 100)
            self.assertEqual(lines, Skip(SID, "kiro", "over bound"))
            self.assertEqual(size, Skip(SID, "kiro", "over bound"))
            (refs[0].path).unlink()
            self.assertEqual(
                read_kiro_session(refs[0], 4, FILE_BYTES_MAX), Skip(SID, "kiro", "unreadable")
            )

    def test_the_step_bound_is_a_skip(self) -> None:
        events = [kiro_call(f"c{i}", "read_file", i, path="/a") for i in range(3)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_kiro_session(root, SID, events)
            refs = find_kiro_sessions(root, 5)
            assert isinstance(refs, list)
            with mock.patch("traj.reader_kiro.STEPS_PER_SESSION_MAX", 2):
                self.assertEqual(
                    read_kiro_session(refs[0], 100, FILE_BYTES_MAX), Skip(SID, "kiro", "over bound")
                )


class UnknownTimeTest(unittest.TestCase):
    """An unparseable time stays null; it is never replaced by another event's time."""

    @staticmethod
    def bad(event: dict[str, object]) -> dict[str, object]:
        event["timestamp"] = "not a time"
        return event

    def test_a_bad_first_or_last_event_time_gives_null_turn_times(self) -> None:
        data = one(
            [
                self.bad(kiro_call("c1", "read_file", 5, path="/a")),
                kiro_result("c1", 50),
                self.bad(kiro_event("usage_summary", 60, executionId="e1", promptTurnSummaries=[])),
            ]
        )
        self.assertEqual((data.turns[0].started_ms, data.turns[0].ended_ms), (None, None))

    def test_a_valid_edge_with_a_bad_middle_keeps_the_edges(self) -> None:
        data = one(
            [
                kiro_call("c1", "read_file", 5, path="/a"),
                self.bad(kiro_result("c1", 30)),
                kiro_event("usage_summary", 60, executionId="e1", promptTurnSummaries=[]),
            ]
        )
        turn = data.turns[0]
        assert turn.started_ms is not None and turn.ended_ms is not None
        self.assertEqual(turn.ended_ms - turn.started_ms, 55)

    def test_a_turn_start_or_end_with_a_bad_time_gives_null_not_the_first_or_last_event(
        self,
    ) -> None:
        data = one(
            [
                kiro_call("c1", "read_file", 5, path="/a"),
                self.bad(kiro_event("turn_start", 10, executionId="e1")),
                kiro_result("c1", 50),
                self.bad(kiro_event("turn_end", 90, executionId="e1", stopReason="end_turn")),
            ]
        )
        self.assertEqual((data.turns[0].started_ms, data.turns[0].ended_ms), (None, None))
        self.assertEqual(data.turns[0].stop_reason, "end_turn")

    def test_a_sub_file_with_a_bad_first_or_last_time_gives_null_sub_turn_times(self) -> None:
        main = [kiro_call("m1", "read_file", 1, path="/a")]
        first_bad = [self.bad(kiro_call("s1", "read_file", 5, path="/a")), kiro_result("s1", 9)]
        last_bad = [kiro_call("s2", "read_file", 5, path="/a"), self.bad(kiro_result("s2", 9))]
        for sub, null_start, null_end in ((first_bad, True, False), (last_bad, False, True)):
            data = one(main, subs={"sub": sub})
            turn = data.turns[1]
            self.assertEqual(turn.depth, 1)
            self.assertEqual(
                (turn.started_ms is None, turn.ended_ms is None), (null_start, null_end)
            )


class BoundTest(unittest.TestCase):
    def read(self, root: Path) -> SessionData | Skip:
        refs = find_kiro_sessions(root, 5)
        assert isinstance(refs, list)
        return read_kiro_session(refs[0], LINES_PER_FILE_MAX, FILE_BYTES_MAX)

    def usage(self, count: int) -> dict[str, object]:
        summaries = [{"usage": 1} for _ in range(count)]
        return kiro_event("usage_summary", 5, executionId="e1", promptTurnSummaries=summaries)

    def test_summaries_at_the_limit_count_and_one_over_fails_closed(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("traj.reader_kiro.SUMMARIES_PER_EVENT_MAX", 3),
        ):
            root = Path(tmp)
            write_kiro_session(root / "a", SID, [self.usage(3)])
            at_limit = self.read(root / "a")
            assert isinstance(at_limit, SessionData)
            self.assertEqual(at_limit.turns[0].credits, 3.0)
            write_kiro_session(root / "b", SID, [self.usage(4)])
            self.assertEqual(self.read(root / "b"), Skip(SID, "kiro", "over bound"))

    def test_a_sub_file_usage_summary_is_not_used_so_it_is_not_bounded(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("traj.reader_kiro.SUMMARIES_PER_EVENT_MAX", 1),
        ):
            root = Path(tmp)
            main = [kiro_call("m1", "read_file", 1, path="/a")]
            write_kiro_session(root, SID, main, subs={"s1": [self.usage(5)]})
            self.assertIsInstance(self.read(root), SessionData)

    def test_the_step_budget_stops_the_scan_before_later_files_are_read(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("traj.reader_kiro.STEPS_PER_SESSION_MAX", 3),
        ):
            root = Path(tmp)
            main = [kiro_call("m1", "read_file", 1, path="/a")]
            two = [kiro_call(f"a{i}", "read_file", 2 + i, path="/a") for i in range(2)]
            write_kiro_session(root / "ok", SID, main, subs={"s1": two})
            at_limit = self.read(root / "ok")  # 1 + 2 steps, exactly the budget
            assert isinstance(at_limit, SessionData)
            self.assertEqual(len(at_limit.steps), 3)
            over = [kiro_call(f"b{i}", "read_file", 2 + i, path="/b") for i in range(3)]
            sess = write_kiro_session(root / "bad", SID, main, subs={"s1": over, "s2": two})
            refs = find_kiro_sessions(root / "bad", 5)
            assert isinstance(refs, list)
            unreadable = (
                sess / "sub-executions" / "s2.jsonl"
            )  # a later file the scan must not reach
            unreadable.chmod(0)
            try:
                result = read_kiro_session(refs[0], LINES_PER_FILE_MAX, FILE_BYTES_MAX)
            finally:
                unreadable.chmod(0o644)
            self.assertEqual(result, Skip(SID, "kiro", "over bound"))  # not "unreadable" from s2

    def test_the_stored_stats_are_taken_right_before_the_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sess = write_kiro_session(root, SID, [kiro_call("c1", "read_file", 1, path="/a")])
            refs = find_kiro_sessions(root, 5)
            assert isinstance(refs, list)
            with (sess / "messages.jsonl").open("a") as handle:
                handle.write(json.dumps(kiro_event("session_start", 2)) + "\n")  # after the listing
            data = read_kiro_session(refs[0], LINES_PER_FILE_MAX, FILE_BYTES_MAX)
            assert isinstance(data, SessionData)
            self.assertGreater(data.session.source_bytes, refs[0].source_bytes)
            self.assertEqual(data.session.events_total, 2)

    def test_unreadable_outranks_no_messages_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events = [kiro_event("turn_start", 0, executionId="e1")]
            sess = write_kiro_session(root, SID, events, subs={"s1": events})
            (sess / "messages.jsonl").unlink()
            subdir = sess / "sub-executions"
            subdir.chmod(0)
            try:
                refs = find_kiro_sessions(root, 5)
            finally:
                subdir.chmod(0o755)
            assert isinstance(refs, list)
            self.assertEqual(refs[0].skip_reason, "unreadable")


if __name__ == "__main__":
    unittest.main()
