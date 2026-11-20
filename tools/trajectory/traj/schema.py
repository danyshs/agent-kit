"""Record types for the events file, and the one place their invariants are checked.

Nothing here can hold free text: strings are ids, hex hashes, clean names, enums or a day.
Field meanings are in design_draft.md, "Event file format".
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, fields
from functools import cache
from pathlib import Path
from typing import Final, cast, get_args, get_type_hints

from traj.common import HEX12_RE, NAME_RE, SESSION_ID_RE
from traj.limits import (
    EVENTS_LINES_MAX,
    OTHER,
    SCHEMA_VERSION,
    SESSION_LINE_BYTES_MAX,
    SHAPE_OPTIONS,
    SHAPE_PROGRAMS,
    SHAPE_SEGMENTS_MAX,
    SHAPE_SUBCOMMANDS,
    STATUS_VALUES,
    STEPS_PER_SESSION_MAX,
    TOOL_CLASSES,
    TREES,
    TURNS_PER_SESSION_MAX,
)

DAY_RE: Final = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class Segment:
    prog: str
    sub: str | None
    opts: tuple[str, ...]


@dataclass(frozen=True)
class Shape:
    segments: tuple[Segment, ...]
    has_heredoc: bool
    has_commit_trailer: bool
    truncated: bool


@dataclass(frozen=True)
class SessionRecord:
    schema_version: int
    reader_version: int
    rule_version: int
    harness: str
    tree: str
    session_id: str
    harness_version: str | None
    model_label: str | None
    workspace_hash: str
    workspace_label: str
    created_day: str
    source_bytes: int
    source_mtime_ms: int
    parse_errors: int
    malformed_events: int
    unknown_events: int
    events_used: int
    events_total: int
    unpaired_calls: int
    usage_conflicts: int
    negative_durations: int
    turns: int
    steps: int


@dataclass(frozen=True)
class TurnRecord:
    turn_index: int
    turn_key: str
    depth: int
    parent_turn_index: int | None
    started_ms: int | None
    ended_ms: int | None
    stop_reason: str | None
    steps: int
    errors: int
    model_calls: int | None
    credits: float | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_creation_tokens: int | None
    permission_wait_ms: int | None
    parallel_groups: int | None


@dataclass(frozen=True)
class StepRecord:
    step_index: int
    turn_index: int
    dispatch_group: int | None
    tool: str
    tool_class: str
    args_hash: str
    target_hash: str | None
    status: str
    exit_code: int | None
    started_ms: int | None
    duration_ms: int | None
    permission_wait_ms: int | None
    shape: Shape | None


@dataclass(frozen=True)
class SessionData:
    session: SessionRecord
    turns: tuple[TurnRecord, ...]
    steps: tuple[StepRecord, ...]


@dataclass(frozen=True)
class Finding:
    detector: str
    session_id: str
    harness: str
    turn_index: int
    first_step: int
    last_step: int
    count: int
    severity: str
    note: str


@dataclass(frozen=True)
class SessionRef:
    harness: str
    session_id: str
    tree: str
    path: Path
    sub_paths: tuple[Path, ...]
    cwds: tuple[str, ...]
    created_day: str
    skip_reason: str  # "" when the session is readable
    source_bytes: int
    source_mtime_ms: int


@dataclass(frozen=True)
class Skip:
    session_id: str
    harness: str
    reason: str


@dataclass(frozen=True)
class RequestUsage:
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_creation_tokens: int | None


@dataclass(frozen=True)
class RawCall:
    """One tool call as a reader saw it, before pairing. Hashes and clean names only."""

    call_id: str  # the harness id; pairing key, never written to the events file
    tool: str
    args_hash: str
    target_hash: str | None
    started_ms: int | None
    dispatch_group: int | None
    shape: Shape | None


@dataclass(frozen=True)
class RawResult:
    """One tool result as a reader saw it, before pairing."""

    call_id: str
    ended_ms: int | None
    status: str
    exit_code: int | None


# ---------------------------------------------------------------- encode


def encode_session(data: SessionData) -> list[str]:
    """Lines of the events file: session, turns, steps. Sorted keys, no spaces."""
    problem = check_session(data)
    assert problem is None, f"encode only valid data: {problem}"
    lines = [_dump("session", data.session)]
    lines.extend(_dump("turn", turn) for turn in data.turns)
    lines.extend(_dump("step", step) for step in data.steps)
    assert len(lines) == 1 + len(data.turns) + len(data.steps), "one line per record"
    return lines


def _dump(kind: str, record: SessionRecord | TurnRecord | StepRecord) -> str:
    body: dict[str, object] = {**asdict(record), "kind": kind}
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------- decode


class _Bad(Exception):
    """A line that does not fit its record type; the message is safe to print."""


def _keys(raw: dict[str, object], kind: str, names: Sequence[str]) -> None:
    want = {*names, "kind"}
    if raw.get("kind") != kind or set(raw) != want:
        raise _Bad(f"{kind} line has the wrong keys")


def _int(raw: dict[str, object], key: str) -> int:
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise _Bad(f"{key} is not an int")
    return value


def _int_or_none(raw: dict[str, object], key: str) -> int | None:
    return None if raw[key] is None else _int(raw, key)


def _str(raw: dict[str, object], key: str) -> str:
    value = raw[key]
    if not isinstance(value, str):
        raise _Bad(f"{key} is not a string")
    return value


def _str_or_none(raw: dict[str, object], key: str) -> str | None:
    return None if raw[key] is None else _str(raw, key)


def _bool(raw: dict[str, object], key: str) -> bool:
    value = raw[key]
    if not isinstance(value, bool):
        raise _Bad(f"{key} is not a bool")
    return value


def _float_or_none(raw: dict[str, object], key: str) -> float | None:
    value = raw[key]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _Bad(f"{key} is not a number")
    if not math.isfinite(value):
        raise _Bad(f"{key} is not finite")
    return float(value)


def _obj(value: object, what: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _Bad(f"{what} is not an object")
    return cast(dict[str, object], value)


def _decode_shape(value: object) -> Shape | None:
    if value is None:
        return None
    raw = _obj(value, "shape")
    if set(raw) != {"segments", "has_heredoc", "has_commit_trailer", "truncated"}:
        raise _Bad("shape has the wrong keys")
    items = raw["segments"]
    if not isinstance(items, list) or len(cast(list[object], items)) > SHAPE_SEGMENTS_MAX:
        raise _Bad("shape segments are not a short list")
    segments: list[Segment] = []
    for item in cast(list[object], items):
        seg = _obj(item, "segment")
        if set(seg) != {"prog", "sub", "opts"} or not isinstance(seg["opts"], list):
            raise _Bad("segment has the wrong keys")
        opts = tuple(str(o) for o in cast(list[object], seg["opts"]))
        segments.append(Segment(_str(seg, "prog"), _str_or_none(seg, "sub"), opts))
    return Shape(
        tuple(segments),
        _bool(raw, "has_heredoc"),
        _bool(raw, "has_commit_trailer"),
        _bool(raw, "truncated"),
    )


def _session_from(raw: dict[str, object]) -> SessionRecord:
    ints = (
        "schema_version reader_version rule_version source_bytes source_mtime_ms parse_errors "
        "malformed_events unknown_events events_used events_total unpaired_calls usage_conflicts "
        "negative_durations "
        "turns steps"
    ).split()
    strs = "harness tree session_id workspace_hash workspace_label created_day".split()
    opt_strs = ("harness_version", "model_label")
    _keys(raw, "session", [*ints, *strs, *opt_strs])
    i = {k: _int(raw, k) for k in ints}
    s = {k: _str(raw, k) for k in strs}
    return SessionRecord(
        schema_version=i["schema_version"],
        reader_version=i["reader_version"],
        rule_version=i["rule_version"],
        harness=s["harness"],
        tree=s["tree"],
        session_id=s["session_id"],
        harness_version=_str_or_none(raw, "harness_version"),
        model_label=_str_or_none(raw, "model_label"),
        workspace_hash=s["workspace_hash"],
        workspace_label=s["workspace_label"],
        created_day=s["created_day"],
        source_bytes=i["source_bytes"],
        source_mtime_ms=i["source_mtime_ms"],
        parse_errors=i["parse_errors"],
        malformed_events=i["malformed_events"],
        unknown_events=i["unknown_events"],
        events_used=i["events_used"],
        events_total=i["events_total"],
        unpaired_calls=i["unpaired_calls"],
        usage_conflicts=i["usage_conflicts"],
        negative_durations=i["negative_durations"],
        turns=i["turns"],
        steps=i["steps"],
    )


def _turn_from(raw: dict[str, object]) -> TurnRecord:
    names = (
        "turn_index turn_key depth parent_turn_index started_ms ended_ms stop_reason steps errors "
        "model_calls credits input_tokens output_tokens cache_read_tokens cache_creation_tokens "
        "permission_wait_ms parallel_groups"
    ).split()
    _keys(raw, "turn", names)
    return TurnRecord(
        turn_index=_int(raw, "turn_index"),
        turn_key=_str(raw, "turn_key"),
        depth=_int(raw, "depth"),
        parent_turn_index=_int_or_none(raw, "parent_turn_index"),
        started_ms=_int_or_none(raw, "started_ms"),
        ended_ms=_int_or_none(raw, "ended_ms"),
        stop_reason=_str_or_none(raw, "stop_reason"),
        steps=_int(raw, "steps"),
        errors=_int(raw, "errors"),
        model_calls=_int_or_none(raw, "model_calls"),
        credits=_float_or_none(raw, "credits"),
        input_tokens=_int_or_none(raw, "input_tokens"),
        output_tokens=_int_or_none(raw, "output_tokens"),
        cache_read_tokens=_int_or_none(raw, "cache_read_tokens"),
        cache_creation_tokens=_int_or_none(raw, "cache_creation_tokens"),
        permission_wait_ms=_int_or_none(raw, "permission_wait_ms"),
        parallel_groups=_int_or_none(raw, "parallel_groups"),
    )


def _step_from(raw: dict[str, object]) -> StepRecord:
    names = (
        "step_index turn_index dispatch_group tool tool_class args_hash target_hash status "
        "exit_code started_ms duration_ms permission_wait_ms shape"
    ).split()
    _keys(raw, "step", names)
    return StepRecord(
        step_index=_int(raw, "step_index"),
        turn_index=_int(raw, "turn_index"),
        dispatch_group=_int_or_none(raw, "dispatch_group"),
        tool=_str(raw, "tool"),
        tool_class=_str(raw, "tool_class"),
        args_hash=_str(raw, "args_hash"),
        target_hash=_str_or_none(raw, "target_hash"),
        status=_str(raw, "status"),
        exit_code=_int_or_none(raw, "exit_code"),
        started_ms=_int_or_none(raw, "started_ms"),
        duration_ms=_int_or_none(raw, "duration_ms"),
        permission_wait_ms=_int_or_none(raw, "permission_wait_ms"),
        shape=_decode_shape(raw["shape"]),
    )


def _parse_line(line: str) -> dict[str, object]:
    try:
        value: object = json.loads(line)
    except ValueError as err:
        raise _Bad("a line is not JSON") from err
    return _obj(value, "line")


def decode_session_line(line: str) -> SessionRecord | str:
    """The session line alone, validated as a session line, or why it is not. For callers that
    only need the stored versions and source stamps (needs_rebuild)."""
    assert SESSION_LINE_BYTES_MAX > 0, "the line bound is positive"
    if len(line) > SESSION_LINE_BYTES_MAX:
        return "session line is over its size bound"
    try:
        record = _session_from(_parse_line(line))
    except _Bad as err:
        return str(err)
    problem = _check_types(record) or _check_session_line(record)
    return record if problem is None else problem


def decode_session(lines: Sequence[str]) -> SessionData | str:
    """The records in an events file, or why it is not valid. Checks every invariant."""
    if not 1 <= len(lines) <= EVENTS_LINES_MAX:
        return "events file has no lines or too many"
    try:
        session = _session_from(_parse_line(lines[0]))
        if session.schema_version != SCHEMA_VERSION:
            return f"schema_version {session.schema_version} is not {SCHEMA_VERSION}"
        if len(lines) != 1 + session.turns + session.steps:
            return "line count does not match the session line"
        turns = tuple(_turn_from(_parse_line(x)) for x in lines[1 : 1 + session.turns])
        steps = tuple(_step_from(_parse_line(x)) for x in lines[1 + session.turns :])
    except _Bad as err:
        return str(err)
    data = SessionData(session, turns, steps)
    problem = check_session(data)
    return data if problem is None else problem


# ---------------------------------------------------------------- invariants


def _nonneg(label: str, value: int | float | None) -> str | None:
    if value is not None and value < 0:
        return f"{label} is negative"
    return None


def _name_ok(value: str | None) -> bool:
    return value is None or value == OTHER or NAME_RE.fullmatch(value) is not None


_SCALARS: Final = (int, str, float)


@cache
def scalar_fields(cls: type) -> tuple[tuple[str, type, bool], ...]:
    """(name, scalar type, may be None) for each int, str or float field of a record class,
    read from the real annotations. A nested record or a tuple field is left out."""
    hints = get_type_hints(cls)
    found: list[tuple[str, type, bool]] = []
    for spec in fields(cls):
        hint = hints[spec.name]
        parts = get_args(hint)
        optional = type(None) in parts
        base = next((p for p in parts if p is not type(None)), hint) if parts else hint
        if base in _SCALARS:
            found.append((spec.name, cast(type, base), optional))
    assert found, "a record class has scalar fields"
    return tuple(found)


def _check_types(record: SessionRecord | TurnRecord | StepRecord) -> str | None:
    """Exact types for the scalar fields: no bool as int, no None in a required field, no
    NaN or infinity."""
    for name, want, optional in scalar_fields(type(record)):
        value = getattr(record, name)
        if value is None:
            if not optional:
                return f"{name} is required"
        elif type(value) is not want:
            return f"{name} has the wrong type"
        elif want is float and not math.isfinite(cast(float, value)):
            return f"{name} is not finite"
    return None


def _check_session_line(rec: SessionRecord) -> str | None:
    # only the schema version is a load invariant; reader and rule versions are for needs_rebuild,
    # so a later bump never makes a session with an expired raw log unreadable
    if rec.schema_version != SCHEMA_VERSION:
        return "schema_version is not the current one"
    if rec.harness not in TREES:
        return "harness is not in the enum"
    if rec.tree not in TREES[rec.harness]:
        return "tree is not valid for the harness"
    if SESSION_ID_RE.fullmatch(rec.session_id) is None:
        return "session_id is not a uuid"
    if HEX12_RE.fullmatch(rec.workspace_hash) is None:
        return "workspace_hash is not 12 hex"
    if not all(_name_ok(v) for v in (rec.workspace_label, rec.harness_version, rec.model_label)):
        return "a label is not a clean name"
    if rec.created_day and DAY_RE.fullmatch(rec.created_day) is None:
        return "created_day is not a day"
    counts = (
        rec.reader_version,
        rec.rule_version,
        rec.source_bytes,
        rec.source_mtime_ms,
        rec.parse_errors,
        rec.malformed_events,
        rec.unknown_events,
        rec.events_used,
        rec.events_total,
        rec.unpaired_calls,
        rec.usage_conflicts,
        rec.negative_durations,
    )
    if min(counts) < 0 or rec.turns < 0 or rec.steps < 0:
        return "a session count is negative"
    if rec.malformed_events > rec.events_used or (
        rec.events_used + rec.unknown_events > rec.events_total
    ):
        return "event counts do not nest (malformed, used, unknown, total)"
    if rec.turns > TURNS_PER_SESSION_MAX or rec.steps > STEPS_PER_SESSION_MAX:
        return "a session has more turns or steps than the bounds allow"
    return None


def _check_turn_nulls(turn: TurnRecord, harness: str) -> str | None:
    tokens = (
        turn.input_tokens,
        turn.output_tokens,
        turn.cache_read_tokens,
        turn.cache_creation_tokens,
    )
    assert harness in TREES, "known harness"
    if turn.depth not in (0, 1):
        return "depth is not 0 or 1"
    if (
        harness == "kiro"
        and turn.depth == 1
        and (turn.model_calls, turn.stop_reason) != (None, None)
    ):
        return "a Kiro depth 1 turn has no model_calls or stop_reason"
    if (turn.parent_turn_index is not None) and (turn.depth != 1 or harness != "kiro"):
        return "only a Kiro depth 1 turn has a parent"
    if turn.credits is not None and (harness != "kiro" or turn.depth != 0):
        return "credits only on Kiro depth 0 turns"
    if harness == "kiro" and any(t is not None for t in tokens):
        return "Kiro turns carry no tokens"
    if harness == "claude" and turn.permission_wait_ms is not None:
        return "Claude turns carry no permission wait"
    if harness == "kiro" and turn.parallel_groups is not None:
        return "Kiro turns carry no parallel groups"
    if not _name_ok(turn.stop_reason):
        return "stop_reason is not a clean name"
    numbers = (
        turn.model_calls,
        turn.credits,
        turn.permission_wait_ms,
        turn.parallel_groups,
        turn.started_ms,
        turn.ended_ms,
        turn.turn_index,
        turn.steps,
        turn.errors,
    )
    return next((m for m in (_nonneg("a turn number", n) for n in (*numbers, *tokens)) if m), None)


def _check_turns(data: SessionData) -> str | None:
    assert len(data.turns) <= TURNS_PER_SESSION_MAX, "the caller bounded the turns"
    steps_by_turn = [0] * len(data.turns)
    errors_by_turn = [0] * len(data.turns)
    for step in data.steps:
        if not 0 <= step.turn_index < len(data.turns):
            return "a step turn_index is out of range"
        steps_by_turn[step.turn_index] += 1
        errors_by_turn[step.turn_index] += step.status == "error"
    for position, turn in enumerate(data.turns):
        if turn.turn_index != position:
            return "turn_index is not dense"
        if HEX12_RE.fullmatch(turn.turn_key) is None:
            return "turn_key is not 12 hex"
        problem = _check_turn_nulls(turn, data.session.harness)
        if problem:
            return problem
        parent = turn.parent_turn_index
        if parent is not None and not (
            0 <= parent < len(data.turns) and data.turns[parent].depth == 0
        ):
            return "parent_turn_index is not a depth 0 turn"
        if (turn.steps, turn.errors) != (steps_by_turn[position], errors_by_turn[position]):
            return "turn steps or errors do not match its step lines"
    return None


def _check_shape(shape: object) -> str | None:
    """Reason a shape is invalid, or None. Takes any object: a non-Shape gets a reason."""
    if not isinstance(shape, Shape):
        return "shape is not a Shape"
    if not isinstance(cast(object, shape.segments), tuple):
        return "shape segments are not a tuple"
    flags = (shape.has_heredoc, shape.has_commit_trailer, shape.truncated)
    if any(type(flag) is not bool for flag in flags):
        return "a shape flag is not a bool"
    if len(shape.segments) > SHAPE_SEGMENTS_MAX:
        return "shape has too many segments"
    for item in shape.segments:
        seg = cast(object, item)
        if not isinstance(seg, Segment):
            return "a shape segment is not a Segment"
        if type(seg.prog) is not str or not (seg.sub is None or type(seg.sub) is str):
            return "a shape segment has the wrong types"
        if not isinstance(cast(object, seg.opts), tuple) or not all(
            type(opt) is str for opt in seg.opts
        ):
            return "a shape option is not a string"
        if seg.prog != OTHER and seg.prog not in SHAPE_PROGRAMS:
            return "shape prog is outside the vocabulary"
        if seg.sub is not None and seg.sub not in SHAPE_SUBCOMMANDS.get(seg.prog, frozenset()):
            return "shape sub is outside the vocabulary"
        if not set(seg.opts) <= SHAPE_OPTIONS or list(seg.opts) != sorted(set(seg.opts)):
            return "shape opts are outside the vocabulary or not sorted and unique"
    return None


def _check_step(step: StepRecord, harness: str) -> str | None:
    assert harness in TREES, "known harness"
    if step.tool_class not in TOOL_CLASSES:
        return "tool_class is not in the enum"
    if step.status not in STATUS_VALUES:
        return "status is not in the enum"
    if not _name_ok(step.tool) or HEX12_RE.fullmatch(step.args_hash) is None:
        return "tool or args_hash is not clean"
    if step.target_hash is not None and (
        step.tool_class not in ("read", "write") or HEX12_RE.fullmatch(step.target_hash) is None
    ):
        return "target_hash is only for read and write steps, as 12 hex"
    if step.shape is not None and step.tool_class != "shell":
        return "shape is only for shell steps"
    if step.exit_code is not None and (step.tool_class != "shell" or harness != "kiro"):
        return "exit_code is only for Kiro shell steps"
    if harness == "kiro" and step.dispatch_group is not None:
        return "Kiro steps carry no dispatch_group"
    if harness == "claude" and step.permission_wait_ms is not None:
        return "Claude steps carry no permission wait"
    if step.status == "unpaired" and step.duration_ms is not None:
        return "an unpaired step has no duration"
    numbers = (
        step.dispatch_group,
        step.started_ms,
        step.duration_ms,
        step.permission_wait_ms,
        step.exit_code,
        step.step_index,
        step.turn_index,
    )
    problem = next((m for m in (_nonneg("a step number", n) for n in numbers) if m), None)
    return problem or (_check_shape(step.shape) if step.shape is not None else None)


def check_session(data: SessionData) -> str | None:
    """None when every invariant holds, else the first violation (safe to print)."""
    if not isinstance(cast(object, data.turns), tuple) or not isinstance(
        cast(object, data.steps), tuple
    ):
        return "turns and steps are not tuples"
    problem = _check_types(data.session) or _check_session_line(data.session)
    if problem:
        return problem
    if data.session.turns != len(data.turns) or data.session.steps != len(data.steps):
        return "session counts do not match the records"
    # the line bound the counts, so the loops below are bounded too
    assert len(data.turns) <= TURNS_PER_SESSION_MAX, "turns are bounded"
    assert len(data.steps) <= STEPS_PER_SESSION_MAX, "steps are bounded"
    # a denied or cancelled call with no result counts as a call but not an unpaired step;
    # the upper bound covers all statuses that can arise when result is None
    unpaired_steps = sum(s.status == "unpaired" for s in data.steps)
    no_result_steps = sum(s.status in ("unpaired", "denied", "cancelled") for s in data.steps)
    if not unpaired_steps <= data.session.unpaired_calls <= no_result_steps:
        return "unpaired_calls does not fit the unpaired steps"
    for turn in data.turns:
        problem = _check_types(turn)
        if problem:
            return problem
    for position, step in enumerate(data.steps):
        if step.step_index != position:
            return "step_index is not dense"
        problem = _check_types(step) or _check_step(step, data.session.harness)
        if problem:
            return problem
    return _check_turns(data)
