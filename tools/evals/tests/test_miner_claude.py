"""Goal: `eval mine --harness claude` counts Claude Code transcripts the way the Kiro miner counts
Kiro logs: tool calls under the cases' tool names, pairing and errors, skill loads and SKILL.md
reads, sub-agent files folded in, eval traffic skipped, and no message text in the report.
Method: write small transcripts into a throwaway projects folder and mine it."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lib.miner import (
    MINER_PARTS_PER_MESSAGE_MAX,
    OTHER,
    SKIP_CLAUDE_NO_CWD,
    SKIP_EVAL,
    SessionFacts,
    find_claude_sessions,
    mine_session,
    run_mine,
)

CANARY = "SECRET-0xFEEDFACE"
SID = "12345678-1234-4234-8234-123456789abc"
SKILLS = frozenset({"tiger-style", "ponytail"})
Line = dict[str, object]


def prompt(cwd: str = "/home/someone/dev/p") -> Line:
    return {
        "type": "user",
        "cwd": cwd,
        "timestamp": "2026-11-20T10:00:00.000Z",
        "message": {"role": "user", "content": CANARY},
    }


def use(call_id: str, name: str, tool_input: dict[str, object]) -> Line:
    block = {"type": "tool_use", "id": call_id, "name": name, "input": tool_input}
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": CANARY}, block]}}


def done(call_id: str, is_error: bool = False) -> Line:
    block = {"type": "tool_result", "tool_use_id": call_id, "is_error": is_error, "content": CANARY}
    return {"type": "user", "message": {"content": [block]}}


def write(path: Path, lines: list[Line]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")


def store(tmp: str, lines: list[Line], name: str = SID) -> Path:
    projects = Path(tmp) / "projects"
    write(projects / "-Users-someone-dev-p" / f"{name}.jsonl", lines)
    return projects


def mine(lines: list[Line], subs: list[Line] | None = None) -> SessionFacts | str:
    with tempfile.TemporaryDirectory() as tmp:
        projects = store(tmp, lines)
        if subs is not None:
            write(projects / "-Users-someone-dev-p" / SID / "subagents" / "agent-a1.jsonl", subs)
        refs = find_claude_sessions(projects, 10)
        assert isinstance(refs, list) and len(refs) == 1, refs
        return mine_session(refs[0], SKILLS, frozenset())


class CountTest(unittest.TestCase):
    def test_tools_pair_and_fail_under_case_names(self) -> None:
        facts = mine(
            [
                prompt(),
                use("c1", "Bash", {"command": "ls"}),
                done("c1"),
                use("c2", "Bash", {"command": "false"}),
                done("c2", is_error=True),
                use("c3", "TodoWrite", {"todos": []}),
            ]
        )
        assert not isinstance(facts, str), facts
        self.assertEqual(facts.tool_calls, {"execute_bash": 2, "todowrite": 1})
        self.assertEqual(facts.tool_fails, {"execute_bash": 1})
        self.assertEqual(facts.unpaired, 1)
        self.assertEqual(facts.created_day, "2026-11-20")

    def test_retry_is_same_tool_and_args(self) -> None:
        same: dict[str, object] = {"command": "make"}
        lines = [prompt(), use("c1", "Bash", same), use("c2", "Bash", same), use("c3", "Bash", {})]
        facts = mine(lines)
        assert not isinstance(facts, str), facts
        self.assertEqual(facts.tool_retries, {"execute_bash": 1})

    def test_skill_loads_and_file_reads(self) -> None:
        facts = mine(
            [
                prompt(),
                use("s1", "Skill", {"skill": "tiger-style"}),
                use("s2", "Skill", {"skill": "plugin:ponytail"}),
                use("s3", "Skill", {"skill": CANARY}),
                use("r1", "Read", {"file_path": "/h/.claude/skills/ponytail/SKILL.md"}),
                use("r2", "Read", {"file_path": "/h/notes.md"}),
            ]
        )
        assert not isinstance(facts, str), facts
        self.assertEqual(facts.skill_loads, {"tiger-style": 1, "ponytail": 1, OTHER: 1})
        self.assertEqual(facts.skill_file_reads, {"ponytail": 1})

    def test_sub_agent_file_is_folded_in(self) -> None:
        facts = mine([prompt(), use("c1", "Agent", {})], subs=[use("c2", "Bash", {})])
        assert not isinstance(facts, str), facts
        self.assertEqual(facts.tool_calls, {"invoke_sub_agent": 1, "execute_bash": 1})

    def test_over_bound_message_fails_closed(self) -> None:
        many = [{"type": "text", "text": "x"}] * (MINER_PARTS_PER_MESSAGE_MAX + 1)
        facts = mine([prompt(), {"type": "assistant", "message": {"content": many}}])
        self.assertIsInstance(facts, str)
        at_bound = [{"type": "text", "text": "x"}] * MINER_PARTS_PER_MESSAGE_MAX
        self.assertNotIsInstance(
            mine([prompt(), {"type": "assistant", "message": {"content": at_bound}}]), str
        )


class DiscoveryTest(unittest.TestCase):
    def test_skips_and_non_session_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            projects = store(tmp, [prompt()])
            write(projects / "-Users-someone-dev-p" / "notes.jsonl", [prompt()])
            write(projects / "-tmp" / f"{SID[:-1]}0.jsonl", [prompt("/tmp/evcwd-x")])
            write(projects / "-x" / f"{SID[:-1]}1.jsonl", [{"type": "summary"}])
            refs = find_claude_sessions(projects, 10)
            assert isinstance(refs, list), refs
            self.assertEqual(len(refs), 3)  # notes.jsonl is not a session
            self.assertEqual([r.skip_reason for r in refs if r.skip_reason], [SKIP_CLAUDE_NO_CWD])
            self.assertIsInstance(find_claude_sessions(projects, 2), str)

    def test_run_mine_skips_eval_traffic_and_keeps_text_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            projects = store(tmp, [prompt(), use("c1", "Bash", {"command": CANARY}), done("c1")])
            write(projects / "-tmp" / f"{SID[:-1]}0.jsonl", [prompt("/tmp/evcwd-x")])
            homes = {"claude": Path(tmp) / "home", "kiro": Path("/nonexistent-home")}
            (homes["claude"] / "skills" / "tiger-style").mkdir(parents=True)
            lines, code = run_mine(["--stamp", "t", "--sessions", str(projects)], Path(tmp), homes)
        text = "\n".join(lines)
        self.assertEqual(code, 0, text)
        self.assertIn("read-only over the Claude Code transcripts", text)
        self.assertIn(f"| skipped, {SKIP_EVAL} | 1 |", text)
        self.assertIn("| `execute_bash` | 1 | 1 |", text)
        self.assertIn("n/a: Claude Code loads every always-on rule", text)
        self.assertTrue(lines[-1].endswith("credits=n/a"), lines[-1])
        self.assertNotIn(CANARY.lower(), text.lower())


if __name__ == "__main__":
    unittest.main()
