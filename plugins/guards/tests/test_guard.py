"""Tests for guard.py. Run: python3 -I plugins/guards/tests/test_guard.py

Goal: every rule guard.py enforces (budget, model ceiling, turn cap) has a case that fails
when the rule breaks. Method: build session folders in a temp dir, pipe hook JSON into the
script as a subprocess, read status and output. GUARD_PATH points the suite at a mutant copy;
`--mutants` reruns the suite once per mutant and expects every run to fail.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import ModuleType

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
GUARD = Path(os.environ.get("GUARD_PATH", str(SCRIPTS / "guard.py")))
HOOKS = HERE.parent / "hooks" / "hooks.json"
MEGABYTE = 1024 * 1024
MILLION = 1_000_000

Record = dict[str, object]


def load_guard() -> ModuleType:
    loader = importlib.machinery.SourceFileLoader("guard_under_test", str(GUARD))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None, GUARD
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the module up while building classes
    loader.exec_module(module)
    return module


# -- fixture records, in the shapes Claude Code writes to its transcripts


def stamp(second: int) -> str:
    return f"2026-11-15T10:{second // 60:02d}:{second % 60:02d}.000Z"


def typed(text: str, second: int = 0) -> Record:
    return {
        "type": "user",
        "origin": {"kind": "human"},
        "timestamp": stamp(second),
        "message": {"role": "user", "content": text},
    }


def typed_image(text: str, second: int = 0) -> Record:
    content: list[Record] = [{"type": "image", "source": {}}, {"type": "text", "text": text}]
    return {
        "type": "user",
        "origin": {"kind": "human"},
        "timestamp": stamp(second),
        "message": {"role": "user", "content": content},
    }


def queued(text: str, second: int = 0) -> Record:
    return {
        "type": "attachment",
        "timestamp": stamp(second),
        "attachment": {"type": "queued_command", "prompt": text, "origin": {"kind": "human"}},
    }


def assistant(
    message_id: str,
    tokens: int = 0,
    second: int = 0,
    model: str = "claude-opus-5-5",
    output: int = 0,
    cache_write: int = 0,
    padding: str = "",
) -> Record:
    usage = {
        "input_tokens": 0,
        "cache_creation_input_tokens": cache_write,
        "cache_read_input_tokens": tokens,
        "output_tokens": output,
    }
    return {
        "type": "assistant",
        "timestamp": stamp(second),
        "message": {
            "id": message_id,
            "model": model,
            "usage": usage,
            "content": [{"type": "text", "text": padding}],
        },
    }


NOT_TYPED: dict[str, Record] = {
    "compaction summary": {
        "type": "user",
        "isCompactSummary": True,
        "timestamp": stamp(0),
        "message": {"content": "budget off"},
    },
    "interrupt": {
        "type": "user",
        "timestamp": stamp(0),
        "message": {"content": [{"type": "text", "text": "[Request interrupted by user]"}]},
    },
    "auto-continuation": {
        "type": "user",
        "isMeta": True,
        "origin": {"kind": "human"},
        "timestamp": stamp(0),
        "message": {"content": "budget off"},
    },
    "command echo": {
        "type": "user",
        "isMeta": True,
        "timestamp": stamp(0),
        "message": {"content": "<command-name>/hooks</command-name>"},
    },
    "task notification": {
        "type": "user",
        "origin": {"kind": "task-notification"},
        "timestamp": stamp(0),
        "message": {"content": "budget off"},
    },
    "peer message": {
        "type": "user",
        "origin": {"kind": "peer"},
        "timestamp": stamp(0),
        "message": {"content": "budget off"},
    },
    "tool result": {
        "type": "user",
        "timestamp": stamp(0),
        "message": {"content": [{"type": "tool_result", "content": "budget off"}]},
    },
}


class Session:
    """A project folder with one main transcript and its subagents/ folder."""

    def __init__(self, root: Path) -> None:
        self.main = root / "project" / "session.jsonl"
        self.subagents = root / "project" / "session" / "subagents"
        self.main.parent.mkdir(parents=True)

    def write_main(self, records: list[Record], tail: bytes = b"") -> None:
        self.main.write_bytes(lines(records) + tail)

    def write_agent(self, agent_id: str, records: list[Record]) -> Path:
        self.subagents.mkdir(parents=True, exist_ok=True)
        path = self.subagents / f"agent-{agent_id}.jsonl"
        path.write_bytes(lines(records))
        return path


def lines(records: list[Record]) -> bytes:
    return b"".join(json.dumps(record).encode() + b"\n" for record in records)


def run(mode: str, hook_input: Record, script: Path | None = None) -> tuple[int, str, str]:
    result = subprocess.run(
        [sys.executable, "-I", str(script or GUARD), mode],
        input=json.dumps(hook_input).encode(),
        capture_output=True,
        timeout=30,
        check=False,
    )
    return result.returncode, result.stdout.decode(), result.stderr.decode()


class GuardCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.session = Session(Path(self.temp.name))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def hook(self, model: str | None = None, agent_id: str | None = None) -> Record:
        tool_input: Record = {"description": "d", "prompt": "p", "subagent_type": "x"}
        if model is not None:
            tool_input["model"] = model
        hook_input: Record = {"transcript_path": str(self.session.main), "tool_input": tool_input}
        if agent_id is not None:
            hook_input["agent_id"] = agent_id
        return hook_input

    def assert_allowed(self, mode: str, hook_input: Record) -> str:
        status, stdout, stderr = run(mode, hook_input)
        self.assertEqual((status, stderr), (0, ""), stdout)
        return stdout

    def assert_refused(self, mode: str, hook_input: Record, needle: str) -> str:
        status, stdout, stderr = run(mode, hook_input)
        self.assertEqual(status, 2, stdout)
        self.assertIn(needle, stderr)
        self.assertTrue(stderr.startswith("guard: "), stderr)
        return stderr


class TypedMessages(GuardCase):
    """Shared by agent and turn modes: which transcript lines count as the user typing."""

    def test_typed_shapes(self) -> None:
        guard = load_guard()
        self.assertEqual(guard.typed_text(typed("hi\nthere")), "hi\nthere")
        self.assertEqual(guard.typed_text(typed_image("look")), "look")
        self.assertEqual(guard.typed_text(queued("queued one")), "queued one")

    def test_not_typed_shapes(self) -> None:
        guard = load_guard()
        for name, record in NOT_TYPED.items():
            with self.subTest(name):
                self.assertIsNone(guard.typed_text(record))

    def test_first_line_only(self) -> None:
        guard = load_guard()
        self.assertEqual(guard.first_line("  budget $5 \nbudget off"), "budget $5")
        self.assertEqual(guard.first_line("hello\nbudget $5"), "hello")


class TurnGuard(GuardCase):
    def test_subagent_call_allowed_without_reading(self) -> None:
        # no transcript exists at all: a read would refuse
        self.assertEqual(self.assert_allowed("turn", self.hook(agent_id="abc")), "")

    def test_subagent_call_over_cap_allowed(self) -> None:
        self.session.write_main([typed("go"), assistant("a", 11 * MILLION)])
        self.assert_allowed("turn", self.hook(agent_id="abc"))

    def test_under_cap_silent(self) -> None:
        self.session.write_main([typed("go"), assistant("a", 9 * MILLION)])
        self.assertEqual(self.assert_allowed("turn", self.hook()), "")

    def test_over_cap_refused(self) -> None:
        self.session.write_main([typed("go"), assistant("a", 10 * MILLION + 1)])
        self.assert_refused("turn", self.hook(), "turn cap: 10.0M of 10M tokens this turn")

    def test_previous_turn_not_counted(self) -> None:
        self.session.write_main(
            [typed("one"), assistant("a", 9 * MILLION), typed("two"), assistant("b", 9 * MILLION)]
        )
        self.assert_allowed("turn", self.hook())

    def test_notification_turn_rolls_into_typed_turn(self) -> None:
        self.session.write_main(
            [
                typed("go"),
                assistant("a", 6 * MILLION),
                NOT_TYPED["task notification"],
                assistant("b", 6 * MILLION),
            ]
        )
        self.assert_refused("turn", self.hook(), "turn cap: 12.0M")

    def test_queued_message_starts_turn(self) -> None:
        self.session.write_main(
            [typed("go"), assistant("a", 9 * MILLION), queued("more"), assistant("b", 9 * MILLION)]
        )
        self.assert_allowed("turn", self.hook())

    def test_image_message_starts_turn(self) -> None:
        self.session.write_main(
            [
                typed("go"),
                assistant("a", 9 * MILLION),
                typed_image("see"),
                assistant("b", 9 * MILLION),
            ]
        )
        self.assert_allowed("turn", self.hook())

    def test_raised_cap_carries_to_later_turns(self) -> None:
        self.session.write_main(
            [typed("turn cap 20M"), assistant("a", 1), typed("next"), assistant("b", 15 * MILLION)]
        )
        self.assert_allowed("turn", self.hook())
        self.session.write_main(
            [typed("turn cap 20M"), typed("next"), assistant("b", 21 * MILLION)]
        )
        self.assert_refused("turn", self.hook(), "of 20M tokens")

    def test_off_only_for_its_own_turn(self) -> None:
        self.session.write_main([typed("Turn Cap Off"), assistant("a", 50 * MILLION)])
        self.assert_allowed("turn", self.hook())
        self.session.write_main(
            [typed("turn cap off"), typed("next"), assistant("b", 11 * MILLION)]
        )
        self.assert_refused("turn", self.hook(), "of 10M tokens")

    def test_ignored_cap_lines(self) -> None:
        for text in ["hello\nturn cap 50M", "turn cap 0M", "turn cap 101M", "turn cap 50"]:
            with self.subTest(text):
                self.session.write_main([typed(text), assistant("a", 11 * MILLION)])
                self.assert_refused("turn", self.hook(), "of 10M tokens")
        tool_result = NOT_TYPED["tool result"]
        tool_result["message"] = {"content": [{"type": "tool_result", "content": "turn cap 50M"}]}
        self.session.write_main([typed("go"), tool_result, assistant("a", 11 * MILLION)])
        self.assert_refused("turn", self.hook(), "of 10M tokens")

    def test_repeated_id_counted_once_at_largest(self) -> None:
        self.session.write_main(
            [typed("go"), assistant("a", 6 * MILLION), assistant("a", 6 * MILLION)]
        )
        self.assert_allowed("turn", self.hook())
        self.session.write_main(
            [
                typed("go"),
                assistant("a", 9 * MILLION, output=3),
                assistant("a", 9 * MILLION, output=2 * MILLION),
            ]
        )
        self.assert_refused("turn", self.hook(), "turn cap: 11.0M")

    def test_breakdown_without_total_counted(self) -> None:
        # real shape (2425c6c3): total 0 while the 1h breakdown holds the tokens
        record = assistant("a", 0)
        usage = {
            "input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 10 * MILLION,
            "output_tokens": 0,
            "cache_creation": {"ephemeral_1h_input_tokens": 11523, "ephemeral_5m_input_tokens": 0},
        }
        record["message"] = {"id": "a", "model": "claude-opus-5-5", "usage": usage}
        self.session.write_main([typed("go"), record])
        self.assert_refused("turn", self.hook(), "turn cap: 10.0M")

    def test_synthetic_skipped(self) -> None:
        self.session.write_main([typed("go"), assistant("s", 11 * MILLION, model="<synthetic>")])
        self.assert_allowed("turn", self.hook())

    def test_no_typed_message_counts_from_start(self) -> None:
        self.session.write_main([assistant("a", 6 * MILLION), assistant("b", 6 * MILLION)])
        self.assert_refused("turn", self.hook(), "turn cap: 12.0M")

    def test_half_written_last_line_skipped(self) -> None:
        self.session.write_main([typed("go"), assistant("a", 1)], tail=b'{"type": "assis')
        self.assert_allowed("turn", self.hook())

    def test_broken_middle_line_skips_cap(self) -> None:
        # a crash in turn mode warns and allows: a broken cap must not lock every tool call
        self.session.main.write_bytes(
            lines([typed("go")]) + b'{"type": "assistant" x\n' + lines([assistant("a", 1)])
        )
        status, stdout, stderr = run("turn", self.hook())
        self.assertEqual(status, 0, stdout)
        self.assertTrue(stderr.startswith("guard: turn cap skipped, internal error"), stderr)

    def test_line_split_across_block_boundary(self) -> None:
        # pad so the typed line straddles the 1 MB cut, with a 3-byte character at the cut
        tail = lines([assistant("a", 11 * MILLION)])
        head = lines([typed("go")])
        cap_record = typed("turn cap 20M\n" + "€" * (MEGABYTE // 3))
        middle = lines([cap_record])
        pad_bytes = MEGABYTE - len(tail) - len(middle) // 2
        filler = lines([assistant("f", 1, padding="x" * pad_bytes)])
        self.session.main.write_bytes(head + middle + filler + tail)
        self.assertGreater(self.session.main.stat().st_size, MEGABYTE)
        self.assert_allowed("turn", self.hook())

    def test_missing_transcript_refused(self) -> None:
        self.assert_refused("turn", self.hook(), "transcript missing")

    def test_main_over_limit_refused(self) -> None:
        with self.session.main.open("wb") as handle:
            handle.truncate(128 * MEGABYTE + 1)  # sparse: no disk used
        self.assert_refused("turn", self.hook(), "over the 128 MB limit")

    def test_whole_file_one_turn_at_limit_well_under_hook_timeout(self) -> None:
        # the turn hook times out at 5 s; 3 s leaves room for a slow disk (1.8 s seen under load)
        write_big_main(self.session.main, [typed("go")], 128 * MEGABYTE)
        started = time.monotonic()
        self.assert_allowed("turn", self.hook())
        self.assertLess(time.monotonic() - started, 3.0)


def write_big_main(path: Path, head: list[Record], size_bytes: int) -> None:
    """Head records, then assistant lines padded to exactly size_bytes."""
    data = bytearray(lines(head))
    line_bytes = 16 * 1024
    index = 0
    while len(data) + 2 * line_bytes < size_bytes:
        data += padded(f"m{index}", line_bytes)
        index += 1
    data += padded("last", size_bytes - len(data))
    assert len(data) == size_bytes, (len(data), size_bytes)
    path.write_bytes(bytes(data))


def padded(message_id: str, line_bytes: int) -> bytes:
    """One assistant line of exactly line_bytes, newline included."""
    bare = len(json.dumps(assistant(message_id, 10)).encode()) + 1
    assert line_bytes >= bare, (line_bytes, bare)
    raw = json.dumps(assistant(message_id, 10, padding="y" * (line_bytes - bare))).encode()
    return raw + b"\n"


HAIKU_LAUNCH_FLOOR_USD = 60_000 * 0.125 / MILLION  # 0.0075


class AgentGuard(GuardCase):
    def setUp(self) -> None:
        super().setUp()
        self.session.write_main([typed("hi"), assistant("m0", 10)])

    def test_alias_above_ceiling_refused(self) -> None:
        self.assert_refused("agent", self.hook("fable"), "above the ceiling claude-opus-5-5")

    def test_one_rate_above_ceiling_refused(self) -> None:
        self.assert_refused("agent", self.hook("claude-opus-5"), "above the ceiling")
        # a model cheaper than the ceiling on four rates and dearer on one
        copy = Path(self.temp.name) / "guard.py"
        copy.write_bytes(GUARD.read_bytes())
        prices = json.loads((SCRIPTS / "prices.json").read_text())
        prices["models"]["claude-sonnet-x"] = {"in": 1, "cw5m": 1, "cw1h": 1, "cr": 0.3, "out": 1}
        (copy.parent / "prices.json").write_text(json.dumps(prices))
        status, _, stderr = run("agent", self.hook("claude-sonnet-x"), copy)
        self.assertEqual(status, 2)
        self.assertIn("(cr 0.3 > 0.2 $/M)", stderr)

    def test_unknown_family_refused(self) -> None:
        self.assert_refused(
            "agent", self.hook("claude-fable-5-2"), "unknown model claude-fable-5-2"
        )
        self.assert_refused("agent", self.hook("gpt-9"), "unknown model gpt-9")

    def test_new_opus_id_priced_by_family(self) -> None:
        stdout = self.assert_allowed("agent", self.hook("claude-opus-9"))
        self.assertIn("model claude-opus-9 (unpriced, family rates)", stdout)

    def test_ceiling_and_cheaper_allowed(self) -> None:
        for model in ["opus", "claude-opus-5-5", "sonnet", "haiku", "claude-haiku-4-5-20251001"]:
            with self.subTest(model):
                self.assertIn("no budget line", self.assert_allowed("agent", self.hook(model)))

    def test_no_model_inherits_main(self) -> None:
        self.session.write_main(
            [
                typed("hi"),
                assistant("m0", 10, model="claude-sonnet-5"),
                assistant("s", 0, model="<synthetic>"),
            ]
        )
        self.assertIn("model claude-sonnet-5", self.assert_allowed("agent", self.hook()))

    def test_no_model_with_agent_id_inherits_that_agent(self) -> None:
        self.session.write_agent("a1", [assistant("x", 1, model="claude-haiku-5-5")])
        self.assertIn(
            "model claude-haiku-5-5", self.assert_allowed("agent", self.hook(agent_id="a1"))
        )

    def test_inherited_above_ceiling_refused(self) -> None:
        self.session.write_main([typed("hi"), assistant("m0", 10, model="claude-fable-5-1")])
        self.assert_refused("agent", self.hook(), "model claude-fable-5-1 is above the ceiling")

    def budget_session(self, budget: str, agent_output: int) -> None:
        # haiku output is $0.50/M, so agent_output tokens cost agent_output * 0.5e-6 dollars
        self.session.write_main(
            [
                typed("hi", 0),
                assistant("m0", 10),
                typed(budget, 10),
                assistant("m1", 50 * MILLION, 11),
            ]
        )
        self.session.write_agent("a1", [assistant("x", 0, 20, "claude-haiku-5-5", agent_output)])

    def test_budget_under_allowed(self) -> None:
        self.budget_session("budget $1", 1 * MILLION)  # $0.50 + floor
        stdout = self.assert_allowed("agent", self.hook("haiku"))
        self.assertIn("$0.50 of $1.00 since budget line, 1 files, 1 messages, 0 unpriced", stdout)

    def test_spend_plus_floor_over_refused(self) -> None:
        # $0.995 spent: alone under $1, plus the $0.0075 floor over it
        self.budget_session("budget 1", 1_990_000)
        self.assert_refused("agent", self.hook("haiku"), "spent $0.99 of $1.00")

    def test_main_transcript_spend_not_counted(self) -> None:
        self.budget_session("budget $0.10", 0)  # main has 50M opus cache reads = $10
        self.assert_allowed("agent", self.hook("haiku"))

    def test_nested_agent_files_counted(self) -> None:
        self.budget_session("budget $1", 1 * MILLION)
        self.session.write_agent("a2", [assistant("y", 0, 30, "claude-haiku-5-5", 1 * MILLION)])
        self.assert_refused("agent", self.hook("haiku"), "spent $1.00 of $1.00")

    def test_rhino_transcripts_counted(self) -> None:
        # goal: headless rhinos copied into <session>/rhinos/ count toward the budget
        self.budget_session("budget $1", 1 * MILLION)  # $0.50 from the sub-agent
        rhinos = self.session.subagents.parent / "rhinos"
        rhinos.mkdir()
        (rhinos / "rhino-r1.jsonl").write_bytes(
            lines([assistant("r", 0, 30, "claude-haiku-5-5", 1 * MILLION)])
        )
        self.assert_refused("agent", self.hook("haiku"), "spent $1.00 of $1.00")

    def test_spend_before_budget_line_not_counted(self) -> None:
        self.budget_session("budget $1", 0)
        self.session.write_agent("old", [assistant("z", 0, 5, "claude-haiku-5-5", 10 * MILLION)])
        self.assert_allowed("agent", self.hook("haiku"))

    def test_ignored_budget_lines(self) -> None:
        for record in [
            NOT_TYPED["tool result"],
            NOT_TYPED["peer message"],
            typed("note\nbudget $0.01", 10),
            typed("budget $0", 10),
        ]:
            with self.subTest(json.dumps(record)[:60]):
                self.session.write_main([typed("hi"), assistant("m0", 10), record])
                self.session.write_agent("a1", [assistant("x", 0, 20, "claude-haiku-5-5", 10)])
                self.assertIn("no budget line", self.assert_allowed("agent", self.hook("haiku")))

    def test_queued_budget_line_counts(self) -> None:
        self.session.write_main([typed("hi"), assistant("m0", 10), queued("budget $0.01", 10)])
        self.session.write_agent("a1", [assistant("x", 0, 20, "claude-haiku-5-5", 1 * MILLION)])
        self.assert_refused("agent", self.hook("haiku"), "of $0.01")

    def test_budget_off_allowed_with_note(self) -> None:
        self.budget_session("budget $0.01", 10 * MILLION)
        self.session.write_main([typed("budget $0.01"), typed("BUDGET OFF", 30)])
        self.assertIn("budget off", self.assert_allowed("agent", self.hook("haiku")))
        self.assert_refused("agent", self.hook("fable"), "above the ceiling")

    def test_growing_output_priced_at_largest(self) -> None:
        self.budget_session("budget $1", 3)
        self.session.write_agent(
            "a1",
            [
                assistant("x", 0, 20, "claude-haiku-5-5", 3),
                assistant("x", 0, 21, "claude-haiku-5-5", 1_990_000),
            ],
        )
        self.assert_refused("agent", self.hook("haiku"), "spent $0.99 of $1.00")

    def test_synthetic_and_unknown_transcript_models(self) -> None:
        self.budget_session("budget $5", 0)
        self.session.write_agent(
            "a1",
            [
                assistant("s", 0, 20, "<synthetic>", 100 * MILLION),
                assistant("u", 0, 21, "claude-mystery-1", 1 * MILLION),
            ],
        )  # priced as opus: $20
        self.assert_refused("agent", self.hook("haiku"), "spent $20.00 of $5.00")
        self.session.write_agent("a1", [assistant("u", 0, 21, "claude-mystery-1", 10)])
        self.assertIn("1 unpriced", self.assert_allowed("agent", self.hook("haiku")))

    def test_request_blob_lines_ignored(self) -> None:
        # real shape: no timestamp, and the word "assistant" inside
        self.budget_session("budget $1", 10)
        blob: Record = {"type": "api-request-blob", "messages": [{"role": "assistant"}]}
        path = self.session.subagents / "agent-a1.jsonl"
        path.write_bytes(path.read_bytes() + lines([blob]) + lines([assistant("y", 0, 25)]))
        self.assert_allowed("agent", self.hook("haiku"))

    def test_no_subagent_files_is_zero(self) -> None:
        self.session.write_main([typed("budget $1")])
        self.assertIn("$0.00 of $1.00", self.assert_allowed("agent", self.hook("haiku")))

    def test_transcript_inside_subagents_refused(self) -> None:
        path = self.session.write_agent("a1", [assistant("x", 1)])
        hook_input = self.hook("haiku")
        hook_input["transcript_path"] = str(path)
        self.assert_refused("agent", hook_input, "transcript_path is a sub-agent's")

    def test_missing_or_empty_main_refused(self) -> None:
        self.session.main.write_bytes(b"")
        self.assert_refused("agent", self.hook("haiku"), "transcript empty")
        self.session.main.unlink()
        self.assert_refused("agent", self.hook("haiku"), "transcript missing")

    def test_malformed_prices_refused(self) -> None:
        copy = Path(self.temp.name) / "guard.py"
        copy.write_bytes(GUARD.read_bytes())
        (copy.parent / "prices.json").write_text('{"ceiling": "x"')
        status, _, stderr = run("agent", self.hook("haiku"), copy)
        self.assertEqual(status, 2)
        self.assertIn("prices.json unreadable", stderr)

    def test_non_string_alias_refused(self) -> None:
        copy = Path(self.temp.name) / "guard.py"
        copy.write_bytes(GUARD.read_bytes())
        prices = json.loads((SCRIPTS / "prices.json").read_text())
        prices["aliases"]["haiku"] = 4
        (copy.parent / "prices.json").write_text(json.dumps(prices))
        status, _, stderr = run("agent", self.hook("haiku"), copy)
        self.assertEqual(status, 2)
        self.assertIn("aliases holds a non-string entry", stderr)

    def test_unknown_mode_refused(self) -> None:
        self.assert_refused("bogus", self.hook("haiku"), "unknown mode 'bogus'")

    def test_size_limits_refused(self) -> None:
        with self.session.main.open("r+b") as handle:
            handle.truncate(128 * MEGABYTE + 1)
        self.assert_refused("agent", self.hook("haiku"), "over the 128 MB limit")
        self.session.write_main([typed("budget $1")])
        for index in range(2):
            path = self.session.write_agent(f"big{index}", [])
            with path.open("wb") as handle:
                handle.truncate(32 * MEGABYTE + 1)
        self.assert_refused("agent", self.hook("haiku"), "over 64 MB")
        for path in self.session.subagents.iterdir():
            path.unlink()
        for index in range(65):
            self.session.write_agent(f"n{index}", [])
        self.assert_refused("agent", self.hook("haiku"), "65 sub-agent transcripts")

    def test_at_limits_under_three_seconds(self) -> None:
        write_big_main(self.session.main, [typed("budget $1000")], 128 * MEGABYTE)
        for index in range(64):
            path = self.session.subagents / f"agent-{index}.jsonl"
            self.session.subagents.mkdir(parents=True, exist_ok=True)
            write_big_main(path, [], MEGABYTE)
        started = time.monotonic()
        stdout = self.assert_allowed("agent", self.hook())
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertIn("64 files", stdout)


def reply(text: str, second: int) -> Record:
    return {
        "type": "assistant",
        "timestamp": stamp(second),
        "message": {
            "id": f"r{second}",
            "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": text}],
        },
    }


def thinking(second: int) -> Record:
    return {
        "type": "assistant",
        "timestamp": stamp(second),
        "message": {"id": f"t{second}", "content": [{"type": "thinking", "thinking": "x"}]},
    }


class ApprovedProposal(GuardCase):
    """Approved proposal: an agent's `proposed budget $N` plus the user's typed yes."""

    def proposal_session(self, records: list[Record], agent_output: int) -> None:
        # haiku output is $0.50/M; the agent file is written after every main line
        self.session.write_main([typed("hi", 0), assistant("m0", 10)] + records)
        self.session.write_agent("a1", [assistant("x", 0, 50, "claude-haiku-5-5", agent_output)])

    def test_yes_approves_proposal(self) -> None:
        # the last two are real replies that a first word list missed
        for word in [
            "yes",
            "Go on.",
            "GO",
            "approve!",
            "do it",
            "Sure I like that. Please go on.",
            "I approve, keep going.",
        ]:
            with self.subTest(word):
                self.proposal_session(
                    [
                        reply("worst case $0.80\n`proposed budget $1`\n", 20),
                        thinking(21),
                        typed(word, 30),
                    ],
                    1 * MILLION,
                )
                stdout = self.assert_allowed("agent", self.hook("haiku"))
                self.assertIn("$0.50 of $1.00 since first approved proposal", stdout)

    def test_approved_proposal_enforced(self) -> None:
        self.proposal_session([reply("proposed budget $1", 20), typed("yes", 30)], 1_990_000)
        self.assert_refused("agent", self.hook("haiku"), "spent $0.99 of $1.00")

    def test_spend_counted_from_approval(self) -> None:
        self.proposal_session([reply("proposed budget $1", 20), typed("yes", 30)], 0)
        self.session.write_agent("old", [assistant("z", 0, 25, "claude-haiku-5-5", 10 * MILLION)])
        self.assert_allowed("agent", self.hook("haiku"))

    def test_tool_lines_between_still_pair(self) -> None:
        tool_call: Record = {
            "type": "assistant",
            "timestamp": stamp(22),
            "message": {
                "id": "c22",
                "content": [{"type": "tool_use", "name": "Bash", "input": {}}],
            },
        }
        self.proposal_session([reply("proposed budget $1", 20), tool_call, queued("go", 30)], 0)
        self.assertIn("of $1.00", self.assert_allowed("agent", self.hook("haiku")))

    def test_text_blocks_joined(self) -> None:
        two: Record = reply("intro", 20)
        content = [{"type": "text", "text": "proposed budget $1"}, {"type": "text", "text": "x"}]
        two["message"] = {"id": "r20", "content": content}
        self.proposal_session([two, typed("yes", 30)], 0)
        self.assertIn("no budget line", self.assert_allowed("agent", self.hook("haiku")))
        content.reverse()
        self.proposal_session([two, typed("yes", 30)], 0)
        self.assertIn("of $1.00", self.assert_allowed("agent", self.hook("haiku")))

    def test_not_approved(self) -> None:
        cases: dict[str, list[Record]] = {
            "over max": [reply("proposed budget $20.01", 20), typed("yes", 30)],
            "zero": [reply("proposed budget $0", 20), typed("yes", 30)],
            "not the last line": [reply("proposed budget $1\nthoughts?", 20), typed("yes", 30)],
            "older reply only": [
                reply("proposed budget $1", 20),
                typed("what?", 25),
                reply("sure", 26),
                typed("yes", 30),
            ],
            "later reply in the same turn": [
                reply("proposed budget $1", 20),
                thinking(21),
                reply("actually, more work first", 22),
                typed("yes", 30),
            ],
            "typed message between": [
                reply("proposed budget $1", 20),
                typed("hmm", 25),
                typed("yes", 30),
            ],
            "yes but": [reply("proposed budget $1", 20), typed("yes but", 30)],
            "second line": [
                reply("proposed budget $1", 20),
                typed("yes\nalso fix the docs after", 30),
            ],
            "question": [reply("proposed budget $1", 20), typed("go?", 30)],
            "negated": [reply("proposed budget $1", 20), typed("don't go yet", 30)],
            "own number": [reply("proposed budget $1", 20), typed("ok, $5", 30)],
            "too long": [reply("proposed budget $1", 20), typed("yes " + "x" * 120, 30)],
            "no approval word": [reply("proposed budget $1", 20), typed("going home", 30)],
            "y is not a word": [reply("proposed budget $1", 20), typed("y", 30)],
            "user wrote the proposal": [typed("proposed budget $1", 20), typed("yes", 30)],
            "proposal mid-line": [reply("say proposed budget $1 ok", 20), typed("yes", 30)],
            "no approval": [reply("proposed budget $1", 20)],
        }
        for name, records in cases.items():
            with self.subTest(name):
                self.proposal_session(records, 10)
                self.assertIn("no budget line", self.assert_allowed("agent", self.hook("haiku")))

    def test_over_max_keeps_older_approval(self) -> None:
        self.proposal_session(
            [
                reply("proposed budget $1", 20),
                typed("yes", 30),
                reply("proposed budget $50", 40),
                typed("yes", 45),
            ],
            1_990_000,
        )
        self.assert_refused("agent", self.hook("haiku"), "of $1.00")

    def test_max_is_approvable(self) -> None:
        self.proposal_session([reply("proposed budget $20", 20), typed("ok", 30)], 0)
        self.assertIn("of $20.00", self.assert_allowed("agent", self.hook("haiku")))

    def test_typed_line_beats_proposals_in_any_order(self) -> None:
        # an agent-written number can never raise what the user typed
        self.proposal_session(
            [typed("budget $1", 15), reply("proposed budget $20", 20), typed("yes", 30)], 1_990_000
        )
        self.assert_refused("agent", self.hook("haiku"), "1 approved proposal(s) ignored")
        self.proposal_session(
            [reply("proposed budget $1", 20), typed("yes", 30), typed("budget $5", 40)], 1_990_000
        )
        allowed = self.assert_allowed("agent", self.hook("haiku"))
        self.assertIn("of $5.00", allowed)
        self.assertNotIn("ignored", allowed)  # an approval older than the typed line isn't news
        self.proposal_session(
            [typed("budget off", 15), reply("proposed budget $1", 20), typed("yes", 30)], 1_990_000
        )
        self.assertIn("budget off", self.assert_allowed("agent", self.hook("haiku")))

    def test_approved_amounts_add_up(self) -> None:
        # a real case: $3 then $2 approved means $5, all counted from the first approval
        self.proposal_session(
            [
                reply("proposed budget $1", 20),
                typed("yes", 30),
                reply("refused\nproposed budget $3", 40),
                typed("go", 45),
            ],
            10,
        )
        self.session.write_agent("early", [assistant("e", 0, 35, "claude-haiku-5-5", 7 * MILLION)])
        self.assertIn(
            "$3.50 of $4.00 since first approved proposal",
            self.assert_allowed("agent", self.hook("haiku")),
        )

    def test_approvals_capped_per_session(self) -> None:
        # a run of "go" replies never lets more than $20 through; spend from the oldest one
        self.proposal_session(
            [
                reply("proposed budget $15", 20),
                typed("yes", 30),
                reply("proposed budget $15", 40),
                typed("go", 45),
            ],
            10,
        )
        spend = [assistant("e", 0, 35, "claude-haiku-5-5", 40 * MILLION)]
        self.session.write_agent("early", spend)
        self.assert_refused("agent", self.hook("haiku"), "spent $20.00 of $20.00")


class HookCommands(GuardCase):
    """The hooks.json wrapper must turn a missing script into exit 2, not let it through."""

    def commands(self) -> list[str]:
        settings = json.loads(HOOKS.read_text())
        found: list[str] = []
        for group in settings["hooks"]["PreToolUse"]:
            for hook in group["hooks"]:
                found.append(hook["command"])
        self.assertEqual(len(found), 2)
        return [
            command.replace("${CLAUDE_PLUGIN_ROOT}/scripts/guard.py", "/nonexistent/guard.py")
            for command in found
        ]

    def test_missing_script_exits_2(self) -> None:
        for command in self.commands():
            with self.subTest(command):
                self.assertIn("/nonexistent/guard.py", command)
                if os.environ.get("GUARD_MUTANT") == "exit-wrap":
                    command = command.replace(" || exit 2", "")
                status = subprocess.run(
                    ["sh", "-c", command], input=b"{}", capture_output=True, timeout=30, check=False
                ).returncode
                self.assertEqual(status, 2)


# -- mutants: exact strings in guard.py; a replacement that matches nothing fails the run

MUTANTS: dict[str, tuple[str, str]] = {
    "dedup-max": (
        "seen.output_tokens = max(seen.output_tokens, usage.output_tokens)",
        "seen.output_tokens = seen.output_tokens",
    ),
    "budget-cmp": (
        "if spend.dollars + floor > budget.dollars:",
        "if spend.dollars + floor < budget.dollars:",
    ),
    "human-only": ('if field(record, "origin", "kind") != "human":', "if False:"),
    "first-line": (
        'return text.split("\\n", 1)[0].strip()',
        'return text.strip().split("\\n")[-1].strip()',
    ),
    "turn-cmp": ("if tokens > cap:", "if tokens < cap:"),
    "subagent-skip": (
        'if hook_input.get("agent_id") is not None:\n        return ""',
        'if False:\n        return ""',
    ),
    "proposal-max": ("if not 0 < dollars <= PROPOSAL_DOLLARS_MAX:", "if not 0 < dollars:"),
    "proposal-zero": (
        "if not 0 < dollars <= PROPOSAL_DOLLARS_MAX:",
        "if not 0 <= dollars <= PROPOSAL_DOLLARS_MAX:",
    ),
    "proposal-nearest": ("pending = None  # only the nearest agent text", "pass  #"),
    "proposal-pair": ("pending = None  # a typed message between", "pass  #"),
    "approval-veto": ("    if VETO_PATTERN.search(reply):\n        return False\n", ""),
    "approval-one-line": (
        'if "\\n" in reply or len(reply) > APPROVAL_CHARS_MAX:',
        "if len(reply) > APPROVAL_CHARS_MAX:",
    ),
    "proposal-last-line": (
        "PROPOSAL_PATTERN.fullmatch(lines[-1])",
        "PROPOSAL_PATTERN.fullmatch(lines[0])",
    ),
    "proposal-since-oldest": (
        "newest, oldest = approvals[0], approvals[-1]",
        "newest, oldest = approvals[0], approvals[0]",
    ),
    "approval-cap": (
        "min(sum(approval.dollars for approval in approvals), PROPOSAL_DOLLARS_MAX)",
        "sum(approval.dollars for approval in approvals)",
    ),
    "approval-sum": (
        "min(sum(approval.dollars for approval in approvals), PROPOSAL_DOLLARS_MAX)",
        "min(approvals[0].dollars, PROPOSAL_DOLLARS_MAX)",
    ),
    "typed-wins": (
        "        return dataclasses.replace(typed_budget, ignored=len(approvals))\n",
        "        return dataclasses.replace(typed_budget, ignored=0) if not approvals else None\n",
    ),
    "ignored-silent": ("    if budget.ignored:", "    if False:"),
    "turn-crash-open": (
        'if mode == "turn":  # a broken turn cap',
        "if False:  # a broken turn cap",
    ),
    "exit-wrap": ("", ""),  # applied to the hook command inside HookCommands
}


def run_mutants() -> int:
    source = (SCRIPTS / "guard.py").read_text()
    survivors: list[str] = []
    for name, (original, replacement) in MUTANTS.items():
        if original and source.count(original) != 1:
            print(f"mutant {name}: original string found {source.count(original)} times")
            return 1
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory) / "guard.py"
            copy.write_text(source.replace(original, replacement) if original else source)
            (copy.parent / "prices.json").write_bytes((SCRIPTS / "prices.json").read_bytes())
            env = {**os.environ, "GUARD_PATH": str(copy), "GUARD_MUTANT": name}
            result = subprocess.run(
                [sys.executable, "-I", __file__],
                env=env,
                capture_output=True,
                timeout=600,
                check=False,
            )
            killed = result.returncode != 0
            print(f"mutant {name}: {'killed' if killed else 'SURVIVED'}")
            if not killed:
                survivors.append(name)
    print(f"mutants: {len(MUTANTS) - len(survivors)}/{len(MUTANTS)} killed")
    return 1 if survivors else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--mutants"]:
        sys.exit(run_mutants())
    unittest.main()
