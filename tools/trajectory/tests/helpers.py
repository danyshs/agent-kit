"""Builders for in-memory records. Tests change one field with dataclasses.replace."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from traj import limits
from traj.limits import READER_VERSION, RULE_VERSION, SCHEMA_VERSION
from traj.report import Coverage
from traj.schema import (
    Segment,
    SessionData,
    SessionRecord,
    Shape,
    StepRecord,
    TurnRecord,
    encode_session,
)

SESSION_ID = "0a1b2c3d-0000-4000-8000-123456789abc"
OTHER_SESSION_ID = "ffffffff-1111-4222-8333-444444444444"
HEX = "0123456789ab"
# has a space, which NAME_RE rejects: a canary that matched NAME_RE would survive clean_name
CANARY = "CANARY 0xDEADBEEF"


def make_session_line(
    turns: int, steps: int, harness: str = "kiro", session_id: str = SESSION_ID
) -> SessionRecord:
    return SessionRecord(
        schema_version=SCHEMA_VERSION,
        reader_version=READER_VERSION,
        rule_version=RULE_VERSION,
        harness=harness,
        tree="interactive" if harness == "kiro" else "main",
        session_id=session_id,
        harness_version="1.0.0",
        model_label=None,
        workspace_hash=HEX,
        workspace_label="myrepo",
        created_day="2026-11-14",
        source_bytes=1234,
        source_mtime_ms=1_763_000_000_000,
        parse_errors=0,
        malformed_events=0,
        unknown_events=0,
        events_used=0,
        events_total=0,
        unpaired_calls=0,
        usage_conflicts=0,
        negative_durations=0,
        turns=turns,
        steps=steps,
    )


def make_turn(index: int, depth: int, steps: int, errors: int, parent: int | None) -> TurnRecord:
    return TurnRecord(
        turn_index=index,
        turn_key=HEX,
        depth=depth,
        parent_turn_index=parent,
        started_ms=1_000 * index,
        ended_ms=1_000 * index + 500,
        stop_reason="end_turn" if depth == 0 else None,
        steps=steps,
        errors=errors,
        model_calls=2 if depth == 0 else None,
        credits=0.25 if depth == 0 else None,
        input_tokens=None,
        output_tokens=None,
        cache_read_tokens=None,
        cache_creation_tokens=None,
        permission_wait_ms=None,
        parallel_groups=None,
    )


def make_step(index: int, turn: int, tool_class: str, status: str) -> StepRecord:
    shell = tool_class == "shell"
    shape = Shape((Segment("git", "commit", ("-m",)),), False, False, False) if shell else None
    return StepRecord(
        step_index=index,
        turn_index=turn,
        dispatch_group=None,
        tool="execute_bash" if shell else "read_file",
        tool_class=tool_class,
        args_hash=HEX,
        target_hash=None if shell else HEX,
        status=status,
        exit_code=1 if shell and status == "error" else None,
        started_ms=100 * index,
        duration_ms=None if status == "unpaired" else 5,
        permission_wait_ms=None,
        shape=shape,
    )


def worst_line_bytes() -> tuple[int, int]:
    """(turn line, step line) bytes at their largest valid content, newline included, over both
    harnesses. 19-digit ints (int64 max), 64-char names, 16 full segments. Index fields are
    counted at 7 digits: one-record data cannot hold a real index."""
    big = 2**63 - 1
    word = "a" * limits.NAME_CHARS_MAX
    shape = Shape(
        tuple(
            Segment("xcodebuild", None, tuple(sorted(limits.SHAPE_OPTIONS)))
            for _ in range(limits.SHAPE_SEGMENTS_MAX)
        ),
        True,
        True,
        True,
    )
    index_slack = 2 * 7  # two index fields per line
    turn_max = step_max = 0
    for harness in limits.HARNESSES:
        kiro = harness == "kiro"
        turn = make_turn(0, 0, 1, 0, None)
        turn = replace(
            turn,
            started_ms=big,
            ended_ms=big,
            stop_reason=word,
            model_calls=big,
            credits=1.2345678901234567e300 if kiro else None,
            input_tokens=None if kiro else big,
            output_tokens=None if kiro else big,
            cache_read_tokens=None if kiro else big,
            cache_creation_tokens=None if kiro else big,
            permission_wait_ms=big if kiro else None,
            parallel_groups=None if kiro else big,
        )
        step = replace(
            make_step(0, 0, "shell", "ok"),
            tool=word,
            dispatch_group=None if kiro else big,
            exit_code=big if kiro else None,
            started_ms=big,
            duration_ms=big,
            permission_wait_ms=big if kiro else None,
            shape=shape,
        )
        line = replace(
            make_session_line(1, 1, harness),
            workspace_label=word,
            harness_version=word,
            model_label=word,
        )
        lines = encode_session(SessionData(line, (turn,), (step,)))
        turn_max = max(turn_max, len(lines[1]) + 1 + index_slack)
        step_max = max(step_max, len(lines[2]) + 1 + index_slack)
    return turn_max, step_max


def kiro_event(kind: str, ms: int, **payload: object) -> dict[str, object]:
    """One Kiro log line. `ms` is an offset in milliseconds from 2026-11-14T10:00:00Z."""
    stamp = datetime(2026, 11, 14, 10, 0, 0, tzinfo=UTC) + timedelta(milliseconds=ms)
    text = stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond // 1000:03d}Z"
    return {"id": "x", "payload": {"type": kind, **payload}, "timestamp": text}


def kiro_call(
    call_id: str, tool: str, ms: int, status: str = "approved", **args: object
) -> dict[str, object]:
    return kiro_event(
        "tool_call",
        ms,
        toolCallId=call_id,
        toolName=tool,
        args=args,
        executionId="e1",
        status=status,
    )


def kiro_result(
    call_id: str, ms: int, success: bool = True, content: str = "ok"
) -> dict[str, object]:
    return kiro_event(
        "tool_result", ms, toolCallId=call_id, success=success, content=content, executionId="e1"
    )


def write_kiro_session(
    root: Path,
    session_id: str,
    events: Sequence[dict[str, object] | str],
    meta: dict[str, object] | None = None,
    subs: dict[str, Sequence[dict[str, object]]] | None = None,
) -> Path:
    """A `sess_<id>` directory under `<root>/<group>/`; returns the session directory. A str
    event is written as a raw line. `meta=None` writes a normal session.json."""
    sess = root / "aaaaaaaaaaaaaaaa" / f"sess_{session_id}"
    sess.mkdir(parents=True)
    base: dict[str, object] = {
        "agentMode": "vibe",
        "schemaVersion": "1.0.0",
        "dataModelVersion": 1,
        "createdAt": "2026-11-14T10:00:00.000Z",
        "modelId": "auto",
        "workspacePaths": ["/home/dev/proj"],
        "rootPaths": [],
    }
    (sess / "session.json").write_text(json.dumps(meta if meta is not None else base))
    lines = [e if isinstance(e, str) else json.dumps(e) for e in events]
    (sess / "messages.jsonl").write_text("\n".join(lines) + "\n")
    for name, sub_events in (subs or {}).items():
        (sess / "sub-executions").mkdir(exist_ok=True)
        text = "\n".join(json.dumps(e) for e in sub_events) + "\n"
        (sess / "sub-executions" / f"{name}.jsonl").write_text(text)
    return sess


def make_coverage(**changes: int) -> Coverage:
    """Sound coverage: one scanned session, no skips, 10 used of 20 events."""
    base = Coverage(
        seen=1,
        scanned_from_raw=1,
        not_built_yet=0,
        skipped=tuple((reason, 0) for reason in limits.SKIP_REASONS),
        events_not_written=0,
        events_without_raw_log=0,
        filtered_out=0,
        malformed_events=0,
        unknown_events=0,
        parse_errors=0,
        unpaired_calls=0,
        usage_conflicts=0,
        negative_durations=0,
        truncated_findings_sessions=0,
        zero_step_sessions=0,
        events_used=10,
        events_total=20,
    )
    return replace(base, **changes)


def make_data(session_id: str = SESSION_ID) -> SessionData:
    """A Kiro session. Turn 0 (depth 0) has two steps, one an error; turn 1 (depth 1) has one."""
    steps = (
        make_step(0, 0, "read", "ok"),
        make_step(1, 0, "shell", "error"),
        make_step(2, 1, "shell", "ok"),
    )
    turns = (make_turn(0, 0, 2, 1, None), make_turn(1, 1, 1, 0, 0))
    return SessionData(make_session_line(2, 3, "kiro", session_id), turns, steps)
