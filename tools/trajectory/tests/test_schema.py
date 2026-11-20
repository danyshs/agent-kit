"""Round trip and every invariant violation."""

from __future__ import annotations

import json
import unittest
from dataclasses import FrozenInstanceError, replace
from dataclasses import fields as dataclass_fields

from tests.helpers import HEX, make_data, make_turn
from traj import limits
from traj.limits import SCHEMA_VERSION
from traj.schema import (
    RawCall,
    RawResult,
    Segment,
    SessionData,
    SessionRecord,
    Shape,
    StepRecord,
    TurnRecord,
    check_session,
    decode_session,
    decode_session_line,
    encode_session,
    scalar_fields,
)


def _with_step(data: SessionData, index: int, **changes: object) -> SessionData:
    steps = list(data.steps)
    steps[index] = replace(steps[index], **changes)
    return replace(data, steps=tuple(steps))


def _with_turn(data: SessionData, index: int, **changes: object) -> SessionData:
    turns = list(data.turns)
    turns[index] = replace(turns[index], **changes)
    return replace(data, turns=tuple(turns))


class RoundTripTest(unittest.TestCase):
    def test_round_trip_and_format(self) -> None:
        data = make_data()
        lines = encode_session(data)
        self.assertEqual(len(lines), 1 + 2 + 3)
        self.assertEqual(decode_session(lines), data)
        for line in lines:
            self.assertNotIn(" ", line)  # no spaces anywhere in this data
            body = json.loads(line)
            self.assertEqual(line, json.dumps(body, sort_keys=True, separators=(",", ":")))
        kinds = [json.loads(line)["kind"] for line in lines]
        self.assertEqual(kinds, ["session", "turn", "turn", "step", "step", "step"])

    def test_decode_rejects_bad_files(self) -> None:
        lines = encode_session(make_data())
        self.assertIsInstance(decode_session([]), str)
        self.assertIsInstance(decode_session(lines[:-1]), str)  # count mismatch
        self.assertIsInstance(decode_session([*lines, lines[-1]]), str)
        self.assertIsInstance(decode_session(["not json", *lines[1:]]), str)
        self.assertIsInstance(decode_session(["[1]", *lines[1:]]), str)
        session = json.loads(lines[0])
        session["schema_version"] = SCHEMA_VERSION + 1
        wrong = json.dumps(session, sort_keys=True)
        self.assertIsInstance(decode_session([wrong, *lines[1:]]), str)
        session["schema_version"] = SCHEMA_VERSION
        session["extra"] = 1
        self.assertIsInstance(decode_session([json.dumps(session), *lines[1:]]), str)
        session.pop("extra")
        session["turns"] = True  # a bool is not an int
        self.assertIsInstance(decode_session([json.dumps(session), *lines[1:]]), str)

    def test_decode_rejects_wrong_kind_and_types(self) -> None:
        lines = encode_session(make_data())
        step = json.loads(lines[3])
        step["kind"] = "turn"
        self.assertIsInstance(decode_session([*lines[:3], json.dumps(step), *lines[4:]]), str)
        step["kind"] = "step"
        step["duration_ms"] = "5"
        self.assertIsInstance(decode_session([*lines[:3], json.dumps(step), *lines[4:]]), str)
        turn = json.loads(lines[1])
        turn["credits"] = float("nan")
        self.assertIsInstance(decode_session([lines[0], json.dumps(turn), *lines[2:]]), str)

    def test_credits_int_decodes_as_float(self) -> None:
        lines = encode_session(make_data())
        turn = json.loads(lines[1])
        turn["credits"] = 2
        decoded = decode_session([lines[0], json.dumps(turn), *lines[2:]])
        assert isinstance(decoded, SessionData)
        self.assertEqual(decoded.turns[0].credits, 2.0)
        self.assertIsInstance(decoded.turns[0].credits, float)


class InvariantTest(unittest.TestCase):
    def test_valid_data_passes(self) -> None:
        self.assertIsNone(check_session(make_data()))

    def test_session_line_violations(self) -> None:
        data = make_data()
        for field, value in (
            ("harness", "gemini"),
            ("tree", "main"),
            ("session_id", "../../x"),
            ("workspace_hash", "xyz"),
            ("workspace_label", "has space"),
            ("model_label", "x" * 65),
            ("created_day", "yesterday"),
            ("source_bytes", -1),
            ("turns", 3),
            ("steps", 2),
        ):
            bad = replace(data, session=replace(data.session, **{field: value}))
            self.assertIsNotNone(check_session(bad), field)

    def test_turn_violations(self) -> None:
        data = make_data()
        for field, value in (
            ("turn_index", 5),
            ("turn_key", "nothex"),
            ("depth", 2),
            ("steps", 9),
            ("errors", 0),
            ("credits", -1.0),
            ("input_tokens", 5),  # Kiro carries no tokens
            ("parallel_groups", 1),
            ("stop_reason", "has space"),
        ):
            self.assertIsNotNone(check_session(_with_turn(data, 0, **{field: value})), field)
        self.assertIsNotNone(check_session(_with_turn(data, 1, credits=1.0)))  # depth 1: no credits
        self.assertIsNotNone(check_session(_with_turn(data, 1, parent_turn_index=1)))
        self.assertIsNotNone(check_session(_with_turn(data, 1, parent_turn_index=7)))
        self.assertIsNotNone(check_session(_with_turn(data, 0, parent_turn_index=0)))
        self.assertIsNone(check_session(_with_turn(data, 1, parent_turn_index=None)))  # orphan ok

    def test_step_violations(self) -> None:
        data = make_data()
        for field, value in (
            ("step_index", 4),
            ("turn_index", 9),
            ("tool_class", "magic"),
            ("status", "maybe"),
            ("args_hash", "short"),
            ("tool", "a b"),
            ("target_hash", "zz"),
            ("dispatch_group", 0),  # Kiro: null only
            ("duration_ms", -1),
        ):
            self.assertIsNotNone(check_session(_with_step(data, 0, **{field: value})), field)
        self.assertIsNotNone(check_session(_with_step(data, 1, tool_class="read")))  # shape on read
        self.assertIsNotNone(check_session(_with_step(data, 0, exit_code=0)))  # exit on read
        self.assertIsNotNone(check_session(_with_step(data, 0, shape=data.steps[1].shape)))
        unpaired = _with_step(data, 2, status="unpaired")
        self.assertIsNotNone(check_session(unpaired))  # duration must be null
        fixed = _with_step(unpaired, 2, duration_ms=None)
        self.assertIsNotNone(check_session(fixed))  # fewer unpaired_calls than unpaired steps
        counted = replace(fixed, session=replace(fixed.session, unpaired_calls=1))
        self.assertIsNone(check_session(counted))

    def test_omitted_boundaries(self) -> None:
        data = make_data()
        for value in (-1, 99):
            bad = replace(data, session=replace(data.session, schema_version=value))
            self.assertIsNotNone(check_session(bad), "schema_version")
        for field in ("reader_version", "rule_version"):
            old = replace(data, session=replace(data.session, **{field: 99}))
            self.assertIsNone(check_session(old), field)  # stale code version is not corruption
            negative = replace(data, session=replace(data.session, **{field: -1}))
            self.assertIsNotNone(check_session(negative), field)
        for field in ("started_ms", "ended_ms", "model_calls", "permission_wait_ms"):
            self.assertIsNotNone(check_session(_with_turn(data, 0, **{field: -1})), field)
        # Kiro depth 1: no model_calls, no stop_reason
        self.assertIsNotNone(check_session(_with_turn(data, 1, model_calls=1)))
        self.assertIsNotNone(check_session(_with_turn(data, 1, stop_reason="end_turn")))
        self.assertIsNotNone(check_session(_with_step(data, 1, exit_code=-1)))
        self.assertIsNotNone(check_session(_with_step(data, 1, started_ms=-1)))
        self.assertIsNotNone(check_session(_with_step(data, 1, permission_wait_ms=-1)))
        bad_count = replace(data, session=replace(data.session, unpaired_calls=1))
        self.assertIsNotNone(check_session(bad_count))  # no denied or unpaired steps: only 0 valid
        # a denied step means a resultless call counted as a call but not as unpaired
        denied = _with_step(data, 0, status="denied")
        denied_one = replace(denied, session=replace(denied.session, unpaired_calls=1))
        self.assertIsNone(check_session(denied_one))  # one denied call is a valid count of 1
        too_many = replace(denied, session=replace(denied.session, unpaired_calls=2))
        self.assertIsNotNone(check_session(too_many))  # exceeds denied + unpaired
        over = replace(data, session=replace(data.session, turns=limits.TURNS_PER_SESSION_MAX + 1))
        self.assertIsNotNone(check_session(over))

    def test_claude_shell_step_has_no_exit_code(self) -> None:
        data = make_data()
        line = replace(data.session, harness="claude", tree="main")
        claude = SessionData(line, data.turns, data.steps)
        claude = replace(claude, turns=tuple(replace(t, credits=None) for t in claude.turns))
        claude = replace(
            claude, turns=tuple(replace(t, parent_turn_index=None) for t in claude.turns)
        )
        self.assertIsNotNone(check_session(claude))  # step 1 is a shell step with exit_code 1
        self.assertIsNone(check_session(_with_step(claude, 1, exit_code=None)))

    def test_shape_flags_must_be_bools(self) -> None:
        data = make_data()
        shell = data.steps[1]
        assert shell.shape is not None
        for field in ("has_heredoc", "has_commit_trailer", "truncated"):
            for junk in ("CANARY", 1, None):
                shape = replace(shell.shape, **{field: junk})
                self.assertIsNotNone(check_session(_with_step(data, 1, shape=shape)), field)
                with self.assertRaises(AssertionError):
                    encode_session(_with_step(data, 1, shape=shape))

    def test_per_program_subcommands(self) -> None:
        data = make_data()

        def shaped(prog: str, sub: str) -> SessionData:
            return _with_step(data, 1, shape=Shape((Segment(prog, sub, ()),), False, False, False))

        self.assertIsNone(check_session(shaped("git", "commit")))
        self.assertIsNone(check_session(shaped("gh", "pr")))
        self.assertIsNotNone(check_session(shaped("git", "pr")))
        self.assertIsNotNone(check_session(shaped("gh", "commit")))

    def test_decode_rejects_null_in_a_non_null_field(self) -> None:
        lines = encode_session(make_data())
        step = json.loads(lines[3])
        for field in ("tool", "args_hash", "status", "tool_class"):
            broken = {**step, field: None}
            result = decode_session([*lines[:3], json.dumps(broken), *lines[4:]])
            self.assertIsInstance(result, str, field)
        head = json.loads(lines[0])
        head["session_id"] = None
        self.assertIsInstance(decode_session([json.dumps(head), *lines[1:]]), str)

    def test_raw_records_are_frozen_and_hold_no_text_fields(self) -> None:
        call = RawCall("toolu_1", "Bash", HEX, None, 5, 0, None)
        result = RawResult("toolu_1", 9, "ok", None)
        self.assertEqual((call.call_id, result.call_id), ("toolu_1", "toolu_1"))
        with self.assertRaises(FrozenInstanceError):
            call.tool = "x"  # type: ignore[misc]  # the point of the test: frozen

    def test_shape_vocabulary(self) -> None:
        data = make_data()

        def shaped(segment: Segment, truncated: bool = False) -> SessionData:
            return _with_step(data, 1, shape=Shape((segment,), False, False, truncated))

        self.assertIsNone(check_session(shaped(Segment("(other)", None, ()))))
        self.assertIsNone(check_session(shaped(Segment("git", "push", ("--force", "-n")))))
        self.assertIsNotNone(check_session(shaped(Segment("rm -rf /", None, ()))))
        self.assertIsNotNone(check_session(shaped(Segment("git", "my-secret", ()))))
        self.assertIsNotNone(
            check_session(shaped(Segment("ls", "commit", ())))
        )  # sub only on git/gh
        self.assertIsNotNone(check_session(shaped(Segment("git", None, ("--token=abc",)))))
        self.assertIsNotNone(check_session(shaped(Segment("git", None, ("-n", "--force")))))
        too_many = Shape(tuple(Segment("ls", None, ()) for _ in range(17)), False, False, False)
        self.assertIsNotNone(check_session(_with_step(data, 1, shape=too_many)))

    def test_claude_nulls(self) -> None:
        data = make_data()
        line = replace(data.session, harness="claude", tree="main")
        turns = (
            replace(make_turn(0, 0, 2, 1, None), credits=None, input_tokens=10),
            replace(make_turn(1, 1, 1, 0, None), credits=None),
        )
        claude = SessionData(line, turns, tuple(replace(s, shape=None) for s in data.steps))
        claude = _with_step(claude, 1, tool_class="read", target_hash=HEX, exit_code=None)
        self.assertIsNone(check_session(claude))
        self.assertIsNotNone(check_session(_with_turn(claude, 0, credits=1.0)))
        self.assertIsNotNone(check_session(_with_turn(claude, 0, permission_wait_ms=5)))
        self.assertIsNotNone(check_session(_with_step(claude, 0, permission_wait_ms=5)))
        self.assertIsNone(check_session(_with_step(claude, 0, dispatch_group=0)))
        self.assertIsNotNone(check_session(_with_turn(claude, 1, parent_turn_index=0)))

    def test_every_scalar_field_is_type_checked(self) -> None:
        # pins the checked-field counts so a new field or an odd annotation cannot go unchecked;
        # the unchecked ones are the nested record (StepRecord.shape) and nothing else
        counts = {SessionRecord: 23, TurnRecord: 17, StepRecord: 12}
        for cls, want in counts.items():
            checked = {name for name, _, _ in scalar_fields(cls)}
            self.assertEqual(len(checked), want, cls.__name__)
            every = {spec.name for spec in dataclass_fields(cls)}
            self.assertEqual(every - checked, {"shape"} if cls is StepRecord else set())
        optional = {name for name, _, opt in scalar_fields(TurnRecord) if opt}
        self.assertIn("credits", optional)
        self.assertNotIn("turn_index", optional)

    def test_non_shape_values_get_a_reason_not_an_exception(self) -> None:
        data = make_data()
        junk_shapes: list[object] = ["ls", 5, {"segments": []}, [Segment("ls", None, ())]]
        for junk in junk_shapes:
            self.assertIsNotNone(check_session(_with_step(data, 1, shape=junk)), repr(junk))
        for segments in (["x"], ("x",), [Segment("ls", None, ())], 5):
            shape = Shape(segments, False, False, False)  # type: ignore[arg-type]  # junk on purpose
            self.assertIsNotNone(check_session(_with_step(data, 1, shape=shape)), repr(segments))
        listy = Segment("ls", None, ["-n"])  # type: ignore[arg-type]  # junk on purpose
        shape = Shape((listy,), False, False, False)
        self.assertIsNotNone(check_session(_with_step(data, 1, shape=shape)))

    def test_credits_is_a_real_float(self) -> None:
        data = make_data()
        self.assertIsNone(check_session(_with_turn(data, 0, credits=0.0)))
        for junk in (1, True, "1", float("nan")):
            self.assertIsNotNone(check_session(_with_turn(data, 0, credits=junk)), repr(junk))
        lines = encode_session(data)
        turn = json.loads(lines[1])
        turn["credits"] = 2  # a JSON int decodes to a float
        result = decode_session([lines[0], json.dumps(turn), *lines[2:]])
        assert not isinstance(result, str)
        self.assertEqual(result.turns[0].credits, 2.0)
        self.assertIs(type(result.turns[0].credits), float)

    def test_session_line_decodes_alone(self) -> None:
        lines = encode_session(make_data())
        self.assertEqual(decode_session_line(lines[0]), make_data().session)
        self.assertIsInstance(decode_session_line(lines[1]), str)  # a turn line
        self.assertIsInstance(decode_session_line("x" * 9_000), str)  # over the bound

    def test_encode_refuses_invalid_data(self) -> None:
        bad = _with_step(make_data(), 0, status="maybe")
        with self.assertRaises(AssertionError):
            encode_session(bad)
