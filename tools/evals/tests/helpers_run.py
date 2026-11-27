"""Builders for step-2 tests: events, views, specs, a fake kiro-cli that replays fixtures."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from lib.dataset import Arm
from lib.run import Event, Invoker, InvokeResult, RunSpec, RunStatus, RunView

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SHELL_OK = "stream_v3_kiro-cli-3.2.1_shell_ok.jsonl"
DENIED = "stream_v3_kiro-cli-3.2.1_read_shell_write_denied.jsonl"
DENIED_LOG = "session_v3_kiro-cli-3.2.1_read_shell_write_denied.messages.jsonl"
SKILL_LOAD = "stream_v3_kiro-cli-3.2.1_skill_load.jsonl"
SHELL_OK_SESSION = "sess_491bfa57-52a4-4409-8339-c2b0d18e96f4"
SHELL_OK_CALL = "run_command_toolu_bdrk_01KNYrKf7qkQd1xkLrLp7kVd"
DENIED_SESSION = "sess_9e22f479-79fd-4b42-b3a2-4baedf7eb312"
SHELL_OK_CREDITS = 0.00306286169154229


def fixture_text(name: str) -> str:
    path = FIXTURES / name
    # a missing recording is a broken checkout, not a reason to skip
    assert path.is_file(), f"recorded fixture {name} is missing"
    return path.read_text(encoding="utf-8")


def write_session_log(home: Path, session_id: str, calls: list[tuple[str, str, str]]) -> None:
    """A minimal messages.jsonl: calls are (toolCallId, toolName, status)."""
    log_dir = home / "sessions" / "0123456789abcdef" / session_id
    log_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            {
                "id": f"{call_id}-call",
                "payload": {
                    "type": "tool_call",
                    "toolCallId": call_id,
                    "toolName": tool,
                    "status": status,
                },
            }
        )
        for call_id, tool, status in calls
    ]
    (log_dir / "messages.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_event(
    kind: str,
    name: str | None = None,
    args: Mapping[str, object] | None = None,
    *,
    status: str | None = None,
    text: str | None = None,
    call_id: str | None = None,
    paths: tuple[str, ...] = (),
) -> Event:
    args_json = None if args is None else json.dumps(dict(args), sort_keys=True)
    return Event(0, kind, name, args_json, status, text, call_id, paths)


def call(tool: str, args: Mapping[str, object] | None = None, paths: tuple[str, ...] = ()) -> Event:
    """An attempted tool call event."""
    return make_event("tool_call", tool, args or {}, call_id=f"id-{tool}", paths=paths)


def make_spec(**over: object) -> RunSpec:
    base = RunSpec(
        run_id="c1--full--r0--1",
        case_id="c1",
        arm=Arm("full", None),
        repeat_index=0,
        engine="v3",
        model_id="claude-haiku-5.5",
        cli_version="3.2.1",
        home_sha="abc",
        trust_tools=(),
        credits_max=0.2,
        wall_ms_max=120000,
    )
    return replace(base, **over)


def make_view(
    events: list[Event],
    final_text: str = "",
    status: RunStatus = "ok",
    cwd: str = "/tmp/evcwd-x",
    stderr_text: str = "",
) -> RunView:
    indexed = tuple(
        Event(i, e.kind, e.name, e.args_json, e.status, e.text, e.call_id, e.paths)
        for i, e in enumerate(events)
    )
    return RunView(make_spec(), status, indexed, final_text, stderr_text, "/tmp/evhome-x", cwd)


def fake_kiro(
    stdout_text: str,
    session_id: str,
    calls: list[tuple[str, str, str]],
    seen_argv: list[list[str]] | None = None,
    wall_ms: int = 100,
    stderr_text: str = "[INFO] fake\n",
) -> Invoker:
    """Replays a recorded stream and writes the matching session log into $KIRO_HOME."""

    def invoke(
        argv: list[str], env: Mapping[str, str], cwd: Path, timeout_s: float
    ) -> InvokeResult:
        assert timeout_s > 0, "the runner always passes a positive timeout"
        if seen_argv is not None:
            seen_argv.append(argv)
        if argv[1] == "--version":
            return InvokeResult("kiro-cli 3.2.1\n", "", 0, False, 1)
        write_session_log(Path(env["KIRO_HOME"]), session_id, calls)
        assert os.path.isdir(cwd), "the runner gives an existing cwd"
        return InvokeResult(stdout_text, stderr_text, 0, False, wall_ms)

    return invoke
