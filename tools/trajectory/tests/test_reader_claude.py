"""Claude Code reader: discovery and skips, turn boundaries, usage, dispatch groups, the status
table, sub-agent files, and the canary (no raw text survives into the events file)."""

from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path

from tests.helpers import CANARY
from traj.limits import BLOCKS_PER_MESSAGE_MAX, FILE_BYTES_MAX, LINES_PER_FILE_MAX
from traj.reader_claude import find_claude_sessions, read_claude_session
from traj.schema import SessionData, SessionRef, Skip, check_session, encode_session

SID = "12345678-1234-4234-8234-123456789abc"
CWD = "/home/someone/dev/project"
Event = dict[str, object]


def stamp(second: int) -> str:
    return f"2026-11-20T10:00:{second:02d}.000Z"


def prompt(second: int, text: object = CANARY, **extra: object) -> Event:
    return {
        "type": "user",
        "uuid": f"u{second}",
        "timestamp": stamp(second),
        "cwd": CWD,
        "version": "9.9.9",
        "message": {"role": "user", "content": text},
        **extra,
    }


def call(
    second: int,
    call_id: str,
    request: str | None = "r1",
    tool: str = "Bash",
    usage: dict[str, int] | None = None,
    stop: str | None = None,
) -> Event:
    line: Event = {
        "type": "assistant",
        "uuid": f"a{second}{call_id}",
        "timestamp": stamp(second),
        "message": {
            "model": "claude-test",
            "stop_reason": stop,
            "usage": usage if usage is not None else {"input_tokens": 10, "output_tokens": 5},
            "content": [
                {"type": "text", "text": CANARY},
                {
                    "type": "tool_use",
                    "id": call_id,
                    "name": tool,
                    "input": {"command": CANARY, "file_path": CANARY},
                },
            ],
        },
    }
    if request is not None:
        line["requestId"] = request
    return line


def result(second: int, call_id: str, is_error: bool = False, **extra: object) -> Event:
    return {
        "type": "user",
        "uuid": f"t{second}{call_id}",
        "timestamp": stamp(second),
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "is_error": is_error,
                    "content": CANARY,
                },
            ]
        },
        **extra,
    }


def write_lines(path: Path, events: Sequence[Event | str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(e if isinstance(e, str) else json.dumps(e) for e in events)
    path.write_text(text + "\n", encoding="utf-8")


def refs_in(root: Path) -> list[SessionRef]:
    refs = find_claude_sessions(root, 100)
    assert isinstance(refs, list), refs
    return refs


def read(
    events: Sequence[Event | str], subs: dict[str, Sequence[Event]] | None = None
) -> SessionData | Skip:
    with tempfile.TemporaryDirectory() as tmp:
        main = Path(tmp) / "-Users-someone-dev-project" / f"{SID}.jsonl"
        write_lines(main, events)
        for name, sub_events in (subs or {}).items():
            write_lines(main.parent / SID / "subagents" / f"{name}.jsonl", sub_events)
        (ref,) = refs_in(Path(tmp))
        if ref.skip_reason:
            return Skip(ref.session_id, "claude", ref.skip_reason)
        return read_claude_session(ref, LINES_PER_FILE_MAX, FILE_BYTES_MAX)


def ok(
    events: Sequence[Event | str], subs: dict[str, Sequence[Event]] | None = None
) -> SessionData:
    data = read(events, subs)
    assert isinstance(data, SessionData), data
    assert check_session(data) is None, check_session(data)
    return data


class DiscoveryTest(unittest.TestCase):
    # goal: a dash-led project directory lists, non-uuid files are ignored, skips carry reasons
    def test_lists_uuid_files_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "-Users-x"
            write_lines(project / f"{SID}.jsonl", [prompt(1)])
            write_lines(project / "notes.jsonl", [prompt(1)])
            refs = refs_in(Path(tmp))
        self.assertEqual([r.session_id for r in refs], [SID])
        self.assertEqual(refs[0].cwds, (CWD,))
        self.assertEqual(refs[0].created_day, "2026-11-20")

    def test_tmp_cwd_is_eval_traffic(self) -> None:
        data = read([prompt(1, cwd="/tmp/run-1")])
        self.assertEqual(data, Skip(SID, "claude", "eval traffic"))

    def test_no_cwd_is_skipped(self) -> None:
        line = prompt(1)
        del line["cwd"]
        self.assertEqual(read([line]), Skip(SID, "claude", "no cwd"))

    def test_over_session_bound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for n in range(3):
                write_lines(Path(tmp) / "-p" / f"{SID[:-1]}{n}.jsonl", [prompt(1)])
            self.assertIsInstance(find_claude_sessions(Path(tmp), 2), str)


class BlockBoundTest(unittest.TestCase):
    # goal: a message over the block bound skips the session as over bound, never cuts it
    def test_assistant_over_bound(self) -> None:
        line = call(2, "c1")
        blocks = [{"type": "text", "text": "x"}] * (BLOCKS_PER_MESSAGE_MAX + 1)
        line["message"] = {"model": "claude-test", "content": blocks}
        self.assertIsInstance(read([prompt(1), line]), Skip)

    def test_user_over_bound(self) -> None:
        line = result(3, "c1")
        block = {"type": "tool_result", "tool_use_id": "c1", "content": "x"}  # not a prompt
        line["message"] = {"content": [block] * (BLOCKS_PER_MESSAGE_MAX + 1)}
        self.assertIsInstance(read([prompt(1), call(2, "c1"), line]), Skip)

    def test_at_bound_reads(self) -> None:
        line = call(2, "c1")
        blocks = [{"type": "text", "text": "x"}] * BLOCKS_PER_MESSAGE_MAX
        line["message"] = {"model": "claude-test", "content": blocks}
        self.assertIsInstance(read([prompt(1), line]), SessionData)


class TurnTest(unittest.TestCase):
    # goal: one depth-0 turn per typed prompt; meta, compact summaries and results are not turns
    def test_prompts_split_turns(self) -> None:
        data = ok([prompt(1), call(2, "c1"), result(3, "c1"), prompt(4), call(5, "c2", "r2")])
        self.assertEqual([t.steps for t in data.turns], [1, 1])
        self.assertEqual(data.steps[1].turn_index, 1)
        self.assertEqual(
            (data.turns[0].started_ms, data.turns[0].ended_ms),
            (1_795_168_801_000, 1_795_168_803_000),
        )

    def test_not_boundaries(self) -> None:
        meta = prompt(2, isMeta=True)
        summary = prompt(3, isCompactSummary=True)
        mixed = prompt(
            4, [{"type": "text", "text": "x"}, {"type": "tool_result", "tool_use_id": "c1"}]
        )
        data = ok([prompt(1), call(2, "c1"), meta, summary, mixed])
        self.assertEqual(len(data.turns), 1)

    def test_text_array_is_boundary(self) -> None:
        data = ok([prompt(1), prompt(2, [{"type": "image"}, {"type": "text", "text": "x"}])])
        self.assertEqual(len(data.turns), 2)

    def test_implicit_first_turn_only_when_used(self) -> None:
        self.assertEqual(len(ok([call(1, "c0"), prompt(2), call(3, "c1")]).turns), 2)
        self.assertEqual(len(ok([prompt(1), call(2, "c1")]).turns), 1)


class UsageTest(unittest.TestCase):
    # goal: tokens count once per request (max over its lines); a conflict is counted
    def test_request_counted_once(self) -> None:
        lines = [
            prompt(1),
            call(2, "c1", "r1", usage={"input_tokens": 10, "output_tokens": 1}),
            call(2, "c2", "r1", usage={"input_tokens": 10, "output_tokens": 7}),
            call(3, "c3", "r2", usage={"input_tokens": 4, "output_tokens": 2}),
        ]
        data = ok(lines)
        turn = data.turns[0]
        self.assertEqual((turn.input_tokens, turn.output_tokens, turn.model_calls), (14, 9, 2))
        self.assertEqual(data.session.usage_conflicts, 1)
        self.assertIsNone(turn.cache_read_tokens)

    def test_no_request_id_adds_no_usage(self) -> None:
        data = ok([prompt(1), call(2, "c1", None)])
        self.assertEqual((data.turns[0].model_calls, data.turns[0].input_tokens), (0, None))
        self.assertIsNone(data.steps[0].dispatch_group)

    def test_parallel_groups(self) -> None:
        data = ok([prompt(1), call(2, "c1", "r1"), call(2, "c2", "r1"), call(3, "c3", "r2")])
        self.assertEqual([s.dispatch_group for s in data.steps], [0, 0, 1])
        self.assertEqual(data.turns[0].parallel_groups, 1)

    def test_stop_reason_is_last_non_null(self) -> None:
        data = ok([prompt(1), call(2, "c1", stop="tool_use"), call(3, "c2", "r2")])
        self.assertEqual(data.turns[0].stop_reason, "tool_use")


class StatusTest(unittest.TestCase):
    # goal: each row of the Claude status table, first match wins
    def status(self, *extra: Event) -> str:
        return ok([prompt(1), call(2, "c1"), *extra]).steps[0].status

    def test_rows(self) -> None:
        reject = {"decision": "reject"}
        self.assertEqual(self.status(result(3, "c1", True, permissionDecision=reject)), "denied")
        self.assertEqual(self.status(), "unpaired")
        cancelled = result(3, "c1", True, toolUseResult={"interrupted": True})
        self.assertEqual(self.status(cancelled), "cancelled")
        self.assertEqual(self.status(result(3, "c1", True, toolUseResult="x")), "error")
        self.assertEqual(self.status(result(3, "c1")), "ok")

    def test_duration_and_unpaired_count(self) -> None:
        data = ok([prompt(1), call(2, "c1"), result(5, "c1"), call(6, "c2", "r2")])
        self.assertEqual(data.steps[0].duration_ms, 3_000)
        self.assertEqual(data.session.unpaired_calls, 1)

    def test_repeated_call_counts_once(self) -> None:
        data = ok([prompt(1), call(2, "c1"), call(2, "c1")])
        self.assertEqual(len(data.steps), 1)


class SubagentTest(unittest.TestCase):
    # goal: each sub-agent file is one depth-1 turn, whatever prompts it holds
    def test_sub_file_is_one_turn(self) -> None:
        sub = [prompt(10), call(11, "s1", "rs"), result(12, "s1"), prompt(13), call(14, "s2", "rt")]
        data = ok([prompt(1), call(2, "c1")], {"agent-abc": sub})
        self.assertEqual([t.depth for t in data.turns], [0, 1])
        self.assertEqual(data.turns[1].steps, 2)
        self.assertEqual(data.turns[1].model_calls, 2)
        self.assertIsNone(data.turns[1].parent_turn_index)


class FormatTest(unittest.TestCase):
    # goal: unknown and malformed lines are counted; over the malformed bound is a skip
    def test_counts(self) -> None:
        data = ok([prompt(1), {"type": "attachment"}, {"type": "brand-new"}, "not json"])
        self.assertEqual((data.session.unknown_events, data.session.parse_errors), (1, 1))

    def test_malformed_gate(self) -> None:
        broken: Event = {"type": "assistant", "timestamp": stamp(2), "message": {}}
        self.assertEqual(read([prompt(1), broken]), Skip(SID, "claude", "unknown format"))


class CanaryTest(unittest.TestCase):
    # goal: no prompt, reply, tool input or result text reaches the events file
    def test_no_raw_text(self) -> None:
        events = [prompt(1), call(2, "c1", tool="Read"), result(3, "c1"), prompt(4)]
        text = "\n".join(encode_session(ok(events, {"agent-a": [prompt(9)]})))
        self.assertNotIn("CANARY", text)
        self.assertNotIn(CWD, text)


if __name__ == "__main__":
    unittest.main()
