"""Isolation of Claude eval runs: scoped file tools, sandbox settings, the suite-start probe,
and contamination that counts only what happened (design: evals-isolation)."""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from unittest import mock

from lib import isolation_probe
from lib.isolation import isolated_environ, isolation_env, sandbox_settings, scoped_allowed_tools
from lib.isolation_probe import PROBE_ATTEMPTS_MAX, probe_isolation
from lib.run import (
    CLAUDE_ENGINE,
    Ceiling,
    Event,
    Invoker,
    InvokeResult,
    contamination_note,
    foreign_skills,
    outside_paths,
)
from lib.run_cli import with_isolation_probe
from tests.helpers_run import make_spec
from tests.test_run_claude import done, init, result, stream, use

BLOCKED = "cat: x: Operation not permitted"


class ScopedToolsTest(unittest.TestCase):
    # goal: no bare file tool ever reaches --allowedTools; unknown tools and bad paths refuse
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.cwd, self.home = base / "cwd", base / "home"
        self.cwd.mkdir()
        self.home.mkdir()
        self.rc, self.rh = os.path.realpath(self.cwd), os.path.realpath(self.home)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_each_tool_gets_its_scope(self) -> None:
        rules = scoped_allowed_tools(
            ("Bash", "Read", "Write", "Edit", "Skill"), self.cwd, self.home
        )
        self.assertEqual(
            rules,
            (
                "Bash",
                f"Read(/{self.rc}/**)",
                f"Read(/{self.rh}/**)",
                f"Write(/{self.rc}/**)",
                f"Edit(/{self.rc}/**)",
                "Skill",
            ),
        )

    def test_unknown_tool_raises(self) -> None:
        with self.assertRaises(ValueError):
            scoped_allowed_tools(("Read", "WebFetch"), self.cwd, self.home)

    def test_glob_character_in_path_trips(self) -> None:
        odd = Path(self.tmp.name) / "a*b"
        odd.mkdir()
        with self.assertRaises(AssertionError):
            scoped_allowed_tools(("Read",), odd, self.home)

    def test_path_under_real_home_trips(self) -> None:
        with self.assertRaises(AssertionError):
            scoped_allowed_tools(("Read",), Path.home(), self.home)


class SandboxSettingsTest(unittest.TestCase):
    # goal: the settings JSON is pinned; a change to it is a deliberate, reviewed change
    def test_pinned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.realpath(tmp)
            got = json.loads(sandbox_settings(Path(tmp)))
            env = isolation_env(Path(tmp))
        want = {
            "sandbox": {
                "enabled": True,
                "allowUnsandboxedCommands": False,
                "filesystem": {
                    "denyRead": ["~"],
                    "allowRead": ["~/.local/share/uv"],
                    "allowWrite": [real],
                },
            }
        }
        self.assertEqual(got, want)
        self.assertEqual(env["UV_CACHE_DIR"], os.path.join(real, "uv"))
        self.assertEqual(env["XDG_CACHE_HOME"], real)
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
        with tempfile.TemporaryDirectory() as tmp:
            run_env = isolated_environ({"VIRTUAL_ENV": "/x/.venv", "PATH": "/bin"}, Path(tmp))
        self.assertNotIn("VIRTUAL_ENV", run_env)
        self.assertEqual(run_env["PATH"], "/bin")


def call(cid: str, name: str, status: str | None, paths: tuple[str, ...] = ()) -> Event:
    return Event(0, "tool_call", name, "{}", status, None, cid, paths)


def res(cid: str, status: str) -> Event:
    return Event(0, "tool_result", None, None, status, None, cid, ())


def skill(cid: str, name: str) -> Event:
    return Event(0, "skill_read", name, None, "approved", None, cid, ())


class CountWhatHappenedTest(unittest.TestCase):
    # goal: a failed or unanswered skill load and a denied outside read are not contamination
    def test_foreign_skills_needs_a_completed_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "skills" / "mine").mkdir(parents=True)
            events = (
                skill("a", "other"),
                res("a", "failed"),
                skill("b", "lost"),  # no result ever arrived
                skill("c", "leaked"),
                res("c", "completed"),
                skill("d", "mine"),
                res("d", "completed"),
            )
            self.assertEqual(foreign_skills(events, home), ["leaked"])

    def test_outside_paths_skips_denied_calls(self) -> None:
        events = (
            call("a", "read_file", "denied", ("/etc/denied.txt",)),
            call("b", "read_file", "approved", ("/etc/read.txt",)),
        )
        self.assertEqual(outside_paths(events, "/tmp/evx/cwd", "/tmp/evx/home"), ["/etc/read.txt"])

    def test_note_counts_not_paths(self) -> None:
        events = (
            call("a", "read_file", "denied", ("/etc/secret-name.txt",)),
            call("b", "read_file", "approved", ("/etc/other.txt",)),
        )
        note = contamination_note(events, "/tmp/evx/cwd", "/tmp/evx/home", ["leaked"])
        self.assertEqual(
            note, "contaminated: foreign_skills=leaked outside_paths=1 denied_path_calls=1"
        )
        self.assertNotIn("secret", note)


def probe_invoker(kind: str, calls: list[list[str]]) -> Invoker:
    """Plays one probe outcome; reads the canary path from the prompt like a model would."""

    def invoke(
        argv: list[str], env: Mapping[str, str], cwd: Path, timeout_s: float
    ) -> InvokeResult:
        assert timeout_s > 0 and cwd.is_dir() and "--settings" in argv, "a real probe turn"
        calls.append(argv)
        match = re.search(r"cat (\S+)", argv[-1])
        assert match, "the prompt names the canary"
        path = match.group(1)
        if kind == "raise":
            raise RuntimeError("invoker crashed")
        if kind == "missed":
            return InvokeResult(stream(init(), result()), "", 0, False, 10)
        bash_out = Path(path).read_text(encoding="utf-8") if kind == "leak" else BLOCKED
        if kind == "unblocked":
            bash_out = "some other output"
        lines = stream(
            init(),
            use("b1", "Bash", {"command": f"cat {path}"}),
            done("b1", True, bash_out),
            use("r1", "Read", {"file_path": path}),
            done("r1", True, "denied"),
            result(denied=() if kind == "read_allowed" else ("r1",)),
        )
        return InvokeResult(lines, "", 0, False, 10)

    return invoke


class ProbeTest(unittest.TestCase):
    # goal: the probe passes only on proof both reads were blocked; everything else refuses,
    # and the canary never outlives it
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.parent = Path(self.tmp.name)
        patcher = mock.patch.object(isolation_probe, "CANARY_PARENT", self.parent)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.spec = make_spec(engine=CLAUDE_ENGINE, model_id="claude-haiku-5-5", credits_max=0.15)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_probe(self, kind: str, settings: str | None = None) -> tuple[str | None, int]:
        calls: list[list[str]] = []
        reason = probe_isolation(self.spec, probe_invoker(kind, calls), settings)
        self.assertEqual(list(self.parent.glob("evals-canary-*")), [], "canary removed")
        return reason, len(calls)

    def test_blocked_passes(self) -> None:
        self.assertEqual(self.run_probe("blocked"), (None, 1))

    def test_leak_refuses_at_once(self) -> None:
        reason, count = self.run_probe("leak")
        self.assertTrue(reason and reason.startswith("LEAK"), reason)
        self.assertEqual(count, 1)

    def test_missed_is_retried_then_refused(self) -> None:
        reason, count = self.run_probe("missed")
        self.assertEqual(reason, isolation_probe.MISSED)
        self.assertEqual(count, PROBE_ATTEMPTS_MAX)

    def test_unblocked_bash_refuses(self) -> None:
        self.assertEqual(self.run_probe("unblocked"), ("Bash was not blocked by the sandbox", 1))

    def test_allowed_read_refuses(self) -> None:
        reason, count = self.run_probe("read_allowed")
        self.assertEqual((reason, count), ("Read outside the project folder was not denied", 1))

    def test_canary_gone_after_a_crash(self) -> None:
        with self.assertRaises(RuntimeError):
            self.run_probe("raise")
        self.assertEqual(list(self.parent.glob("evals-canary-*")), [])

    def test_settings_seam_reaches_the_argv(self) -> None:
        calls: list[list[str]] = []
        probe_isolation(self.spec, probe_invoker("blocked", calls), '{"sandbox": {}}')
        self.assertEqual(calls[0][calls[0].index("--settings") + 1], '{"sandbox": {}}')


class CeilingTest(unittest.TestCase):
    # goal: a Claude suite's worst case includes the probe's attempts; Kiro's doesn't change
    def test_claude_adds_probe_attempts(self) -> None:
        spec = make_spec(engine=CLAUDE_ENGINE, credits_max=0.15, wall_ms_max=1000)
        got = with_isolation_probe(Ceiling(3, 0.45, 3000), [spec], CLAUDE_ENGINE)
        self.assertEqual(got.run_count, 3 + PROBE_ATTEMPTS_MAX)
        self.assertAlmostEqual(got.credits_max_total, 0.45 + 0.15 * PROBE_ATTEMPTS_MAX)
        self.assertEqual(got.wall_ms_max_total, 3000 + 1000 * PROBE_ATTEMPTS_MAX)

    def test_kiro_unchanged(self) -> None:
        ceiling = Ceiling(3, 0.45, 3000)
        self.assertEqual(with_isolation_probe(ceiling, [make_spec()], "v3"), ceiling)
