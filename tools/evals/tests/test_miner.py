"""Goal: `eval mine` counts tools, skills and steering from session logs and never prints text.
Method: tiny logs built in a temp dir (never copies of real ones). Each test has a planted twin:
the same log with one fact changed, whose count must change, so a miner that counts everything or
nothing fails. The canary test plants a secret in every text-bearing field."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from lib.miner import (
    MINER_RETRY_WINDOW_CALLS,
    OTHER,
    SessionRef,
    find_sessions,
    is_eval_traffic,
    merge,
    mine_session,
    read_events,
    render_report,
    run_mine,
)

CANARY = "SECRET-0xDEADBEEF"
NO_HOMES = {"claude": Path("/nonexistent-home"), "kiro": Path("/nonexistent-home")}
SKILLS = frozenset({"tiger-style", "ponytail", "egm-draft"})
STEERING = frozenset({"shell", "working-style"})
STAMP = "2026-11-12 22:00 -0500"
EXEC_A, EXEC_B = "exec-a", "exec-b"


def line(payload: dict[str, Any]) -> str:
    return json.dumps({"id": "i", "timestamp": "t", "payload": payload})


def call(
    cid: str,
    tool: str,
    args: dict[str, Any] | None = None,
    exec_id: str = EXEC_A,
    status: str = "completed",
) -> str:
    return line(
        {
            "type": "tool_call",
            "toolCallId": cid,
            "toolName": tool,
            "args": args or {},
            "status": status,
            "executionId": exec_id,
            "title": CANARY,
        }
    )


def result(cid: str, content: str = "ok", success: bool = True) -> str:
    return line({"type": "tool_result", "toolCallId": cid, "content": content, "success": success})


def usage(exec_id: str, *amounts: float) -> str:
    return line(
        {
            "type": "usage_summary",
            "executionId": exec_id,
            "promptTurnSummaries": [{"unit": "credit", "usage": a} for a in amounts],
        }
    )


def make_session(
    store: Path,
    group: str,
    name: str,
    lines: list[str],
    cwd: str = "/home/dev",
    mode: str = "vibe",
    subs: dict[str, list[str]] | None = None,
    title: str = "t",
) -> Path:
    sess = store / group / name
    (sess / "sub-executions").mkdir(parents=True)
    (sess / "messages.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    meta = {
        "agentMode": mode,
        "workspacePaths": [cwd],
        "rootPaths": [cwd],
        "createdAt": "2026-11-01T10:00:00.000Z",
        "title": title,
    }
    (sess / "session.json").write_text(json.dumps(meta), encoding="utf-8")
    for sub_name, sub_lines in (subs or {}).items():
        (sess / "sub-executions" / sub_name).write_text(
            "\n".join(sub_lines) + "\n", encoding="utf-8"
        )
    return sess


def make_headless(store: Path, name: str, parts: list[dict[str, Any]], cwd: str) -> None:
    (store / "cli").mkdir(exist_ok=True)
    msg = {"kind": "AssistantMessage", "data": {"content": parts}}
    (store / "cli" / f"{name}.jsonl").write_text(json.dumps(msg) + "\n", encoding="utf-8")
    meta = {"cwd": cwd, "created_at": "2026-11-12T13:45:55Z", "title": CANARY}
    (store / "cli" / f"{name}.json").write_text(json.dumps(meta), encoding="utf-8")


def mine_lines(lines: list[str], tree: str = "interactive") -> Any:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "messages.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        ref = SessionRef(tree, path, (), ("/home/dev",), "2026-11-01")
        facts = mine_session(ref, SKILLS, STEERING)
    assert not isinstance(facts, str), facts
    return facts


def mine_store(store: Path, *extra: str) -> tuple[list[str], int, Path]:
    home = store.parent / "home"
    (home / "skills" / "tiger-style").mkdir(parents=True, exist_ok=True)
    (home / "skills" / "ponytail").mkdir(parents=True, exist_ok=True)
    (home / "steering").mkdir(parents=True, exist_ok=True)
    (home / "steering" / "shell.md").write_text("x", encoding="utf-8")
    out = store.parent / "report.md"
    lines, code = run_mine(
        ["--stamp", STAMP, "--harness", "kiro", "--sessions", str(store), "--kiro-home", str(home)]
        + list(extra),
        Path(__file__).resolve().parent.parent,
        NO_HOMES,
    )
    return lines, code, out


class TreesTest(unittest.TestCase):
    def test_all_three_trees_counted_and_eval_traffic_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "sessions"
            body = [call("c1", "read_file"), result("c1")]
            make_session(store, "h1", "sess_1", body)
            make_session(store, "h1", "sess_2", body, mode="wf-step")
            make_session(store, "h1", "sess_eval", body, cwd="/tmp/evcwd-abc")
            part: dict[str, Any] = {
                "kind": "toolUse",
                "data": {"toolUseId": "t1", "name": "read", "input": {}},
            }
            make_headless(store, "real", [part], "/home/dev/proj")
            make_headless(store, "probe", [part], "/private/tmp")
            lines, code, _ = mine_store(store, "--out", str(Path(tmp) / "report.md"))
            text = (Path(tmp) / "report.md").read_text(encoding="utf-8")
        self.assertEqual(code, 0, lines)
        self.assertIn("| mined | 3 |", text)
        self.assertIn("| seen | 5 |", text)
        for tree in ("interactive", "workflow", "headless"):
            self.assertIn(f"| mined, {tree} | ", text)
        self.assertIn("| mined, interactive | 1 |", text)
        self.assertIn("| skipped, eval traffic (cwd under /tmp) | 2 |", text)
        self.assertIn("| `read_file` | 2 |", text)  # the planted twin: eval sessions add nothing
        self.assertIn("| `read` | 1 |", text)

    def test_is_eval_traffic_both_ways(self) -> None:
        roots = [Path("/tmp").resolve()]
        self.assertTrue(is_eval_traffic(["/tmp/evcwd-x"], roots))
        self.assertTrue(is_eval_traffic(["/private/tmp"], roots))
        self.assertTrue(is_eval_traffic(["/home/a", "/tmp/y"], roots))
        self.assertFalse(is_eval_traffic(["/home/a/tmp"], roots))
        self.assertFalse(is_eval_traffic([], roots))

    def test_sub_executions_are_attached_to_their_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "sessions"
            make_session(
                store,
                "h1",
                "sess_1",
                [call("c1", "grep_search"), result("c1")],
                subs={"s.jsonl": [call("c2", "grep_search"), result("c2")]},
            )
            refs = find_sessions(store, 10)
            assert not isinstance(refs, str)
            facts = mine_session(refs[0], SKILLS, STEERING)
        assert not isinstance(facts, str)
        self.assertEqual(facts.tool_calls, {"grep_search": 2})
        self.assertEqual(len(refs), 1)


class PairingTest(unittest.TestCase):
    def test_pairing_by_call_id_and_unpaired(self) -> None:
        facts = mine_lines(
            [call("c1", "read_file"), call("c2", "read_file"), result("c2"), result("ghost")]
        )
        self.assertEqual(facts.tool_calls, {"read_file": 2})
        self.assertEqual(facts.tool_paired, {"read_file": 1})
        self.assertEqual(facts.unpaired, 1)  # c1 never got a result; "ghost" is not a call
        self.assertEqual(facts.tool_fails, {})

    def test_repeated_call_event_is_one_call(self) -> None:
        facts = mine_lines([call("c1", "read_file"), call("c1", "read_file"), result("c1")])
        self.assertEqual(facts.tool_calls, {"read_file": 1})

    def test_shell_errors_come_from_exit_code_not_quoted_text(self) -> None:
        fish_msg = "fish: Unknown command: foo\nExit Code: 0"
        facts = mine_lines(
            [
                call("a", "execute_bash"),
                result("a", "done\n\nExit Code: 1"),
                call("b", "execute_bash", {"command": "x"}),
                result("b", fish_msg),
                call("c", "execute_bash", {"command": "y"}),
                result("c", "boom", success=False),
                call("d", "execute_bash", {"command": "z"}),
                result("d", "fine\n\nExit Code: 0"),
                call("e", "read_file"),
                result("e", "Exit Code: 9"),  # non-shell tools ignore the code
            ]
        )
        self.assertEqual(facts.tool_paired["execute_bash"], 4)
        self.assertEqual(facts.tool_fails, {"execute_bash": 2})  # a (code 1) and c (success false)

    def test_retry_window_edges_and_execution_boundary(self) -> None:
        same = {"path": "a"}
        window = MINER_RETRY_WINDOW_CALLS
        gap = [call(f"g{i}", "grep_search", {"q": i}) for i in range(window - 1)]
        inside = [call("c1", "read_file", same), *gap, call("c2", "read_file", same)]
        self.assertEqual(mine_lines(inside).tool_retries, {"read_file": 1})
        far = [
            call("c1", "read_file", same),
            *gap,
            call("g9", "grep_search", {"q": 9}),
            call("c2", "read_file", same),
        ]
        self.assertEqual(mine_lines(far).tool_retries, {})
        other_exec = [call("c1", "read_file", same), call("c2", "read_file", same, EXEC_B)]
        self.assertEqual(mine_lines(other_exec).tool_retries, {})
        other_args = [call("c1", "read_file", same), call("c2", "read_file", {"path": "b"})]
        self.assertEqual(mine_lines(other_args).tool_retries, {})

    def test_usage_credits_are_summed(self) -> None:
        facts = mine_lines([usage(EXEC_A, 0.25, 0.5), usage(EXEC_B, 1.0)])
        self.assertAlmostEqual(facts.credits, 1.75)
        self.assertAlmostEqual(mine_lines([usage(EXEC_A, 0.25)]).credits, 0.25)


class SkillTest(unittest.TestCase):
    def test_load_denied_and_file_read_are_counted_apart(self) -> None:
        facts = mine_lines(
            [
                usage(EXEC_A, 0.5),
                call("l1", SKILL_TOOL, {"name": "tiger-style"}),
                call("l2", SKILL_TOOL, {"id": "skill::ponytail"}),
                call("l3", SKILL_TOOL, {"name": "egm-draft"}, status="denied"),
                call("r1", "read_file", {"path": "/h/.kiro/skills/tiger-style/SKILL.md"}),
                call("r2", "read_file", {"path": "/h/.kiro/skills/tiger-style/references/x.md"}),
            ]
        )
        self.assertEqual(facts.skill_loads, {"tiger-style": 1, "ponytail": 1})
        self.assertEqual(facts.skill_denied, {"egm-draft": 1})
        self.assertEqual(facts.skill_file_reads, {"tiger-style": 1})
        self.assertAlmostEqual(facts.skill_credits["tiger-style"], 0.5)
        self.assertNotIn("egm-draft", facts.skill_credits)

    def test_unknown_and_spaced_skill_names_become_other(self) -> None:
        facts = mine_lines(
            [
                call("l1", SKILL_TOOL, {"name": "no such skill"}),
                call("l2", SKILL_TOOL, {"name": "not-installed"}),
            ]
        )
        self.assertEqual(facts.skill_loads, {OTHER: 2})

    def test_never_used_skills_are_listed(self) -> None:
        facts = mine_lines([call("l1", SKILL_TOOL, {"name": "tiger-style"})])
        totals = merge([facts])
        text = "\n".join(render_report(totals, STAMP, sorted(SKILLS), sorted(STEERING), "kiro"))
        self.assertIn("Never loaded (2 of 3): `egm-draft`, `ponytail`", text)
        both = merge([facts, mine_lines([call("l2", SKILL_TOOL, {"name": "ponytail"})])])
        text2 = "\n".join(render_report(both, STAMP, sorted(SKILLS), sorted(STEERING), "kiro"))
        self.assertIn("Never loaded (1 of 3): `egm-draft`", text2)

    def test_steering_inclusion_both_shapes(self) -> None:
        inc = line(
            {
                "type": "steering_inclusion",
                "executionId": EXEC_A,
                "documents": [
                    "file:///h/.kiro/steering/shell.md",
                    {"id": "file:///x", "displayName": "working-style", "content": CANARY},
                    {"displayName": "unlisted-file"},
                ],
            }
        )
        facts = mine_lines([inc, inc])
        self.assertEqual(facts.steering_events, {"shell": 2, "working-style": 2, OTHER: 2})
        totals = merge([facts])
        self.assertEqual(totals.steering_sessions["shell"], 1)

    def test_headless_shape_counts_tools_and_skill_loads(self) -> None:
        msg = json.dumps(
            {
                "kind": "AssistantMessage",
                "data": {
                    "content": [
                        {
                            "kind": "toolUse",
                            "data": {
                                "toolUseId": "t1",
                                "name": SKILL_TOOL,
                                "input": {"name": "ponytail"},
                            },
                        },
                        {
                            "kind": "toolUse",
                            "data": {
                                "toolUseId": "t2",
                                "name": "read",
                                "input": {
                                    "operations": [{"path": "/h/.kiro/skills/ponytail/SKILL.md"}]
                                },
                            },
                        },
                    ]
                },
            }
        )
        res = json.dumps(
            {
                "kind": "ToolResults",
                "data": {
                    "content": [
                        {"kind": "toolResult", "data": {"toolUseId": "t1", "status": "success"}},
                        {"kind": "toolResult", "data": {"toolUseId": "t2", "status": "error"}},
                    ]
                },
            }
        )
        facts = mine_lines([msg, res], tree="headless")
        self.assertEqual(facts.skill_loads, {"ponytail": 1})
        self.assertEqual(facts.skill_file_reads, {"ponytail": 1})
        self.assertEqual(facts.tool_fails, {"read": 1})


class BoundsAndErrorsTest(unittest.TestCase):
    def test_over_bound_is_a_reason_not_a_partial_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.jsonl"
            path.write_text("\n".join(call(f"c{i}", "t") for i in range(5)) + "\n")
            self.assertIsInstance(read_events(path, 3, 10_000), str)
            self.assertIsInstance(read_events(path, 100, 10), str)
            ok = read_events(path, 5, 10_000)
            self.assertNotIsInstance(ok, str)

    def test_run_mine_stops_on_over_bound_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "sessions"
            make_session(store, "h1", "sess_1", [call(f"c{i}", "read_file") for i in range(5)])
            report = Path(tmp) / "report.md"
            lines, code, _ = mine_store(store, "--lines-max", "3", "--out", str(report))
            self.assertEqual(code, 1)
            self.assertTrue(lines[0].startswith("mine: error "), lines)
            self.assertFalse(report.exists())
            lines2, code2, _ = mine_store(store, "--lines-max", "30", "--out", str(report))
            self.assertEqual(code2, 0, lines2)
            self.assertTrue(report.exists())

    def test_corrupt_line_is_counted_and_fails_the_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "sessions"
            make_session(store, "h1", "sess_1", [call("c1", "read_file"), "{not json", "[1]"])
            report = Path(tmp) / "report.md"
            lines, code, _ = mine_store(store, "--out", str(report))
            text = report.read_text(encoding="utf-8")
        self.assertEqual(code, 1)
        self.assertIn("parse_errors=2", lines[-1])
        self.assertIn("| parse errors (corrupt lines) | 2 |", text)

    def test_usage_errors(self) -> None:
        root = Path(__file__).resolve().parent.parent
        for argv in (
            [],
            ["--sessions", "x"],
            ["--stamp", ""],
            ["--stamp", "s", "--bogus", "1"],
            ["--stamp", "s", "--lines-max", "0"],
            ["--stamp"],
            ["--stamp", "s", "--harness", "codex"],
            ["--stamp", "s", "--home", "a", "--kiro-home", "b"],
        ):
            lines, code = run_mine(argv, root, NO_HOMES)
            self.assertEqual(code, 2, lines)
        for harness in ("claude", "kiro"):
            argv = ["--stamp", "s", "--harness", harness, "--sessions", "/nonexistent-dir"]
            _, code = run_mine(argv, root, NO_HOMES)
            self.assertEqual(code, 1, harness)  # an unreadable store fails closed


class NoTextTest(unittest.TestCase):
    def test_canary_in_every_text_field_never_reaches_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "sessions"
            canary_lower = CANARY.lower()
            body = [
                line({"type": "user", "content": CANARY, "documents": [CANARY]}),
                line({"type": "assistant", "content": CANARY, "executionId": EXEC_A}),
                call("c1", "execute_bash", {"command": f"echo {CANARY}"}),
                result("c1", f"{CANARY}\n\nExit Code: 1"),
                call("c2", CANARY, {"x": CANARY}),
                result("c2", CANARY, success=False),
                call("c4", SKILL_TOOL, {"name": CANARY}),
                call("c5", "read_file", {"path": f"/h/{CANARY}/skills/{CANARY}/SKILL.md"}),
                line(
                    {
                        "type": "steering_inclusion",
                        "documents": [
                            f"file:///x/{CANARY}.md",
                            {"displayName": CANARY, "content": CANARY},
                        ],
                    }
                ),
                line({"type": "session_metadata", "key": CANARY, "value": {"a": CANARY}}),
                usage(EXEC_A, 0.1),
            ]
            make_session(
                store,
                "h1",
                f"sess_{CANARY}",
                body,
                cwd=f"/home/{CANARY}",
                title=CANARY,
                subs={"s.jsonl": [call("c9", "read_file", {"p": CANARY})]},
            )
            part: dict[str, Any] = {
                "kind": "toolUse",
                "data": {"toolUseId": "t1", "name": CANARY, "input": {"q": CANARY}},
            }
            make_headless(store, "h", [part], f"/home/{CANARY}")
            report = Path(tmp) / "report.md"
            lines, code, _ = mine_store(store, "--out", str(report))
            text = report.read_text(encoding="utf-8")
        everything = "\n".join(lines) + text
        self.assertEqual(code, 0, lines)
        self.assertNotIn(CANARY, everything)
        self.assertNotIn(canary_lower, everything.lower())
        self.assertIn("| mined | 2 |", text)  # the planted twin: the session really was mined
        self.assertIn(f"`{OTHER}`", text)


SKILL_TOOL = "disclose_context"

if __name__ == "__main__":
    unittest.main()
