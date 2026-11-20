"""Parity with the real miner for the four copied helpers, plus the helpers that are not copies."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType
from typing import cast
from unittest import mock

from traj import common
from traj.limits import CWDS_PER_SESSION_MAX, OTHER

MINER_PATH = Path(__file__).resolve().parents[2] / "evals" / "lib" / "miner.py"


def _load_miner() -> ModuleType:
    evals_root = str(MINER_PATH.parents[1])
    if evals_root not in sys.path:
        sys.path.insert(0, evals_root)  # the miner imports lib.run from its own tool
    spec = importlib.util.spec_from_file_location("miner_under_test", MINER_PATH)
    assert spec is not None and spec.loader is not None, "the miner file loads"
    module = importlib.util.module_from_spec(spec)
    sys.modules["miner_under_test"] = module  # dataclasses looks the module up by name
    spec.loader.exec_module(module)
    return module


MINER = _load_miner()
EventsResult = tuple[list[dict[str, object]], int] | str


class ParityTest(unittest.TestCase):
    def test_args_hash(self) -> None:
        theirs = cast(Callable[[dict[str, object]], str], MINER._args_hash)
        cases: list[dict[str, object]] = [
            {},
            {"b": 1, "a": [1, 2]},
            {"path": "x/y", "n": None},
            {"k": {"z": 1, "a": 2}},
        ]
        for args in cases:
            self.assertEqual(common.args_hash(args), theirs(args))

    def test_exit_code(self) -> None:
        theirs = cast(Callable[[str], int | None], MINER._exit_code)
        long_tail = "x" * 500 + "\nExit Code: 7\n"
        for text in ("", "Exit Code: 0", "out\nExit Code: 12\n", "Exit Code: 123456", long_tail):
            self.assertEqual(common.exit_code_from_tail(text), theirs(text))
        self.assertEqual(common.exit_code_from_tail("out\nExit Code: 12\n"), 12)
        self.assertIsNone(common.exit_code_from_tail("Exit Code: 3\n" + "y" * 100))

    def test_read_events_and_bounds(self) -> None:
        theirs = cast(Callable[[Path, int, int], EventsResult], MINER.read_events)
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "good.jsonl"
            good.write_text('{"a":1}\nnot json\n\n[1]\n{"b":2}\n', encoding="utf-8")
            cases = [
                (good, 100, 10_000),
                (good, 2, 10_000),
                (good, 100, 5),
                (Path(tmp) / "no", 5, 5),
            ]
            for path, lines_max, bytes_max in cases:
                ours = common.read_events(path, lines_max, bytes_max)
                self.assertEqual(ours, theirs(path, lines_max, bytes_max))
            events = common.read_events(good, 100, 10_000)
            self.assertEqual(events, ([{"a": 1}, {"b": 2}], 2))
            self.assertIsInstance(common.read_events(good, 2, 10_000), str)  # line bound
            self.assertIsInstance(common.read_events(good, 100, 5), str)  # byte bound

    def test_read_events_stops_a_file_that_grew_after_the_stat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "grown.jsonl"
            path.write_text('{"a":1}\n' * 100, encoding="utf-8")  # 800 bytes on disk
            small = mock.Mock(st_size=10)  # what the stat saw before the append
            with mock.patch.object(Path, "stat", return_value=small):
                result = common.read_events(path, 1_000, 50)
            self.assertIsInstance(result, str)
            assert isinstance(result, str)
            self.assertIn("grew past", result)

    def test_is_eval_traffic(self) -> None:
        theirs = cast(Callable[[Sequence[str], Sequence[Path]], bool], MINER.is_eval_traffic)
        roots = [Path("/tmp").resolve()]
        for cwds in (["/tmp/x"], ["/private/tmp"], ["/home/someone/dev"], [], ["/tmpfoo"]):
            self.assertEqual(common.is_eval_traffic(cwds, roots), theirs(cwds, roots))
        self.assertTrue(common.is_eval_traffic(["/tmp/x"], roots))
        self.assertFalse(common.is_eval_traffic(["/home/someone/dev"], roots))

    def test_is_eval_traffic_survives_junk_and_bounds(self) -> None:
        roots = [Path("/tmp").resolve()]
        junk: list[object] = [None, 5, ["/tmp/x"], {"a": 1}, b"/tmp/x", "", "/home/someone/dev"]
        self.assertFalse(common.is_eval_traffic(junk, roots))  # no TypeError, no match
        self.assertTrue(common.is_eval_traffic([*junk, "/tmp/x"], roots))
        many: list[object] = ["/home/a"] * CWDS_PER_SESSION_MAX
        self.assertFalse(common.is_eval_traffic(many, roots))
        self.assertTrue(common.is_eval_traffic([*many[:-1], "/tmp/x"], roots))  # at the bound
        self.assertFalse(common.is_eval_traffic([*many, "/tmp/x"], roots))  # past it: ignored


class NameTest(unittest.TestCase):
    def test_clean_name_keeps_case_and_bounds(self) -> None:
        self.assertEqual(common.clean_name("Bash"), "Bash")
        self.assertEqual(common.clean_name("WebFetch"), "WebFetch")
        self.assertEqual(common.clean_name("mcp__srv__tool"), "mcp__srv__tool")
        self.assertEqual(common.clean_name("a" * 64), "a" * 64)
        self.assertEqual(common.clean_name("a" * 65), OTHER)
        self.assertEqual(common.clean_name("has space"), OTHER)
        self.assertEqual(common.clean_name("naïve"), OTHER)
        self.assertEqual(common.clean_name(""), OTHER)
        self.assertEqual(common.clean_name("ok\n"), OTHER)  # fullmatch, not `$`

    def test_session_id_re(self) -> None:
        self.assertIsNotNone(common.SESSION_ID_RE.fullmatch("0a1b2c3d-0000-4000-8000-123456789abc"))
        self.assertIsNone(common.SESSION_ID_RE.fullmatch("0A1B2C3D-0000-4000-8000-123456789abc"))
        self.assertIsNone(common.SESSION_ID_RE.fullmatch("../../etc/passwd"))
        self.assertIsNone(common.SESSION_ID_RE.fullmatch("0a1b2c3d-0000-4000-8000-123456789abc\n"))

    def test_coercers_and_hash(self) -> None:
        self.assertEqual(common.as_dict([1]), {})
        self.assertEqual(common.as_list({"a": 1}), [])
        self.assertEqual(common.as_str(3), "")
        self.assertEqual(len(common.short_hash("x")), 12)
        self.assertNotEqual(common.short_hash("x"), common.short_hash("y"))


class ParseIsoTest(unittest.TestCase):
    def test_good(self) -> None:
        self.assertEqual(common.parse_iso_ms("1970-01-01T00:00:00.000Z"), 0)
        self.assertEqual(common.parse_iso_ms("1970-01-01T00:00:01.250Z"), 1250)
        self.assertEqual(common.parse_iso_ms("2026-11-14T13:24:05.007Z"), 1_794_662_645_007)
        self.assertEqual(common.parse_iso_ms("1970-01-01T00:00:02Z"), 2000)  # no fraction

    def test_bad(self) -> None:
        for text in ("", "nope", "2026-13-01T00:00:00.000Z", "2026-11-14 13:24:05.007Z"):
            self.assertIsNone(common.parse_iso_ms(text))
        self.assertIsNone(common.parse_iso_ms("2026-11-14T13:24:05.007+00:00"))
        self.assertIsNone(common.parse_iso_ms("2026-02-30T00:00:00.000Z"))
        self.assertIsNone(common.parse_iso_ms("1" * 500))
