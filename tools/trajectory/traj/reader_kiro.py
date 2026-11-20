"""Kiro reader: lists sessions under ~/.kiro/sessions and turns one into SessionData.

Raw text is read here and nowhere else. Text-bearing events (`assistant`, `user`) are never
inspected beyond their type; tool args are hashed and dropped. Rules: design_draft.md, "Kiro
reader" and "Status mapping". Shell command shapes arrive in stage 5, so a shell step's shape is
null here.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace
from itertools import islice
from pathlib import Path

from traj.common import (
    HEX12_RE,
    SESSION_ID_RE,
    args_hash,
    as_dict,
    as_list,
    as_str,
    clean_name,
    exit_code_from_tail,
    is_eval_traffic,
    parse_iso_ms,
    read_events,
    short_hash,
)
from traj.limits import (
    CWDS_PER_SESSION_MAX,
    KIRO_DATA_MODEL_VERSIONS,
    KIRO_IGNORED_TYPES,
    KIRO_SCHEMA_VERSIONS,
    READER_VERSION,
    REQUIRED_KIRO_KEYS,
    RULE_VERSION,
    SCHEMA_VERSION,
    SESSION_JSON_BYTES_MAX,
    SKIP_REASONS,
    STEPS_PER_SESSION_MAX,
    SUB_FILES_MAX,
    SUMMARIES_PER_EVENT_MAX,
    TOOL_CLASS,
)
from traj.schema import (
    DAY_RE,
    SessionData,
    SessionRecord,
    SessionRef,
    Skip,
    StepRecord,
    TurnRecord,
)

HEADLESS, UNREADABLE, NO_SESSION_JSON, NO_MESSAGES, EVAL, NO_CWD, UNKNOWN_FORMAT, OVER_BOUND = (
    SKIP_REASONS
)
EVAL_ROOT_NAMES = ("/tmp",)  # the miner's value; resolved at call time (macOS: /private/tmp)
TARGET_KEYS = ("path", "file_path", "targetFile")
DAY_LENGTH = 10


# ---------------------------------------------------------------- discovery


def _list_bounded(directory: Path, cap: int) -> list[Path] | str:
    """Sorted children of a directory, or the reason it has more than `cap`."""
    assert cap > 0, "the cap is positive"
    try:
        children = list(islice(directory.iterdir(), cap + 1))
    except OSError as err:
        return f"cannot list {directory.name}: {err.strerror}"
    if len(children) > cap:
        return f"{directory.name} has more than {cap} entries"
    return sorted(children)


def _read_meta(path: Path) -> dict[str, object] | None:
    """session.json as an object, or None when it cannot be read, is over its bound, is not JSON
    or is not an object."""
    try:
        with path.open("rb") as handle:
            data = handle.read(SESSION_JSON_BYTES_MAX + 1)
        if len(data) > SESSION_JSON_BYTES_MAX:
            return None
        value: object = json.loads(data.decode("utf-8"))
    except (OSError, ValueError):
        return None
    result = as_dict(value)
    return result if isinstance(value, dict) else None


def _sources(paths: list[Path]) -> tuple[int, int] | None:
    """(summed bytes, newest mtime in epoch ms) over the files, or None if one cannot be stat'ed."""
    total, newest = 0, 0
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            return None
        total += stat.st_size
        newest = max(newest, stat.st_mtime_ns // 1_000_000)
    return total, newest


def _placeholder(session_id: str, path: Path, reason: str, tree: str = "interactive") -> SessionRef:
    """A listed session that will not be read; only its id and reason matter."""
    assert reason in SKIP_REASONS, "a known reason"
    return SessionRef("kiro", clean_name(session_id), tree, path, (), (), "", reason, 0, 0)


def _cwds_of(meta: dict[str, object]) -> tuple[str, ...]:
    raw = [*as_list(meta.get("workspacePaths")), *as_list(meta.get("rootPaths"))]
    texts = [as_str(item) for item in raw[:CWDS_PER_SESSION_MAX]]
    return tuple(text for text in texts if text)


def _sub_files(sess: Path) -> list[Path] | str:
    """The sub-execution logs of a session, or why they cannot be listed (over bound or not
    readable)."""
    directory = sess / "sub-executions"
    if not directory.is_dir():
        return []
    listed = _list_bounded(directory, SUB_FILES_MAX)
    return listed if isinstance(listed, str) else [p for p in listed if p.suffix == ".jsonl"]


def _first_reason(
    meta: dict[str, object],
    cwds: tuple[str, ...],
    eval_roots: list[Path],
    *,
    has_messages: bool,
    stats_ok: bool,
    subs_problem: str | None,
) -> str:
    """The first applicable skip reason in SKIP_REASONS order, or "". `over bound` is last, so
    a session that is both eval traffic and has too many sub files is eval traffic."""
    if not stats_ok or (subs_problem is not None and subs_problem.startswith("cannot")):
        return UNREADABLE
    if not has_messages:
        return NO_MESSAGES
    if is_eval_traffic(cwds, eval_roots):
        return EVAL
    if not cwds:
        return NO_CWD
    if not _known_format(meta):
        return UNKNOWN_FORMAT
    return OVER_BOUND if subs_problem is not None else ""


def _session_ref(sess: Path, eval_roots: list[Path]) -> SessionRef:
    """One `sess_<uuid>` directory as a ref. The reason is the first that applies; a read can
    still find `over bound` later."""
    session_id = sess.name.removeprefix("sess_")
    if SESSION_ID_RE.fullmatch(session_id) is None:
        return _placeholder(session_id, sess, UNREADABLE)
    meta_path, messages = sess / "session.json", sess / "messages.jsonl"
    if not meta_path.is_file():
        return _placeholder(session_id, messages, NO_SESSION_JSON)
    meta = _read_meta(meta_path)
    if meta is None:
        return _placeholder(session_id, messages, UNREADABLE)
    tree = "workflow" if as_str(meta.get("agentMode")).startswith("wf-") else "interactive"
    subs = _sub_files(sess)
    sub_paths = tuple(subs) if isinstance(subs, list) else ()
    stats = _sources([messages, meta_path, *sub_paths]) if messages.is_file() else None
    cwds = _cwds_of(meta)
    reason = _first_reason(
        meta,
        cwds,
        eval_roots,
        has_messages=messages.is_file(),
        stats_ok=stats is not None or not messages.is_file(),
        subs_problem=subs if isinstance(subs, str) else None,
    )
    created = as_str(meta.get("createdAt"))[:DAY_LENGTH]
    day = created if DAY_RE.fullmatch(created) else ""
    size, mtime = stats or (0, 0)
    return SessionRef("kiro", session_id, tree, messages, sub_paths, cwds, day, reason, size, mtime)


def _known_format(meta: dict[str, object]) -> bool:
    version, model = meta.get("schemaVersion"), meta.get("dataModelVersion")
    return (
        isinstance(version, str)
        and version in KIRO_SCHEMA_VERSIONS
        and type(model) is int
        and model in KIRO_DATA_MODEL_VERSIONS
    )


def find_kiro_sessions(sessions_dir: Path, sessions_max: int) -> list[SessionRef] | str:
    """Every session under the store, skipped ones included with their reason, or the reason the
    store cannot be listed. One bad session never aborts the listing."""
    assert sessions_max > 0, "a positive bound"
    if not sessions_dir.is_dir():
        return f"{sessions_dir.name} is not a directory"
    groups = _list_bounded(sessions_dir, 2 * sessions_max)
    if isinstance(groups, str):
        return groups
    eval_roots = [Path(root).resolve() for root in EVAL_ROOT_NAMES]
    refs: list[SessionRef] = []
    for group in (g for g in groups if g.is_dir()):
        children = _list_bounded(group, 2 * sessions_max)
        if isinstance(children, str):
            return children
        if group.name == "cli":
            refs.extend(_placeholder(p.stem, p, HEADLESS) for p in children if p.suffix == ".jsonl")
        else:
            refs.extend(
                _session_ref(p, eval_roots)
                for p in children
                if p.name.startswith("sess_") and p.is_dir()
            )
        if len(refs) > sessions_max:
            return f"over {sessions_max} sessions"
    assert len(refs) <= sessions_max, "bound held"
    return refs


# ---------------------------------------------------------------- scanning one file


@dataclass(frozen=True)
class _Call:
    call_id: str
    exec_id: str
    sub_id: str  # "" when the event has no subExecutionId
    tool: str
    tool_class: str
    args_hash: str
    target_hash: str | None
    started_ms: int | None
    status_raw: str


@dataclass(frozen=True)
class _Result:
    ended_ms: int | None
    ok: bool
    exit_code: int | None


@dataclass
class _Exec:
    """One depth 0 turn while the main file is scanned."""

    first_ms: int | None
    last_ms: int | None
    start_ms: int | None = None
    has_start: bool = False  # a turn_start was seen; its time may still be unknown (null)
    end_ms: int | None = None
    has_end: bool = False
    stop_reason: str | None = None
    credits: float | None = None
    model_calls: int = 0
    usage_events: int = 0
    requests_missing: bool = False


@dataclass
class _Scan:
    """Scratch state of one file (module-private, no methods)."""

    events_total: int = 0
    events_used: int = 0
    malformed: int = 0
    unknown: int = 0
    first_ms: int | None = None
    last_ms: int | None = None
    calls_max: int = STEPS_PER_SESSION_MAX  # what is left of the session's step budget
    over_bound: bool = False  # a bound was crossed while scanning; the session fails closed
    calls: list[_Call] = field(default_factory=lambda: [])
    results: dict[str, _Result] = field(default_factory=lambda: {})
    pending_ms: dict[str, int | None] = field(default_factory=lambda: {})
    waits: dict[str, int | None] = field(default_factory=lambda: {})
    execs: dict[str, _Exec] = field(default_factory=lambda: {})


def _nonempty(payload: dict[str, object], *keys: str) -> bool:
    return all(isinstance(payload.get(k), str) and payload.get(k) != "" for k in keys)


def _target_hash(
    tool_class: str, args: dict[str, object], payload: dict[str, object]
) -> str | None:
    """12 hex of the first non-empty string path-like value. List and object values are skipped."""
    if tool_class not in ("read", "write"):
        return None
    for value in (*(args.get(k) for k in TARGET_KEYS), payload.get("filePath")):
        if isinstance(value, str) and value:
            return short_hash(value)
    return None


def _touch(scan: _Scan, exec_id: str, ms: int | None, *, main: bool) -> _Exec | None:
    """The turn accumulator for an execution, created on first sight (main file only)."""
    if not main:
        return None
    acc = scan.execs.get(exec_id)
    if acc is None:
        acc = scan.execs[exec_id] = _Exec(ms, ms)
    acc.last_ms = ms  # the last event's own time; unknown stays unknown
    return acc


def _add_credits(acc: _Exec, payload: dict[str, object]) -> bool:
    """Add one usage_summary to the turn. False when it has more summaries than the bound: the
    credits would be understated, so the caller fails the session closed."""
    summaries = as_list(payload.get("promptTurnSummaries"))
    if len(summaries) > SUMMARIES_PER_EVENT_MAX:
        return False
    found = [
        float(usage)
        for item in summaries
        if isinstance(usage := as_dict(item).get("usage"), int | float)
        and not isinstance(usage, bool)
        and math.isfinite(usage)
        and usage >= 0
    ]
    if found:
        acc.credits = (acc.credits or 0.0) + sum(found)
    acc.usage_events += 1
    if isinstance(payload.get("requestIds"), list):
        acc.model_calls += len(as_list(payload.get("requestIds")))
    else:
        acc.requests_missing = True
    return True


def _on_call(
    scan: _Scan, payload: dict[str, object], ms: int | None, seen: set[str], main: bool
) -> None:
    exec_id = as_str(payload.get("executionId"))
    _touch(scan, exec_id, ms, main=main)
    call_id = as_str(payload.get("toolCallId"))
    if call_id in seen:
        return  # a repeated call counts once, the first
    seen.add(call_id)
    if len(scan.calls) >= scan.calls_max:
        scan.over_bound = True  # nothing is retained past the budget
        return
    args = as_dict(payload.get("args"))
    tool = clean_name(as_str(payload.get("toolName")))
    tool_class = TOOL_CLASS.get(tool, "other")
    scan.calls.append(
        _Call(
            call_id,
            exec_id,
            as_str(payload.get("subExecutionId")),
            tool,
            tool_class,
            args_hash(args),
            _target_hash(tool_class, args, payload),
            ms,
            as_str(payload.get("status")),
        )
    )


def _on_result(scan: _Scan, payload: dict[str, object], ms: int | None, main: bool) -> None:
    _touch(scan, as_str(payload.get("executionId")), ms, main=main)
    code = exit_code_from_tail(as_str(payload.get("content")))
    scan.results.setdefault(
        as_str(payload.get("toolCallId")), _Result(ms, payload.get("success") is not False, code)
    )


def _on_permission(scan: _Scan, kind: str, payload: dict[str, object], ms: int | None) -> None:
    """First pending pairs with the first later resolved of the same call; nothing else pairs."""
    call_id = as_str(payload.get("toolCallId"))
    if kind == "pending_interaction":
        scan.pending_ms.setdefault(call_id, ms)
    elif call_id in scan.pending_ms and call_id not in scan.waits:
        began = scan.pending_ms[call_id]
        scan.waits[call_id] = None if ms is None or began is None else max(0, ms - began)


def _on_turn_event(
    scan: _Scan, kind: str, payload: dict[str, object], ms: int | None, main: bool
) -> None:
    exec_id = as_str(payload.get("executionId"))
    if kind == "usage_summary":
        acc = _touch(scan, exec_id, ms, main=main)
        if acc is not None and not _add_credits(acc, payload):
            scan.over_bound = True
    elif kind == "turn_start":
        acc = _touch(scan, exec_id, ms, main=main)
        if acc is not None:
            acc.start_ms, acc.has_start = ms, True
    elif main and exec_id in scan.execs:  # a turn_end alone never creates a turn
        acc = _touch(scan, exec_id, ms, main=main)
        assert acc is not None, "main file"
        acc.end_ms, acc.has_end = ms, True
        acc.stop_reason = clean_name(as_str(payload.get("stopReason")))


_ID_NAMES = ("toolCallId", "executionId")
# derived from REQUIRED_KIRO_KEYS so the two cannot drift: the id keys must be non-empty strings.
# Stricter than the doc's event table, which names the keys but not which are ids (stage note).
ID_KEYS = {
    kind: tuple(k for k in keys if k in _ID_NAMES) for kind, keys in REQUIRED_KIRO_KEYS.items()
}


def _scan_event(scan: _Scan, event: dict[str, object], seen: set[str], main: bool) -> None:
    scan.events_total += 1
    assert scan.events_used + scan.unknown <= scan.events_total, "counters nest"
    payload = as_dict(event.get("payload"))
    kind = as_str(payload.get("type"))
    ms = parse_iso_ms(as_str(event.get("timestamp")))
    if scan.events_total == 1:
        scan.first_ms = ms  # the first event's own time; unknown stays unknown
    scan.last_ms = ms
    if kind in KIRO_IGNORED_TYPES:
        return
    required = REQUIRED_KIRO_KEYS.get(kind)
    if required is None:
        scan.unknown += 1
        return
    scan.events_used += 1
    assert kind in ID_KEYS, "ID_KEYS is derived from REQUIRED_KIRO_KEYS"
    if any(key not in payload for key in required) or not _nonempty(payload, *ID_KEYS[kind]):
        scan.malformed += 1
        return
    if kind == "tool_call":
        _on_call(scan, payload, ms, seen, main)
    elif kind == "tool_result":
        _on_result(scan, payload, ms, main)
    elif kind in ("pending_interaction", "interaction_resolved"):
        _on_permission(scan, kind, payload, ms)
    else:
        _on_turn_event(scan, kind, payload, ms, main)


# ---------------------------------------------------------------- assembling a session


def _scan_file(
    path: Path, limits: tuple[int, int, int], seen: set[str], *, main: bool
) -> tuple[_Scan, int] | str:
    """(scan, parse errors) of one raw file, or the skip reason when it cannot be read or crosses
    a bound. `limits` = (max lines, max bytes, step budget left); scanning stops at the first
    crossing."""
    lines_max, bytes_max, calls_left = limits
    assert calls_left >= 0, "the step budget is never negative"
    loaded = read_events(path, lines_max, bytes_max)
    if isinstance(loaded, str):
        return UNREADABLE if loaded.startswith("cannot") else OVER_BOUND
    events, parse_errors = loaded
    assert len(events) <= lines_max, "read_events bounded the lines"
    scan = _Scan(calls_max=calls_left)
    for event in events:
        _scan_event(scan, event, seen, main)
        if scan.over_bound:
            return OVER_BOUND
    assert len(scan.calls) <= calls_left, "the budget held"
    return scan, parse_errors


def _status(call: _Call, result: _Result | None) -> str:
    """The first matching row of the status table."""
    if call.status_raw == "denied":
        return "denied"
    if call.status_raw == "cancelled":
        return "cancelled"
    if result is None:
        return "unpaired"
    bad_exit = call.tool_class == "shell" and result.exit_code not in (None, 0)
    if not result.ok or call.status_raw == "failed" or bad_exit:
        return "error"
    return "ok"


def _duration(
    call: _Call, result: _Result | None, wait: int | None, pending_ms: int | None
) -> tuple[int | None, bool]:
    """(duration in ms, whether it was negative before the floor). Null when unpaired or a time
    is unknown. The wait is subtracted only when the permission prompt began after the call
    event: live Kiro logs write the call event at result time, after the prompt, so subtracting
    a wait that already ended before the call made nearly every duration negative."""
    if result is None or result.ended_ms is None or call.started_ms is None:
        return None, False
    inside = pending_ms is not None and pending_ms >= call.started_ms
    raw = result.ended_ms - call.started_ms - ((wait or 0) if inside else 0)
    return max(raw, 0), raw < 0


def _make_step(
    index: int,
    turn_index: int,
    call: _Call,
    result: _Result | None,
    waiting: tuple[int | None, int | None],
) -> tuple[StepRecord, bool]:
    """(the step, whether its duration was floored). `waiting` = (wait ms, prompt start ms)."""
    wait = waiting[0]
    duration, floored = _duration(call, result, wait, waiting[1])
    shell = call.tool_class == "shell"
    step = StepRecord(
        step_index=index,
        turn_index=turn_index,
        dispatch_group=None,
        tool=call.tool,
        tool_class=call.tool_class,
        args_hash=call.args_hash,
        target_hash=call.target_hash,
        status=_status(call, result),
        exit_code=result.exit_code if shell and result is not None else None,
        started_ms=call.started_ms,
        duration_ms=duration,
        permission_wait_ms=wait,
        shape=None,
    )
    return step, floored


@dataclass(frozen=True)
class _Sub:
    stem: str
    scan: _Scan
    parse_errors: int


def _sub_order(sub: _Sub) -> tuple[bool, int, str]:
    """By time of the first event (unknown last), then file name."""
    return sub.scan.first_ms is None, sub.scan.first_ms or 0, sub.stem


def _sub_disagreements(sub: _Sub) -> int:
    """Calls whose executionId differs from the file's first call, or whose subExecutionId is
    present and differs from the file name. They still count as steps."""
    if not sub.scan.calls:
        return 0
    first = sub.scan.calls[0].exec_id
    return sum(
        1 for c in sub.scan.calls if c.exec_id != first or (c.sub_id and c.sub_id != sub.stem)
    )


def _merged(
    scans: list[_Scan],
) -> tuple[dict[str, _Result], dict[str, int | None], dict[str, int | None]]:
    """(results, waits, prompt start times) by call id across files; the first seen wins."""
    results: dict[str, _Result] = {}
    waits: dict[str, int | None] = {}
    pendings: dict[str, int | None] = {}
    for scan in scans:
        for key, value in scan.results.items():
            results.setdefault(key, value)
        for key, wait in scan.waits.items():
            waits.setdefault(key, wait)
        for key, began in scan.pending_ms.items():
            pendings.setdefault(key, began)
    return results, waits, pendings


def _aggregate(steps: list[StepRecord], turns: int) -> list[tuple[int, int, int | None]]:
    """(steps, errors, permission wait sum or None) per turn index."""
    count, errors = [0] * turns, [0] * turns
    wait: list[int | None] = [None] * turns
    for step in steps:
        count[step.turn_index] += 1
        errors[step.turn_index] += step.status == "error"
        if step.permission_wait_ms is not None:
            wait[step.turn_index] = (wait[step.turn_index] or 0) + step.permission_wait_ms
    return list(zip(count, errors, wait, strict=True))


def _turn0(index: int, exec_id: str, acc: _Exec, agg: tuple[int, int, int | None]) -> TurnRecord:
    started = acc.start_ms if acc.has_start else acc.first_ms
    ended = acc.end_ms if acc.has_end else acc.last_ms
    return TurnRecord(
        turn_index=index,
        turn_key=short_hash(exec_id),
        depth=0,
        parent_turn_index=None,
        started_ms=started,
        ended_ms=ended,
        stop_reason=acc.stop_reason,
        steps=agg[0],
        errors=agg[1],
        model_calls=None if acc.requests_missing or acc.usage_events == 0 else acc.model_calls,
        credits=acc.credits,
        input_tokens=None,
        output_tokens=None,
        cache_read_tokens=None,
        cache_creation_tokens=None,
        permission_wait_ms=agg[2],
        parallel_groups=None,
    )


def _turn1(
    index: int, sub: _Sub, parent: int | None, agg: tuple[int, int, int | None]
) -> TurnRecord:
    return TurnRecord(
        turn_index=index,
        turn_key=short_hash(sub.stem),
        depth=1,
        parent_turn_index=parent,
        started_ms=sub.scan.first_ms,
        ended_ms=sub.scan.last_ms,
        stop_reason=None,
        steps=agg[0],
        errors=agg[1],
        model_calls=None,
        credits=None,
        input_tokens=None,
        output_tokens=None,
        cache_read_tokens=None,
        cache_creation_tokens=None,
        permission_wait_ms=agg[2],
        parallel_groups=None,
    )


def _build_steps(
    entries: list[tuple[int, _Call]], scans: list[_Scan]
) -> tuple[list[StepRecord], int, int]:
    """(steps in read order, calls with no result, durations floored at 0)."""
    results, waits, pendings = _merged(scans)
    steps: list[StepRecord] = []
    unpaired = floored_count = 0
    for index, (turn_index, call) in enumerate(entries):
        result = results.get(call.call_id)
        waiting = (waits.get(call.call_id), pendings.get(call.call_id))
        step, floored = _make_step(index, turn_index, call, result, waiting)
        steps.append(step)
        unpaired += result is None
        floored_count += floored
    assert len(steps) == len(entries), "one step per call"
    return steps, unpaired, floored_count


def _session_line(
    ref: SessionRef,
    meta: dict[str, object],
    scans: list[_Scan],
    extra: tuple[int, int, int, int, int, int],
) -> SessionRecord:
    """`extra` = (parse errors, extra malformed, unpaired calls, negative durations, turns,
    steps)."""
    parse_errors, disagreements, unpaired, negative, turns, steps = extra
    model = as_str(meta.get("modelId"))
    cwd = ref.cwds[0]
    assert HEX12_RE.fullmatch(short_hash(cwd)), "a hash of the first cwd"
    return SessionRecord(
        schema_version=SCHEMA_VERSION,
        reader_version=READER_VERSION,
        rule_version=RULE_VERSION,
        harness="kiro",
        tree=ref.tree,
        session_id=ref.session_id,
        harness_version=clean_name(as_str(meta.get("schemaVersion"))),
        model_label=clean_name(model) if model else None,
        workspace_hash=short_hash(cwd),
        workspace_label=clean_name(Path(cwd).name),
        created_day=ref.created_day,
        source_bytes=ref.source_bytes,
        source_mtime_ms=ref.source_mtime_ms,
        parse_errors=parse_errors,
        malformed_events=sum(s.malformed for s in scans) + disagreements,
        unknown_events=sum(s.unknown for s in scans),
        events_used=sum(s.events_used for s in scans),
        events_total=sum(s.events_total for s in scans),
        unpaired_calls=unpaired,
        usage_conflicts=0,
        negative_durations=negative,
        turns=turns,
        steps=steps,
    )


def read_kiro_session(ref: SessionRef, lines_max: int, bytes_max: int) -> SessionData | Skip:
    """One session as records, or the Skip that says why it could not be read. The returned data
    has not been through check_session; write_session does that."""
    assert ref.skip_reason == "" and ref.cwds, "only readable sessions with a cwd are read"
    assert lines_max > 0 and bytes_max > 0, "bounds are positive"
    meta = _read_meta(ref.path.parent / "session.json")
    # stat again right before the read: data read after this is at least as new as the stats, so
    # a stale record can only cause a rebuild, never a trusted old file
    fresh = _sources([ref.path, ref.path.parent / "session.json", *ref.sub_paths])
    if fresh is None or meta is None:
        return Skip(ref.session_id, "kiro", UNREADABLE)
    ref = replace(ref, source_bytes=fresh[0], source_mtime_ms=fresh[1])
    seen: set[str] = set()
    main = _scan_file(ref.path, (lines_max, bytes_max, STEPS_PER_SESSION_MAX), seen, main=True)
    if isinstance(main, str):
        return Skip(ref.session_id, "kiro", main)
    main_scan, parse_errors = main
    subs: list[_Sub] = []
    calls_left = STEPS_PER_SESSION_MAX - len(main_scan.calls)
    for path in ref.sub_paths[:SUB_FILES_MAX]:
        loaded = _scan_file(path, (lines_max, bytes_max, calls_left), seen, main=False)
        if isinstance(loaded, str):
            return Skip(ref.session_id, "kiro", loaded)
        calls_left -= len(loaded[0].calls)
        subs.append(_Sub(path.stem, loaded[0], loaded[1]))
        parse_errors += loaded[1]
    assert calls_left >= 0, "the budget held across files"
    subs.sort(key=_sub_order)
    return _assemble(ref, meta, main_scan, subs, parse_errors)


def _assemble(
    ref: SessionRef, meta: dict[str, object], main: _Scan, subs: list[_Sub], parse_errors: int
) -> SessionData:
    exec_ids = list(main.execs)
    exec_index = {exec_id: i for i, exec_id in enumerate(exec_ids)}
    entries = [(exec_index[c.exec_id], c) for c in main.calls]
    for offset, sub in enumerate(subs):
        entries.extend((len(exec_ids) + offset, c) for c in sub.scan.calls)
    assert len(entries) <= STEPS_PER_SESSION_MAX, "the scan enforced the step budget"
    assert len(exec_index) == len(exec_ids), "one turn per execution id"
    scans = [main, *(s.scan for s in subs)]
    steps, unpaired, negative = _build_steps(entries, scans)
    agg = _aggregate(steps, len(exec_ids) + len(subs))
    turns = [_turn0(i, e, main.execs[e], agg[i]) for i, e in enumerate(exec_ids)]
    for offset, sub in enumerate(subs):
        first = sub.scan.calls[0].exec_id if sub.scan.calls else None
        turns.append(
            _turn1(
                len(exec_ids) + offset,
                sub,
                exec_index.get(first or ""),
                agg[len(exec_ids) + offset],
            )
        )
    disagreements = sum(_sub_disagreements(s) for s in subs)
    line = _session_line(
        ref, meta, scans, (parse_errors, disagreements, unpaired, negative, len(turns), len(steps))
    )
    return SessionData(line, tuple(turns), tuple(steps))
