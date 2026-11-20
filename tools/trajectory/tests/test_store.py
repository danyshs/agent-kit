"""Atomic write, validated load, the rebuild decision, listing."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest import mock

from tests.helpers import (
    CANARY,
    OTHER_SESSION_ID,
    SESSION_ID,
    make_data,
    make_session_line,
    make_turn,
    worst_line_bytes,
)
from traj import limits, store
from traj.common import NAME_RE, clean_name
from traj.limits import READER_VERSION, RULE_VERSION, SCHEMA_VERSION
from traj.schema import SessionData, SessionRef, check_session, encode_session


def _ref(data: SessionData) -> SessionRef:
    rec = data.session
    return SessionRef(
        harness=rec.harness,
        session_id=rec.session_id,
        tree=rec.tree,
        path=Path("/nonexistent/messages.jsonl"),
        sub_paths=(),
        cwds=("/home/x/dev",),
        created_day=rec.created_day,
        skip_reason="",
        source_bytes=rec.source_bytes,
        source_mtime_ms=rec.source_mtime_ms,
    )


class WriteLoadTest(unittest.TestCase):
    def test_round_trip_is_byte_identical(self) -> None:
        data = make_data()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            store.write_session(out, data)
            path = store.events_path(out, "kiro", SESSION_ID)
            first = path.read_bytes()
            self.assertEqual(store.load_session(path), data)
            store.write_session(out, data)  # rewrite: same bytes
            self.assertEqual(path.read_bytes(), first)
            loaded = store.load_session(path)
            assert isinstance(loaded, SessionData)
            store.write_session(out, loaded)
            self.assertEqual(path.read_bytes(), first)
            self.assertTrue(first.endswith(b"\n"))
            self.assertEqual(sorted(p.name for p in path.parent.iterdir()), [path.name])

    def test_failed_write_keeps_the_old_file_and_leaves_no_temp(self) -> None:
        data = make_data()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            store.write_session(out, data)
            path = store.events_path(out, "kiro", SESSION_ID)
            before = path.read_bytes()
            changed = replace(data, session=replace(data.session, source_bytes=999))
            with mock.patch("traj.store.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    store.write_session(out, changed)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(sorted(p.name for p in path.parent.iterdir()), [path.name])

    def test_path_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for bad in ("../x", "..", "a/b", "", SESSION_ID.upper(), SESSION_ID + "\n"):
                with self.assertRaises(ValueError, msg=repr(bad)):
                    store.events_path(Path(tmp), "kiro", bad)
            with self.assertRaises(ValueError):
                store.events_path(Path(tmp), "../kiro", SESSION_ID)

    def test_load_rejects_invalid_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"{SESSION_ID}.jsonl"
            self.assertIsInstance(store.load_session(path), str)  # missing
            path.write_text("garbage\n", encoding="utf-8")
            self.assertIsInstance(store.load_session(path), str)
            path.write_bytes(b"\xff\xfe\n")
            self.assertIsInstance(store.load_session(path), str)
            with mock.patch("traj.store.EVENTS_BYTES_MAX", 3):
                path.write_text("abcdef", encoding="utf-8")
                self.assertIsInstance(store.load_session(path), str)
            with mock.patch("traj.store.EVENTS_LINES_MAX", 2):  # checked before any decode
                path.write_text("a\nb\nc\nd\n", encoding="utf-8")
                result = store.load_session(path)
                self.assertIn("lines", str(result))


class BoundsTest(unittest.TestCase):
    def test_turns_without_steps_survive_a_round_trip(self) -> None:
        data = make_data()
        empty = replace(data.session, turns=5, steps=0)
        turns = tuple(make_turn(i, 0, 0, 0, None) for i in range(5))
        bare = SessionData(replace(empty), turns, ())
        with tempfile.TemporaryDirectory() as tmp:
            store.write_session(Path(tmp), bare)
            path = store.events_path(Path(tmp), "kiro", SESSION_ID)
            self.assertEqual(store.load_session(path), bare)
        self.assertGreater(limits.EVENTS_LINES_MAX, 1 + limits.STEPS_PER_SESSION_MAX + 150_000)

    def test_canary_in_a_shape_flag_is_never_written(self) -> None:
        # a canary must contain a character NAME_RE rejects, or clean_name would let it through
        self.assertEqual(clean_name(CANARY), limits.OTHER)
        self.assertIsNone(NAME_RE.fullmatch(CANARY))
        canary = CANARY
        data = make_data()
        shell = data.steps[1]
        assert shell.shape is not None
        for field in ("has_heredoc", "has_commit_trailer", "truncated"):
            shape = replace(shell.shape, **{field: canary})
            steps = (data.steps[0], replace(shell, shape=shape), data.steps[2])
            bad = replace(data, steps=steps)
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ValueError, msg=field):
                    store.write_session(Path(tmp), bad)
                self.assertEqual(list(Path(tmp).rglob("*")), [])

    def test_write_refuses_invalid_records_before_touching_disk(self) -> None:
        data = make_data()
        bad_session = (
            ("schema_version", True),
            ("workspace_label", None),
            ("source_bytes", True),
            ("harness_version", 1),
            ("turns", 2.0),
        )
        for field, junk in bad_session:
            bad = replace(data, session=replace(data.session, **{field: junk}))
            self.assertIsNotNone(check_session(bad), field)
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ValueError, msg=field):
                    store.write_session(Path(tmp), bad)
                self.assertEqual(list(Path(tmp).rglob("*")), [])
        for junk in (True, float("nan"), float("inf"), "1"):
            turn = replace(data.turns[0], credits=junk)
            bad = replace(data, turns=(turn, data.turns[1]))
            self.assertIsNotNone(check_session(bad), repr(junk))
            with self.assertRaises(AssertionError):
                encode_session(bad)
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ValueError, msg=repr(junk)):
                    store.write_session(Path(tmp), bad)
        flagged = replace(data, steps=(replace(data.steps[0], step_index=True), *data.steps[1:]))
        self.assertIsNotNone(check_session(flagged))  # True == 0 must not pass as an int

    def test_write_refuses_a_file_load_would_refuse(self) -> None:
        data = make_data()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            size = len(("\n".join(encode_session(data)) + "\n").encode("utf-8"))
            with mock.patch("traj.store.EVENTS_BYTES_MAX", size):  # at the bound: written, loads
                store.write_session(out, data)
                path = store.events_path(out, "kiro", SESSION_ID)
                self.assertEqual(store.load_session(path), data)
            path.unlink()
            with mock.patch("traj.store.EVENTS_BYTES_MAX", size - 1):  # one byte over
                with self.assertRaises(ValueError):
                    store.write_session(out, data)
                self.assertFalse(path.exists())
                self.assertEqual(list(path.parent.iterdir()), [])

    def test_the_largest_valid_session_fits_the_load_bound(self) -> None:
        turn_bytes, step_bytes = worst_line_bytes()
        worst = (
            turn_bytes * limits.TURNS_PER_SESSION_MAX
            + step_bytes * limits.STEPS_PER_SESSION_MAX
            + 4_096  # the session line
        )
        self.assertLessEqual(worst, limits.EVENTS_BYTES_MAX)
        self.assertGreater(worst, limits.EVENTS_BYTES_MAX // 2)  # the bound is not absurdly loose

    def test_the_session_line_fits_its_bound(self) -> None:
        word = "w" * limits.NAME_CHARS_MAX
        big = 10**18
        line = replace(
            make_session_line(limits.TURNS_PER_SESSION_MAX, limits.STEPS_PER_SESSION_MAX),
            workspace_label=word,
            harness_version=word,
            model_label=word,
            source_bytes=big,
            source_mtime_ms=big,
            parse_errors=big,
            malformed_events=big,
            unknown_events=big // 4,
            events_used=big // 2,
            events_total=big,
            unpaired_calls=big,
            usage_conflicts=big,
            negative_durations=big,
        )
        text = json.dumps(asdict(line), sort_keys=True, separators=(",", ":"))
        self.assertLess(len(text) + 20, limits.SESSION_LINE_BYTES_MAX // 4)  # 4x slack

    def test_list_stored_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "kiro").mkdir()
            for index in range(3):
                (out / "kiro" / f"{index:08x}-0000-4000-8000-123456789abc.jsonl").write_text("x")
            self.assertEqual(len(store.list_stored(out, "kiro", 3)), 3)  # at the cap
            reason = store.list_stored(out, "kiro", 2)
            self.assertIsInstance(reason, str)
            self.assertIn("2", str(reason))
            for index in range(3):
                (out / "kiro" / f"stray{index}.txt").write_text("x")
            self.assertIsInstance(store.list_stored(out, "kiro", 3), list)  # 6 entries, at the cap
            (out / "kiro" / "stray3.txt").write_text("x")
            self.assertIsInstance(store.list_stored(out, "kiro", 3), str)  # 7 entries


class NeedsRebuildTest(unittest.TestCase):
    def test_decision_table(self) -> None:
        data = make_data()
        ref = _ref(data)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            self.assertTrue(store.needs_rebuild(out, ref, False))  # no file yet
            store.write_session(out, data)
            self.assertFalse(store.needs_rebuild(out, ref, False))
            self.assertTrue(store.needs_rebuild(out, ref, True))  # force
            self.assertTrue(store.needs_rebuild(out, replace(ref, source_bytes=1), False))
            self.assertTrue(store.needs_rebuild(out, replace(ref, source_mtime_ms=1), False))
            for field, value in (
                ("reader_version", READER_VERSION + 1),
                ("rule_version", RULE_VERSION + 1),
                ("schema_version", SCHEMA_VERSION + 1),
            ):
                lines = encode_session(data)
                head = json.loads(lines[0])
                head[field] = value  # a file from other code: edit the JSON, not the encoder
                lines[0] = json.dumps(head, sort_keys=True, separators=(",", ":"))
                path = store.events_path(out, "kiro", SESSION_ID)
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                self.assertTrue(store.needs_rebuild(out, ref, False), field)
                # a reader or rule version bump still loads (the raw log may be gone)
                self.assertEqual(
                    isinstance(store.load_session(path), SessionData), field != "schema_version"
                )

    def test_invalid_file_is_rebuilt(self) -> None:
        data = make_data()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            store.write_session(out, data)
            path = store.events_path(out, "kiro", SESSION_ID)
            path.write_text("garbage\n", encoding="utf-8")
            self.assertTrue(store.needs_rebuild(out, _ref(data), False))

    def test_only_the_session_line_is_read(self) -> None:
        data = make_data()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            store.write_session(out, data)
            path = store.events_path(out, "kiro", SESSION_ID)
            head = encode_session(data)[0]
            path.write_text(head + "\ngarbage body\n", encoding="utf-8")
            self.assertFalse(store.needs_rebuild(out, _ref(data), False))  # body is not read
            self.assertIsInstance(store.load_session(path), str)  # report would count it
            path.write_text("x" * (limits.SESSION_LINE_BYTES_MAX + 10), encoding="utf-8")
            self.assertTrue(store.needs_rebuild(out, _ref(data), False))  # over the line bound

    def test_skipped_ref_is_a_bug(self) -> None:
        ref = replace(_ref(make_data()), skip_reason="unreadable")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(AssertionError):
                store.needs_rebuild(Path(tmp), ref, False)


class ListingTest(unittest.TestCase):
    def test_list_stored_and_count_without_raw(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            self.assertEqual(store.list_stored(out, "kiro"), [])  # no directory yet
            store.write_session(out, make_data())
            store.write_session(out, make_data(OTHER_SESSION_ID))
            kiro_dir = out / "kiro"
            (kiro_dir / "notes.txt").write_text("x", encoding="utf-8")
            (kiro_dir / "not-a-uuid.jsonl").write_text("x", encoding="utf-8")
            (kiro_dir / f".{SESSION_ID}.tmp").write_text("x", encoding="utf-8")
            stored = store.list_stored(out, "kiro")
            assert isinstance(stored, list)
            self.assertEqual([p.stem for p in stored], sorted([SESSION_ID, OTHER_SESSION_ID]))
            self.assertEqual(store.list_stored(out, "claude"), [])
            both = [_ref(make_data()), _ref(make_data(OTHER_SESSION_ID))]
            self.assertEqual(store.count_without_raw(stored, both), 0)
            self.assertEqual(store.count_without_raw(stored, both[:1]), 1)
            self.assertEqual(store.count_without_raw(stored, []), 2)
            skipped = [replace(both[1], skip_reason="unreadable"), both[0]]
            self.assertEqual(store.count_without_raw(stored, skipped), 0)  # raw log still exists
