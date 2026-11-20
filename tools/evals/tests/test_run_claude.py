"""Goal: the Claude Code engine parses claude -p stream-json into the same events the graders
read, isolates the run from the user's setup, caps spend in dollars, and flags a leaked skill.
Method: replay crafted streams through a fake claude binary; assert on events and records."""

from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path

from lib.cli import EVALS_ROOT
from lib.dataset import Arm, Target, discover_world, lint_all, load_limits
from lib.home import PLUGIN_MANIFEST, HomeDir, build_home
from lib.run import (
    CLAUDE_BLOCKS_MAX,
    CLAUDE_DENIALS_MAX,
    CLAUDE_ENGINE,
    CLAUDE_ISOLATION,
    CLAUDE_MD_ENV,
    Event,
    Invoker,
    InvokeResult,
    Turn,
    build_claude_argv,
    load_run_limits,
    parse_claude_stream,
    run_case,
)
from lib.run_cli import parse_run_args
from tests.helpers import STEERING_ALWAYS, build_tree
from tests.helpers_run import fixture_text, make_spec
from tests.test_run import LIMITS, make_case

SID = "0b6f1c1e-5a4e-4c4f-9a59-1d2e3f405162"
Line = dict[str, object]


def init() -> Line:
    return {"type": "system", "subtype": "init", "session_id": SID, "tools": ["Bash"]}


def use(call_id: str, name: str, tool_input: Mapping[str, object]) -> Line:
    block = {"type": "tool_use", "id": call_id, "name": name, "input": dict(tool_input)}
    content = [{"type": "text", "text": "working"}, block]
    return {"type": "assistant", "session_id": SID, "message": {"content": content}}


def done(call_id: str, is_error: bool = False, content: object = "out") -> Line:
    block = {
        "type": "tool_result",
        "tool_use_id": call_id,
        "is_error": is_error,
        "content": content,
    }
    return {"type": "user", "session_id": SID, "message": {"content": [block]}}


def result(subtype: str = "success", denied: tuple[str, ...] = (), **over: object) -> Line:
    line: Line = {
        "type": "result",
        "subtype": subtype,
        "is_error": subtype != "success",
        "session_id": SID,
        "result": "all done",
        "total_cost_usd": 0.0123,
        "duration_ms": 900,
        "permission_denials": [{"tool_name": "Write", "tool_use_id": d} for d in denied],
    }
    line.update(over)
    return line


def stream(*lines: Line | str) -> str:
    return "\n".join(x if isinstance(x, str) else json.dumps(x) for x in lines) + "\n"


def turn_of(text: str) -> Turn:
    parsed = parse_claude_stream(text)
    assert isinstance(parsed, Turn), parsed
    return parsed


def kinds(events: tuple[Event, ...], kind: str) -> list[Event]:
    return [e for e in events if e.kind == kind]


class BoundTest(unittest.TestCase):
    # goal: over a bound the stream is untrusted (a harness error), never silently cut, since a
    # cut message could hide the very call a grader looks for
    def test_blocks_over_bound(self) -> None:
        blocks = [{"type": "text", "text": "x"}] * (CLAUDE_BLOCKS_MAX + 1)
        line: Line = {"type": "assistant", "session_id": SID, "message": {"content": blocks}}
        got = parse_claude_stream(stream(init(), line, result()))
        self.assertIsInstance(got, str)
        self.assertIn("content blocks", str(got))

    def test_blocks_at_bound(self) -> None:
        blocks = [{"type": "text", "text": "x"}] * CLAUDE_BLOCKS_MAX
        line: Line = {"type": "assistant", "session_id": SID, "message": {"content": blocks}}
        turn_of(stream(init(), line, result()))

    def test_denials_over_bound(self) -> None:
        denied = tuple(f"t{n}" for n in range(CLAUDE_DENIALS_MAX + 1))
        got = parse_claude_stream(stream(init(), result(denied=denied)))
        self.assertIsInstance(got, str)
        self.assertIn("permission denials", str(got))

    def test_denials_at_bound(self) -> None:
        denied = tuple(f"t{n}" for n in range(CLAUDE_DENIALS_MAX))
        turn_of(stream(init(), result(denied=denied)))


class RecordedStreamTest(unittest.TestCase):
    # goal: a real claude -p stream (fish rule case, Bash not allowed) parses as the graders need
    def test_recorded(self) -> None:
        text = fixture_text("stream_claude-2.1.326_shell_denied.jsonl")
        turn = turn_of(text)
        calls = kinds(turn.events, "tool_call")
        self.assertEqual([(c.name, c.status) for c in calls], [("execute_bash", "denied")])
        self.assertIn("for i in 1 2 3", calls[0].args_json or "")
        self.assertEqual(kinds(turn.events, "tool_result")[0].status, "denied")
        self.assertAlmostEqual(turn.credits or 0.0, 0.00647782)
        self.assertTrue(turn.final_text)


class ParseTest(unittest.TestCase):
    # goal: Claude tool names land in the cases' vocabulary; status, cost and text carry over
    def test_clean_stream(self) -> None:
        text = stream(
            init(),
            use("t1", "Bash", {"command": "ls"}),
            done("t1"),
            use("t2", "Write", {"file_path": "/tmp/x/a.py", "content": "x"}),
            done("t2", True, [{"type": "text", "text": "denied"}]),
            result(denied=("t2",)),
        )
        turn = turn_of(text)
        calls = kinds(turn.events, "tool_call")
        self.assertEqual([c.name for c in calls], ["execute_bash", "fs_write"])
        self.assertEqual([c.status for c in calls], ["approved", "denied"])
        self.assertEqual(calls[1].paths, ("/tmp/x/a.py",))
        results = kinds(turn.events, "tool_result")
        self.assertEqual(
            [(r.name, r.status) for r in results],
            [("execute_bash", "completed"), ("fs_write", "denied")],
        )
        self.assertEqual(results[1].text, "denied")
        self.assertEqual(
            (turn.session_id, turn.final_text, turn.credits), (SID, "all done", 0.0123)
        )
        self.assertFalse(turn.capped)
        self.assertEqual(turn.events[-1].kind, "run_end")

    def test_failed_result_and_unknown_tool(self) -> None:
        turn = turn_of(stream(init(), use("t1", "LSP", {}), done("t1", True), result()))
        self.assertEqual(kinds(turn.events, "tool_call")[0].name, "LSP")
        self.assertEqual(kinds(turn.events, "tool_result")[0].status, "failed")

    def test_capped_stream_parses(self) -> None:
        turn = turn_of(stream(init(), result("error_max_budget_usd", result=None)))
        self.assertTrue(turn.capped)
        self.assertEqual(turn.final_text, "")

    def test_rejections(self) -> None:
        cases = {
            "no result": stream(init()),
            "unknown type": stream(init(), {"type": "brand_new", "session_id": SID}, result()),
            "after result": stream(init(), result(), init()),
            "error subtype": stream(init(), result("error_during_execution")),
            "bad session": stream(result(session_id="sess_1")),
            "id changed": stream(
                init(), {"type": "system", "session_id": SID[:-1] + "0"}, result()
            ),
            "no text": stream(init(), result(result=None)),
            "not json": stream(init(), "{oops", result()),
            "bad tool_use": stream(
                init(),
                use("t1", "Bash", {}) | {"message": {"content": [{"type": "tool_use"}]}},
                result(),
            ),
        }
        for label, text in cases.items():
            with self.subTest(label):
                self.assertIsInstance(parse_claude_stream(text), str)


def fake_claude(
    streams: list[str], seen: list[list[str]], envs: list[Mapping[str, str]]
) -> Invoker:
    """Replays one stream per turn; a capped stream exits 1, as claude -p does."""

    def invoke(
        argv: list[str], env: Mapping[str, str], cwd: Path, timeout_s: float
    ) -> InvokeResult:
        assert timeout_s > 0 and cwd.is_dir(), "a positive timeout and a real cwd"
        seen.append(argv)
        envs.append(env)
        text = streams[len(seen) - 1]
        code = 1 if "error_max_budget_usd" in text or "EXIT1" in text else 0
        return InvokeResult(text, "", code, False, 50)

    return invoke


class RunCaseTest(unittest.TestCase):
    # goal: turns resume one session, the env carries no KIRO_HOME, skills and caps behave
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        for name in ("home/skills/alpha", "cwd"):
            (self.base / name).mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_it(self, streams: list[str], turns: tuple[str, ...] = ("go",), usd: float = 0.2):
        seen: list[list[str]] = []
        envs: list[Mapping[str, str]] = []
        spec = make_spec(engine=CLAUDE_ENGINE, model_id="claude-haiku-5-5", credits_max=usd)
        record, view = run_case(
            spec,
            make_case(user_turns=turns, prompt=None),
            HomeDir(str(self.base / "home"), "abc"),
            self.base / "cwd",
            LIMITS,
            self.base / "runs",
            fake_claude(streams, seen, envs),
        )
        return record, view, seen, envs

    def test_two_turns_resume_and_sum_cost(self) -> None:
        one = stream(init(), use("t1", "Bash", {"command": "ls"}), done("t1"), result())
        record, view, seen, envs = self.run_it([one, one], ("first", "second"))
        self.assertEqual(record.status, "ok")
        self.assertAlmostEqual(record.credits or 0.0, 0.0246)
        self.assertNotIn("--resume", seen[0])
        self.assertEqual(seen[1][seen[1].index("--resume") + 1], SID)
        self.assertEqual(seen[1][-2:], ["--", "second"])
        self.assertTrue(all("KIRO_HOME" not in env for env in envs))
        self.assertEqual(record.tool_call_count, 2)
        self.assertEqual(view.final_text, "all done")

    def test_shipped_skill_is_read(self) -> None:
        text = stream(init(), use("s1", "Skill", {"skill": "eval-arm:alpha"}), done("s1"), result())
        record, _, _, _ = self.run_it([text])
        self.assertEqual((record.status, record.skills_read), ("ok", ("alpha",)))

    def test_foreign_skill_is_contamination(self) -> None:
        text = stream(init(), use("s1", "Skill", {"skill": "ponytail"}), done("s1"), result())
        record, _, _, _ = self.run_it([text])
        self.assertEqual(record.status, "contaminated")

    def test_capped_run_is_budget_abort(self) -> None:
        capped = stream(
            init(), use("t1", "Bash", {"command": "ls"}), result("error_max_budget_usd")
        )
        record, _, seen, _ = self.run_it([capped, capped], ("a", "b"))
        self.assertEqual(record.status, "budget_abort")
        self.assertEqual(len(seen), 1, "no turn starts after the cap")
        self.assertEqual(record.tool_call_count, 1, "the capped turn's events are kept")

    def test_clean_stream_with_bad_exit_is_harness_error(self) -> None:
        record, _, _, _ = self.run_it([stream(init(), result(result="EXIT1"))])
        self.assertEqual((record.status, record.note), ("harness_error", "exit code 1"))

    def test_crash_is_harness_error(self) -> None:
        record, _, _, _ = self.run_it(["not a stream"])
        self.assertEqual(record.status, "harness_error")
        self.assertIn("line 1", record.note)

    def test_second_turn_gets_the_remaining_budget(self) -> None:
        one = stream(init(), result(total_cost_usd=0.15))
        _, _, seen, _ = self.run_it([one, one], ("a", "b"), usd=0.2)
        caps = [float(argv[argv.index("--max-budget-usd") + 1]) for argv in seen]
        self.assertAlmostEqual(caps[0], 0.2)
        self.assertAlmostEqual(caps[1], 0.05)


class ArgvTest(unittest.TestCase):
    # goal: isolation flags always present; skills, rules and trust only when the arm has them
    def test_argv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            bare = build_claude_argv(make_spec(), "-p is a prompt", None, 0.1, home)
            (home / "skills").mkdir()
            (home / "steering").mkdir()
            (home / "steering" / "shell.md").write_text("use fish", encoding="utf-8")
            spec = make_spec(trust_tools=("Bash", "Read"))
            full = build_claude_argv(spec, "go", SID, 0.1, home)
        joined = " ".join(bare)
        self.assertIn(" ".join(c for c in CLAUDE_ISOLATION if c), joined.replace("  ", " "))
        self.assertEqual(bare[-2:], ["--", "-p is a prompt"])
        for flag in ("--plugin-dir", "--append-system-prompt", "--resume", "--allowedTools"):
            self.assertNotIn(flag, bare)
            self.assertIn(flag, full)
        self.assertEqual(full[full.index("--allowedTools") + 1], "Bash,Read")
        self.assertIn("use fish", full[full.index("--append-system-prompt") + 1])
        self.assertEqual(full[full.index("--setting-sources") + 1], "")
        self.assertEqual(full[full.index("--permission-mode") + 1], "dontAsk")


class ClaudeMdRulesTest(unittest.TestCase):
    # goal: a home with CLAUDE.md loads rules as memory (--add-dir + env), never as system prompt
    def test_claude_md_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, root = build_tree(Path(tmp))
            limits = load_limits(root / "limits.json")
            world = discover_world(home, root, limits)
            dest = Path(tmp) / "dest"
            dest.mkdir()
            built = build_home(
                Arm("full", None),
                Target("rule-a", "steering"),
                None,
                (),
                (),
                src_home=home,
                dest=dest,
                always_on=world.always_on_names,
                files_max=64,
                plugin=True,
                claude_md=True,
            )
            assert not isinstance(built, str), built
            imports = (dest / "CLAUDE.md").read_text(encoding="utf-8")
            argv = build_claude_argv(make_spec(), "go", None, 0.1, dest)
        self.assertEqual(imports, STEERING_ALWAYS)
        self.assertEqual(argv[argv.index("--add-dir") + 1], str(dest))
        self.assertNotIn("--append-system-prompt", argv)

    def test_no_rules_no_claude_md(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, _ = build_tree(Path(tmp))
            dest = Path(tmp) / "dest"
            dest.mkdir()
            build_home(
                Arm("none", None),
                Target("rule-a", "steering"),
                None,
                (),
                (),
                src_home=home,
                dest=dest,
                always_on=frozenset(),
                files_max=64,
                plugin=True,
                claude_md=True,
            )
            self.assertFalse((dest / "CLAUDE.md").exists())

    def test_env_and_flag(self) -> None:
        seen: list[list[str]] = []
        envs: list[Mapping[str, str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "home").mkdir()
            (base / "cwd").mkdir()
            run_case(
                make_spec(engine=CLAUDE_ENGINE),
                make_case(),
                HomeDir(str(base / "home"), "abc"),
                base / "cwd",
                LIMITS,
                base / "runs",
                fake_claude([stream(init(), result())], seen, envs),
            )
        self.assertEqual(envs[0].get(CLAUDE_MD_ENV), "1")
        default = parse_run_args(["steering/shell"])
        legacy = parse_run_args(["steering/shell", "--rules", "system-prompt"])
        assert not isinstance(default, str) and not isinstance(legacy, str)
        self.assertTrue(default.rules_claude_md, "CLAUDE.md is the default, as in real use")
        self.assertFalse(legacy.rules_claude_md)
        self.assertIsInstance(parse_run_args(["steering/shell", "--rules", "both"]), str)


class LimitsAndArgsTest(unittest.TestCase):
    # goal: Claude caps are dollars from the claude block; Claude is the default engine
    def test_claude_limits(self) -> None:
        limits = load_run_limits(EVALS_ROOT / "limits.json", CLAUDE_ENGINE)
        self.assertEqual(limits.cost_unit, "usd")
        self.assertEqual(limits.probe_credits_max, 0.3)
        self.assertEqual(limits.tiers["smoke"].run_credits_max, 0.15)
        self.assertEqual(limits.model_run_credits_max, {"claude-opus-5-5": 1.5})
        kiro = load_run_limits(EVALS_ROOT / "limits.json")
        self.assertEqual((kiro.cost_unit, kiro.probe_credits_max), ("credits", 0.2))

    def test_missing_claude_block_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = json.loads((EVALS_ROOT / "limits.json").read_text(encoding="utf-8"))
            del raw["claude"]
            path = Path(tmp) / "limits.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_run_limits(path, CLAUDE_ENGINE)

    def test_engine_defaults(self) -> None:
        claude = parse_run_args(["steering/shell"])
        kiro = parse_run_args(["steering/shell", "--engine", "kiro"])
        assert not isinstance(claude, str) and not isinstance(kiro, str)
        self.assertEqual((claude.engine, claude.model_id), ("claude", "claude-haiku-5-5"))
        self.assertEqual(claude.kiro_home, Path("~/.claude").expanduser())
        self.assertEqual((kiro.engine, kiro.model_id), ("v3", "claude-haiku-5.5"))
        self.assertEqual(kiro.kiro_home, Path("~/.kiro").expanduser())
        old = parse_run_args(["steering/shell", "--kiro-home", "/k"])
        assert not isinstance(old, str)
        self.assertEqual(old.kiro_home, Path("/k"))
        self.assertIsInstance(parse_run_args(["steering/shell", "--engine", "codex"]), str)


class ClaudeHomeTest(unittest.TestCase):
    # goal: a home with CLAUDE.md takes its always-on set from imports and skips frontmatter lint
    def test_imports_are_always_on(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, root = build_tree(Path(tmp))
            (home / "steering" / "rule-c.md").write_text("# no frontmatter\n", encoding="utf-8")
            (home / "CLAUDE.md").write_text(
                "# me\n@~/.claude/steering/rule-c.md\n", encoding="utf-8"
            )
            limits = load_limits(root / "limits.json")
            world = discover_world(home, root, limits)
            findings = lint_all(home, world, limits)
        self.assertTrue(world.claude)
        self.assertEqual(world.always_on_names, frozenset({"rule-c"}))
        self.assertEqual([f for f in findings if f.rule in ("frontmatter", "inclusion")], [])

    def test_kiro_home_still_lints_frontmatter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, root = build_tree(Path(tmp))
            (home / "steering" / "rule-c.md").write_text("# no frontmatter\n", encoding="utf-8")
            limits = load_limits(root / "limits.json")
            world = discover_world(home, root, limits)
            findings = lint_all(home, world, limits)
        self.assertFalse(world.claude)
        self.assertEqual(world.always_on_names, frozenset({"rule-a"}))
        self.assertIn("frontmatter", [f.rule for f in findings])

    def test_plugin_manifest_only_for_claude(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, root = build_tree(Path(tmp))
            limits = load_limits(root / "limits.json")
            world = discover_world(home, root, limits)
            built: list[bool] = []
            for plugin in (True, False):
                dest = Path(tmp) / f"dest-{plugin}"
                dest.mkdir()
                home_dir = build_home(
                    Arm("with", None),
                    Target("alpha", "skill"),
                    None,
                    (),
                    (),
                    src_home=home,
                    dest=dest,
                    always_on=world.always_on_names,
                    files_max=64,
                    plugin=plugin,
                )
                assert not isinstance(home_dir, str), home_dir
                built.append((dest / PLUGIN_MANIFEST).is_file())
        self.assertEqual(built, [True, False])


if __name__ == "__main__":
    unittest.main()
