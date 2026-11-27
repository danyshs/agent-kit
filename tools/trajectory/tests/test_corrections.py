"""v2 step 1: the weekly correction list. Each test builds small transcripts in a temp projects
folder (the spec's fixture table, written by `_world`) and checks one rule of the spec."""

from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from traj import corrections
from traj.cli import main, parse_corrections_args
from traj.corrections import Coverage, Entry, classify, clean_quote, counts_line, render, scan

SINCE = datetime(2026, 11, 1, tzinfo=UTC)
UNTIL = datetime(2026, 11, 20, tzinfo=UTC)
STOP = "[Request interrupted by user]"
TOOL_USE = "[Request interrupted by user for tool use]"
REJECT = (
    "The user doesn't want to proceed with this tool use. The tool use was rejected. "
    "To tell you how to proceed, the user said:\n"
)
LONG_400 = "stop " + "x" * 395
LONG_401 = LONG_400 + "x"


def _user(uuid: str, minute: int, content: object, **extra: object) -> dict[str, object]:
    stamp = f"2026-11-10T10:{minute:02d}:00Z"
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": stamp,
        "message": {"content": content},
        **extra,
    }


def _blocks(text: str) -> list[dict[str, str]]:
    return [{"type": "text", "text": text}]


def _result(content: object) -> list[dict[str, object]]:
    return [{"type": "tool_result", "tool_use_id": "t1", "content": content}]


A_RECORDS: list[dict[str, object]] = [
    _user("u1", 0, "first prompt please"),
    {"type": "assistant", "uuid": "x1", "timestamp": "2026-11-10T10:00:30Z", "message": {}},
    _user("u2", 1, _blocks(STOP)),
    _user("u3", 2, "no, use the other file"),
    _user("u4", 3, _blocks(STOP)),
    _user("u5", 4, _blocks(STOP)),
    _user("u6", 5, "why did you open that"),
    _user("u8", 7, _result(REJECT + "keep the old name")),
    _user("u9", 8, _result(_blocks(REJECT + "use fish not bash"))),
    _user("u10", 9, LONG_400),
    _user("u11", 10, LONG_401),
    _user("u12", 11, "<command>stop</command>"),
    _user("u13", 12, "you know the drill"),
    _user("u14", 13, "stop that", isMeta=True),
    _user("u15", 14, "stop", isCompactSummary=True),
    _user("u16", 15, _result("plain output")),
    _user("u17", 16, "don't treat Request interrupted as a stop"),
    _user("u18", 17, "stop now", origin={"kind": "task-notification"}),
    _user("u19", 18, "please don't rename it"),
    {"type": "mode", "mode": "auto"},
    _user("u20", 20, _blocks(TOOL_USE)),
]
B_RECORDS: list[dict[str, object]] = [
    *A_RECORDS[:4],
    _user("u30", 30, _blocks(STOP)),
    _user("u31", 31, "wrong branch"),
]
SUB_RECORDS: list[dict[str, object]] = [_user("s1", 0, STOP), _user("s2", 1, "subagent stop")]
C_RECORDS: list[dict[str, object]] = [
    {**_user("c1", 0, "stop it"), "timestamp": "2026-01-01T10:00:00Z"},
    _user("c2", 1, "no, `rm` is wrong\n# heading [x](y)"),
]


def _write(path: Path, records: list[dict[str, object]], tail: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records) + tail, encoding="utf-8")


def _world(root: Path) -> Path:
    projects = root / "projects"
    _write(projects / "p1" / "a.jsonl", A_RECORDS, tail='{"type":"user","mess')
    _write(projects / "p1" / "b.jsonl", B_RECORDS)
    _write(projects / "p1" / "a" / "subagents" / "x.jsonl", SUB_RECORDS)
    _write(projects / "p2" / "c.jsonl", C_RECORDS)
    return projects


def _scan(projects: Path) -> tuple[list[Entry], Coverage]:
    result = scan(projects, SINCE, UNTIL)
    assert not isinstance(result, str), result
    return result


def _quotes(entries: list[Entry], kind: str) -> list[str]:
    return [e.quote for e in entries if e.kind == kind]


class ClassifyTest(unittest.TestCase):
    def test_markers_and_the_candidate_rule(self) -> None:
        # goal: exact markers only; the 400 edge, a leading "<", "no," vs "know"
        self.assertEqual(classify(STOP), "stop")
        self.assertEqual(classify(TOOL_USE), "tool_use")
        self.assertEqual(classify(LONG_400), "correction")
        self.assertIsNone(classify(LONG_401))
        self.assertIsNone(classify("<command>stop</command>"))
        self.assertEqual(classify("no, the other one"), "correction")
        self.assertIsNone(classify("you know the drill"))
        self.assertEqual(classify("don't treat Request interrupted as a stop"), "correction")
        self.assertEqual(classify("please don't rename it"), "maybe")
        self.assertEqual(classify("Do not push yet"), "maybe")
        self.assertEqual(classify("no, don't push"), "correction")  # the strong rule wins
        self.assertIsNone(classify("dont care"))
        self.assertIsNone(classify("see [Request interrupted by user] in the log"))


class ScanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.projects = _world(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_kinds_quotes_and_pairing(self) -> None:
        # goal: follow-ups become the quote, a stop after a stop shares the next real message,
        # follow-ups are not listed again, rejections quote the typed feedback
        entries, _ = _scan(self.projects)
        self.assertEqual(
            sorted(_quotes(entries, "stop")),
            sorted(
                [
                    "no, use the other file",
                    "why did you open that",  # two stops, one follow-up: listed once
                    "wrong branch",
                ]
            ),
        )
        self.assertEqual(
            sorted(_quotes(entries, "tool_use")),
            sorted(["keep the old name", "use fish not bash", "(no follow-up)"]),
        )
        self.assertEqual(
            sorted(_quotes(entries, "correction")),
            sorted(
                [
                    LONG_400,
                    "don't treat Request interrupted as a stop",
                    "no, `rm` is wrong\n# heading [x](y)",
                ]
            ),
        )
        self.assertEqual(_quotes(entries, "maybe"), ["please don't rename it"])

    def test_entry_points_at_the_quoted_message(self) -> None:
        # goal: path:line is the follow-up's line; with no follow-up, the interrupt's own line
        entries, _ = _scan(self.projects)
        by_quote = {e.quote: e for e in entries}
        self.assertEqual(by_quote["no, use the other file"].line, 4)
        self.assertEqual(by_quote["(no follow-up)"].line, len(A_RECORDS))
        self.assertEqual(by_quote["no, use the other file"].project, "p1")
        stop = by_quote["no, use the other file"]
        self.assertEqual(stop.when, datetime(2026, 11, 10, 10, 1, tzinfo=UTC))  # interrupt's time

    def test_dedupe_window_and_subagents(self) -> None:
        # goal: a resumed copy is listed once, subagent files are not read, the window holds
        entries, coverage = _scan(self.projects)
        self.assertEqual(coverage.duplicates, 3)
        self.assertEqual(coverage.files, 3)
        self.assertNotIn("subagent stop", [e.quote for e in entries])
        self.assertNotIn("stop it", [e.quote for e in entries])
        self.assertEqual(coverage.oldest, datetime(2026, 1, 1, 10, 0, tzinfo=UTC))

    def test_newest_first(self) -> None:
        entries, _ = _scan(self.projects)
        stamps = [e.when for e in entries]
        self.assertEqual(stamps, sorted(stamps, reverse=True))

    def test_caps(self) -> None:
        # goal: a capped file is counted as cut, the files cap is an error, the entries cap cuts
        with mock.patch.object(corrections, "LINES_PER_FILE_MAX", 3):
            _, coverage = _scan(self.projects)
            self.assertEqual(coverage.cut, 2)  # a.jsonl and b.jsonl
        with mock.patch.object(corrections, "FILE_BYTES_MAX", 10):
            entries, coverage = _scan(self.projects)
            self.assertEqual((coverage.cut, entries), (3, []))
        with mock.patch.object(corrections, "CORRECTION_FILES_MAX", 2):
            self.assertIsInstance(scan(self.projects, SINCE, UNTIL), str)
        with mock.patch.object(corrections, "CORRECTION_ENTRIES_MAX", 4):
            entries, coverage = _scan(self.projects)
            self.assertEqual((len(entries), coverage.truncated), (4, 6))

    def test_missing_projects_folder(self) -> None:
        self.assertIsInstance(scan(self.projects / "nope", SINCE, UNTIL), str)


class EvalRepeatAndCleanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.projects = Path(self.tmp.name) / "projects"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_eval_file_is_skipped_and_counted(self) -> None:
        # goal: a session run under /tmp lists nothing, like the report's eval-traffic rule
        evals = [{**_user("e1", 0, "wrong file"), "cwd": "/tmp/evprobe-x"}]
        real = [{**_user("r1", 0, "wrong file"), "cwd": "/home/x/dev"}]
        _write(self.projects / "-private-tmp-evprobe-x" / "e.jsonl", evals)
        _write(self.projects / "-home-x-dev" / "r.jsonl", real)
        entries, coverage = _scan(self.projects)
        self.assertEqual([e.project for e in entries], ["-home-x-dev"])
        self.assertEqual((coverage.evals, coverage.files), (1, 2))

    def test_resent_message_is_listed_once(self) -> None:
        # goal: the same words as a stop's follow-up and again as a resent message: one entry
        records = [
            _user("a1", 0, _blocks(STOP)),
            _user("a2", 1, "wrong repo, the other one"),
            _user("a3", 2, "wrong repo,  the other one"),  # resent; folds to the same quote
            _user("a4", 3, _blocks(TOOL_USE)),
        ]
        _write(self.projects / "p" / "a.jsonl", records)
        entries, coverage = _scan(self.projects)
        self.assertEqual([(e.kind, e.line) for e in entries], [("tool_use", 4), ("stop", 2)])
        self.assertEqual(coverage.repeats, 1)

    def test_harness_text_is_cut_from_quotes(self) -> None:
        note = "Note: The user's next message may contain a correction. Pay attention."
        self.assertEqual(clean_quote("keep the name " + note), "keep the name")
        pasted = 'see <pasted_content id="0f7d">\nlog line\n</pasted_content id="0f7d"> now'
        self.assertEqual(clean_quote(pasted), "see [pasted] now")
        self.assertEqual(clean_quote("<pasted_content>cut off log"), "[pasted]")


class RenderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tz = mock.patch.dict(os.environ, {"TZ": "America/New_York"})
        self.tz.start()
        time.tzset()

    def tearDown(self) -> None:
        self.tz.stop()
        time.tzset()

    def test_exact_output(self) -> None:
        # goal: sections in kind order, (none), quote cleaning, coverage last
        when = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
        entry = Entry(when, "p1", Path("/x/a.jsonl"), 7, "stop", "no, `rm`\n#  it")
        coverage = Coverage(files=2, cut=1, duplicates=3, oldest=when)
        text = render([entry], coverage, SINCE, UNTIL)
        self.assertEqual(
            text,
            "# Corrections, 2026-10-31 20:00 to 2026-11-19 19:00 (-0500)\n"
            "\n## stop (1)\n"
            "- 2026-11-10 10:00 -0500 · p1 · `/x/a.jsonl:7`\n"
            "  > no, 'rm' # it\n"
            "\n## tool_use (0)\n(none)\n"
            "\n## correction (0)\n(none)\n"
            "\n## maybe (0)\n(none)\n"
            "\ncoverage: 2 files read, 1 cut, 3 duplicate messages dropped, 0 repeated quotes "
            "dropped, 0 eval files skipped; oldest message 2026-11-10\n",
        )

    def test_truncation_line_and_quote_cut(self) -> None:
        text = render([], Coverage(truncated=6), SINCE, UNTIL)
        self.assertIn("(truncated: 6 older entries)\ncoverage:", text)
        self.assertIn("oldest message none", text)
        self.assertEqual(len(clean_quote("y" * 500)), 200)

    def test_counts_line(self) -> None:
        when = datetime(2026, 11, 10, tzinfo=UTC)
        entry = Entry(when, "p", Path("a"), 1, "correction", "q")
        self.assertEqual(
            counts_line([entry]), "traj corrections: 0 stop, 0 tool_use, 1 correction, 0 maybe"
        )


class CorrectionsCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.projects = _world(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["corrections", *argv])
        return code, out.getvalue()

    def test_writes_one_file_and_prints_counts_only(self) -> None:
        # goal: the only file written is --out (quotes stay there); stdout carries no quote
        target = self.root / "out" / "corrections" / "w.md"
        before = {p for p in self.root.rglob("*")}
        code, stdout = self._run(
            "--projects", str(self.projects), "--since", "2026-11-01", "--out", str(target)
        )
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "traj corrections: 3 stop, 3 tool_use, 3 correction, 1 maybe\n")
        written = {p for p in self.root.rglob("*") if p.is_file()} - before
        self.assertEqual(written, {target})
        self.assertIn("wrong branch", target.read_text(encoding="utf-8"))

    def test_refuses_existing_out_and_missing_projects(self) -> None:
        target = self.root / "w.md"
        target.write_text("old", encoding="utf-8")
        code, stdout = self._run("--projects", str(self.projects), "--out", str(target))
        self.assertEqual((code, target.read_text(encoding="utf-8")), (1, "old"))
        self.assertTrue(stdout.startswith("error: "))
        code, _ = self._run("--projects", str(self.root / "nope"), "--out", str(self.root / "n"))
        self.assertEqual(code, 1)

    def test_argument_validation(self) -> None:
        self.assertIsInstance(parse_corrections_args([]), str)
        self.assertIsInstance(parse_corrections_args(["--out", "a", "--since", "2026-13-01"]), str)
        self.assertIsInstance(parse_corrections_args(["--out", "a", "--fleet"]), str)
        self.assertEqual(parse_corrections_args(["--out", "a"]), {"--out": "a"})


if __name__ == "__main__":
    unittest.main()
