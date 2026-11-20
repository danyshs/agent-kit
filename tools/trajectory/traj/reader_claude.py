"""Claude Code reader: lists sessions under ~/.claude/projects and turns one into SessionData.

Raw text is read here and nowhere else. `text` and `thinking` blocks are never read beyond their
type; tool inputs are hashed and dropped. Rules: design_draft.md, "Claude reader" and "Status
mapping". Shell command shapes arrive with the detectors, so a shell step's shape is null here.
"""

from __future__ import annotations

import json
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
    is_eval_traffic,
    parse_iso_ms,
    read_events,
    short_hash,
)
from traj.limits import (
    BLOCKS_PER_MESSAGE_MAX,
    CLAUDE_HEAD_BYTES_MAX,
    CLAUDE_HEAD_LINES_MAX,
    CLAUDE_IGNORED_TYPES,
    CLAUDE_MALFORMED_MAX_PERCENT,
    READER_VERSION,
    REQUIRED_CLAUDE_KEYS,
    RULE_VERSION,
    SCHEMA_VERSION,
    SKIP_REASONS,
    STEPS_PER_SESSION_MAX,
    SUB_FILES_MAX,
    TOOL_CLASS,
)
from traj.schema import (
    DAY_RE,
    RequestUsage,
    SessionData,
    SessionRecord,
    SessionRef,
    Skip,
    StepRecord,
    TurnRecord,
)

_, UNREADABLE, _, _, EVAL, NO_CWD, UNKNOWN_FORMAT, OVER_BOUND = SKIP_REASONS
EVAL_ROOT_NAMES = ("/tmp",)  # same rule as the Kiro reader and the miner
TARGET_KEYS = ("file_path", "path", "notebook_path")
USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)
DAY_LENGTH = 10
START_KEY = "start"  # turn key of the implicit first turn, before any boundary


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


def _head_facts(path: Path) -> tuple[str, str] | None:
    """(first cwd, first day) from the head of a transcript, or None when it cannot be read.
    Only the `cwd` and `timestamp` keys are read, and only from the first lines."""
    assert CLAUDE_HEAD_LINES_MAX > 0 and CLAUDE_HEAD_BYTES_MAX > 0, "head bounds are positive"
    cwd, day = "", ""
    try:
        with path.open("rb") as handle:
            head = handle.read(CLAUDE_HEAD_BYTES_MAX)
    except OSError:
        return None
    for line in head.splitlines()[:CLAUDE_HEAD_LINES_MAX]:
        try:
            value: object = json.loads(line)
        except ValueError:
            continue  # a cut last line, or junk; the full read counts parse errors
        event = as_dict(value)
        cwd = cwd or as_str(event.get("cwd"))
        stamp = as_str(event.get("timestamp"))[:DAY_LENGTH]
        day = day or (stamp if DAY_RE.fullmatch(stamp) else "")
        if cwd and day:
            break
    return cwd, day


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


def _sub_files(main: Path) -> list[Path] | str:
    """`<stem>/subagents/agent-*.jsonl` next to a main file, or why they cannot be listed."""
    directory = main.parent / main.stem / "subagents"
    if not directory.is_dir():
        return []
    listed = _list_bounded(directory, SUB_FILES_MAX)
    if isinstance(listed, str):
        return listed
    return [p for p in listed if p.suffix == ".jsonl" and p.name.startswith("agent-")]


def _session_ref(main: Path, eval_roots: list[Path]) -> SessionRef:
    """One main transcript as a ref, with the first skip reason that applies."""
    session_id = main.stem
    assert SESSION_ID_RE.fullmatch(session_id), "the caller listed uuid-named files only"
    subs = _sub_files(main)
    sub_paths = tuple(subs) if isinstance(subs, list) else ()
    stats = _sources([main, *sub_paths])
    head = _head_facts(main)
    if stats is None or head is None or (isinstance(subs, str) and subs.startswith("cannot")):
        return SessionRef("claude", session_id, "main", main, (), (), "", UNREADABLE, 0, 0)
    cwd, day = head
    cwds = (cwd,) if cwd else ()
    reason = ""
    if is_eval_traffic(cwds, eval_roots):
        reason = EVAL
    elif not cwds:
        reason = NO_CWD
    elif isinstance(subs, str):
        reason = OVER_BOUND
    size, mtime = stats
    return SessionRef("claude", session_id, "main", main, sub_paths, cwds, day, reason, size, mtime)


def find_claude_sessions(projects_dir: Path, sessions_max: int) -> list[SessionRef] | str:
    """Every main transcript under the projects directory, skipped ones included with their
    reason, or the reason the directory cannot be listed. Project directory names start with
    `-`, so every path stays a Path and never reaches a shell."""
    assert sessions_max > 0, "a positive bound"
    if not projects_dir.is_dir():
        return f"{projects_dir.name} is not a directory"
    projects = _list_bounded(projects_dir, 2 * sessions_max)
    if isinstance(projects, str):
        return projects
    eval_roots = [Path(root).resolve() for root in EVAL_ROOT_NAMES]
    refs: list[SessionRef] = []
    for project in (p for p in projects if p.is_dir()):
        children = _list_bounded(project, 2 * sessions_max)
        if isinstance(children, str):
            return children
        mains = [
            p
            for p in children
            if p.suffix == ".jsonl" and SESSION_ID_RE.fullmatch(p.stem) and p.is_file()
        ]
        refs.extend(_session_ref(p, eval_roots) for p in mains)
        if len(refs) > sessions_max:
            return f"over {sessions_max} sessions"
    assert len(refs) <= sessions_max, "bound held"
    return refs


# ---------------------------------------------------------------- scanning one file


@dataclass(frozen=True)
class _Call:
    call_id: str
    turn: int  # index into _Scan.turns
    tool: str
    tool_class: str
    args_hash: str
    target_hash: str | None
    started_ms: int | None
    request_id: str  # "" when the line had no requestId


@dataclass(frozen=True)
class _Result:
    ended_ms: int | None
    status: str


@dataclass
class _Turn:
    key: str
    first_ms: int | None
    last_ms: int | None
    stop_reason: str | None = None
    requests: list[str] = field(default_factory=lambda: [])  # first appearance order


@dataclass
class _Scan:
    calls_max: int
    turns: list[_Turn] = field(default_factory=lambda: [])
    calls: list[_Call] = field(default_factory=lambda: [])
    results: dict[str, _Result] = field(default_factory=lambda: {})
    usage: dict[str, RequestUsage] = field(default_factory=lambda: {})
    conflicts: set[str] = field(default_factory=lambda: set())
    events_total: int = 0
    events_used: int = 0
    malformed: int = 0
    unknown: int = 0
    first_ms: int | None = None
    last_ms: int | None = None
    version: str = ""
    model: str = ""
    over_bound: bool = False


def _is_boundary(event: dict[str, object]) -> bool:
    """A typed prompt: not meta, not a compact summary, string content or text without results."""
    if event.get("isMeta") is True or event.get("isCompactSummary") is True:
        return False
    content = as_dict(event.get("message")).get("content")
    if isinstance(content, str):
        return True
    types = [as_str(as_dict(b).get("type")) for b in as_list(content)[:BLOCKS_PER_MESSAGE_MAX]]
    return "text" in types and "tool_result" not in types


def _target_hash(tool_class: str, args: dict[str, object]) -> str | None:
    if tool_class not in ("read", "write"):
        return None
    for value in (args.get(k) for k in TARGET_KEYS):
        if isinstance(value, str) and value:
            return short_hash(value)
    return None


def _token(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _max_or_known(a: int | None, b: int | None) -> int | None:
    if a is None or b is None:
        return a if b is None else b
    return max(a, b)


def _add_usage(scan: _Scan, request_id: str, usage: dict[str, object]) -> None:
    """Per request, each field is the max over its lines (streamed counts only grow)."""
    tokens = [_token(usage.get(k)) for k in USAGE_KEYS]
    new = RequestUsage(tokens[0], tokens[1], tokens[2], tokens[3])
    old = scan.usage.get(request_id)
    if old is None:
        scan.usage[request_id] = new
        return
    if old != new:
        scan.conflicts.add(request_id)
    scan.usage[request_id] = RequestUsage(
        _max_or_known(old.input_tokens, new.input_tokens),
        _max_or_known(old.output_tokens, new.output_tokens),
        _max_or_known(old.cache_read_tokens, new.cache_read_tokens),
        _max_or_known(old.cache_creation_tokens, new.cache_creation_tokens),
    )


def _on_assistant(scan: _Scan, event: dict[str, object], ms: int | None, seen: set[str]) -> None:
    assert scan.turns, "_scan_file opens a turn before any line"
    message = as_dict(event.get("message"))
    turn = scan.turns[-1]
    request_id = as_str(event.get("requestId"))
    scan.model = scan.model or as_str(message.get("model"))
    stop = as_str(message.get("stop_reason"))
    turn.stop_reason = clean_name(stop) if stop else turn.stop_reason
    if request_id:
        if request_id not in turn.requests and request_id not in scan.usage:
            turn.requests.append(request_id)
        _add_usage(scan, request_id, as_dict(message.get("usage")))
    blocks = as_list(message.get("content"))
    if len(blocks) > BLOCKS_PER_MESSAGE_MAX:
        scan.over_bound = True  # cutting would drop calls without saying so
        return
    for block in blocks:
        item = as_dict(block)
        call_id = as_str(item.get("id"))
        if as_str(item.get("type")) != "tool_use" or not call_id or call_id in seen:
            continue  # a repeated call counts once, the first
        seen.add(call_id)
        if len(scan.calls) >= scan.calls_max:
            scan.over_bound = True
            return
        args = as_dict(item.get("input"))
        tool = clean_name(as_str(item.get("name")))
        tool_class = TOOL_CLASS.get(tool, "other")
        target = _target_hash(tool_class, args)
        call = _Call(
            call_id, len(scan.turns) - 1, tool, tool_class, args_hash(args), target, ms, request_id
        )
        scan.calls.append(call)
    assert len(scan.calls) <= scan.calls_max, "the call budget held"


def _result_status(event: dict[str, object], block: dict[str, object]) -> str:
    """Rows 1, 3, 4, 5 of the Claude status table (row 2, unpaired, is decided at pairing)."""
    if as_str(as_dict(event.get("permissionDecision")).get("decision")) == "reject":
        return "denied"
    if as_dict(event.get("toolUseResult")).get("interrupted") is True:
        return "cancelled"
    return "error" if block.get("is_error") is True else "ok"


def _on_user(scan: _Scan, event: dict[str, object], ms: int | None, depth: int) -> None:
    if depth == 0 and _is_boundary(event):
        scan.turns.append(_Turn(short_hash(as_str(event.get("uuid"))), ms, ms))
        return
    blocks = as_list(as_dict(event.get("message")).get("content"))
    if len(blocks) > BLOCKS_PER_MESSAGE_MAX:
        scan.over_bound = True  # cutting would leave calls unpaired without saying so
        return
    for block in blocks:
        item = as_dict(block)
        call_id = as_str(item.get("tool_use_id"))
        if as_str(item.get("type")) == "tool_result" and call_id:
            scan.results.setdefault(call_id, _Result(ms, _result_status(event, item)))


def _well_formed(event: dict[str, object], kind: str) -> bool:
    if any(key not in event for key in REQUIRED_CLAUDE_KEYS[kind]):
        return False
    message = event.get("message")
    return isinstance(message, dict) and bool(as_str(event.get("uuid")))


def _scan_event(scan: _Scan, event: dict[str, object], seen: set[str], depth: int) -> None:
    scan.events_total += 1
    assert scan.events_used + scan.unknown <= scan.events_total, "counters nest"
    kind = as_str(event.get("type"))
    if kind in CLAUDE_IGNORED_TYPES:
        return
    if kind not in REQUIRED_CLAUDE_KEYS:
        scan.unknown += 1
        return
    scan.events_used += 1
    if not _well_formed(event, kind):
        scan.malformed += 1
        return
    ms = parse_iso_ms(as_str(event.get("timestamp")))
    scan.first_ms = scan.first_ms if scan.first_ms is not None else ms
    scan.last_ms = ms if ms is not None else scan.last_ms
    scan.version = scan.version or as_str(event.get("version"))
    if kind == "assistant":
        _on_assistant(scan, event, ms, seen)
    else:
        _on_user(scan, event, ms, depth)
    # after the handler, so a prompt line opens its own turn instead of ending the last one
    scan.turns[-1].last_ms = ms if ms is not None else scan.turns[-1].last_ms


def _scan_file(
    path: Path, limits: tuple[int, int, int], seen: set[str], depth: int
) -> tuple[_Scan, int] | str:
    """(scan, parse errors) of one transcript, or the skip reason. A depth 1 file is one turn."""
    lines_max, bytes_max, calls_left = limits
    assert calls_left >= 0 and depth in (0, 1), "a budget and a known depth"
    loaded = read_events(path, lines_max, bytes_max)
    if isinstance(loaded, str):
        return UNREADABLE if loaded.startswith("cannot") else OVER_BOUND
    events, parse_errors = loaded
    scan = _Scan(calls_max=calls_left)
    scan.turns.append(_Turn(short_hash(START_KEY if depth == 0 else path.stem), None, None))
    for event in events:  # read_events bounded the lines
        _scan_event(scan, event, seen, depth)
        if scan.over_bound:
            return OVER_BOUND
    if scan.turns[0].first_ms is None:
        scan.turns[0].first_ms = scan.first_ms
    if depth == 0 and len(scan.turns) > 1 and not _turn_has_calls(scan, 0):
        scan.turns.pop(0)  # no implicit first turn unless something happened before the prompt
        scan.calls = [replace(c, turn=c.turn - 1) for c in scan.calls]
    assert len(scan.calls) <= calls_left, "the budget held"
    return scan, parse_errors


def _turn_has_calls(scan: _Scan, turn: int) -> bool:
    return any(c.turn == turn for c in scan.calls) or bool(scan.turns[turn].requests)


# ---------------------------------------------------------------- assembling a session


def _malformed_gate(scans: list[_Scan]) -> bool:
    """True when the session is over the malformed bound and must be skipped as unknown."""
    used = sum(s.events_used for s in scans)
    bad = sum(s.malformed for s in scans)
    return used > 0 and bad * 100 > used * CLAUDE_MALFORMED_MAX_PERCENT


def _make_step(
    index: int, turn_index: int, call: _Call, result: _Result | None, group: int | None
) -> tuple[StepRecord, bool]:
    """(the step, whether its duration was floored at 0)."""
    duration: int | None = None
    floored = False
    if result is not None and result.ended_ms is not None and call.started_ms is not None:
        raw = result.ended_ms - call.started_ms
        duration, floored = max(raw, 0), raw < 0
    assert index >= 0 and turn_index >= 0, "indexes count from 0"
    assert duration is None or duration >= 0, "a floored duration is never negative"
    step = StepRecord(
        step_index=index,
        turn_index=turn_index,
        dispatch_group=group,
        tool=call.tool,
        tool_class=call.tool_class,
        args_hash=call.args_hash,
        target_hash=call.target_hash,
        status="unpaired" if result is None else result.status,
        exit_code=None,
        started_ms=call.started_ms,
        duration_ms=duration,
        permission_wait_ms=None,
        shape=None,
    )
    return step, floored


def _turn_record(
    index: int, depth: int, turn: _Turn, steps: list[StepRecord], usage: dict[str, RequestUsage]
) -> TurnRecord:
    """One turn. Tokens sum each request once; a field no request reported is null."""
    totals: list[int | None] = [None, None, None, None]
    for request_id in turn.requests:
        found = usage.get(request_id)
        values = (
            (
                found.input_tokens,
                found.output_tokens,
                found.cache_read_tokens,
                found.cache_creation_tokens,
            )
            if found
            else (None, None, None, None)
        )
        for slot, value in enumerate(values):
            if value is not None:
                totals[slot] = (totals[slot] or 0) + value
    assert all(t is None or t >= 0 for t in totals), "token sums are counts"
    assert all(s.turn_index == index for s in steps), "steps belong to this turn"
    groups: dict[int, int] = {}
    for step in steps:
        if step.dispatch_group is not None:
            groups[step.dispatch_group] = groups.get(step.dispatch_group, 0) + 1
    return TurnRecord(
        turn_index=index,
        turn_key=turn.key,
        depth=depth,
        parent_turn_index=None,
        started_ms=turn.first_ms,
        ended_ms=turn.last_ms,
        stop_reason=turn.stop_reason,
        steps=len(steps),
        errors=sum(s.status == "error" for s in steps),
        model_calls=len(turn.requests),
        credits=None,
        input_tokens=totals[0],
        output_tokens=totals[1],
        cache_read_tokens=totals[2],
        cache_creation_tokens=totals[3],
        permission_wait_ms=None,
        parallel_groups=sum(1 for n in groups.values() if n >= 2),
    )


def read_claude_session(ref: SessionRef, lines_max: int, bytes_max: int) -> SessionData | Skip:
    """One session as records, or the Skip that says why it could not be read. The returned data
    has not been through check_session; write_session does that."""
    assert ref.harness == "claude" and ref.skip_reason == "" and ref.cwds, "a readable ref"
    assert lines_max > 0 and bytes_max > 0, "bounds are positive"
    # stat again right before the read: a stale record can only cause a rebuild
    fresh = _sources([ref.path, *ref.sub_paths])
    if fresh is None:
        return Skip(ref.session_id, "claude", UNREADABLE)
    ref = replace(ref, source_bytes=fresh[0], source_mtime_ms=fresh[1])
    seen: set[str] = set()
    main = _scan_file(ref.path, (lines_max, bytes_max, STEPS_PER_SESSION_MAX), seen, 0)
    if isinstance(main, str):
        return Skip(ref.session_id, "claude", main)
    scans, parse_errors = [main[0]], main[1]
    calls_left = STEPS_PER_SESSION_MAX - len(main[0].calls)
    subs: list[_Scan] = []
    for path in ref.sub_paths[:SUB_FILES_MAX]:
        loaded = _scan_file(path, (lines_max, bytes_max, calls_left), seen, 1)
        if isinstance(loaded, str):
            return Skip(ref.session_id, "claude", loaded)
        calls_left -= len(loaded[0].calls)
        subs.append(loaded[0])
        parse_errors += loaded[1]
    assert calls_left >= 0, "the budget held across files"
    subs.sort(key=lambda s: (s.first_ms is None, s.first_ms or 0, s.turns[0].key))
    scans.extend(subs)
    if _malformed_gate(scans):
        return Skip(ref.session_id, "claude", UNKNOWN_FORMAT)
    return _assemble(ref, scans, parse_errors)


def _assemble(ref: SessionRef, scans: list[_Scan], parse_errors: int) -> SessionData:
    main, subs = scans[0], scans[1:]
    results: dict[str, _Result] = {}
    usage: dict[str, RequestUsage] = {}
    for scan in scans:
        for key, value in scan.results.items():
            results.setdefault(key, value)
        for key, value in scan.usage.items():
            usage.setdefault(key, value)
    group_of: dict[str, int] = {}
    entries: list[tuple[int, int, _Call]] = []  # (turn index, depth, call)
    for call in main.calls:
        entries.append((call.turn, 0, call))
    for offset, sub in enumerate(subs):
        entries.extend((len(main.turns) + offset, 1, c) for c in sub.calls)
    assert len(entries) <= STEPS_PER_SESSION_MAX, "the scans enforced the step budget"
    steps: list[StepRecord] = []
    unpaired = negative = 0
    for index, (turn_index, _, call) in enumerate(entries):
        group = None
        if call.request_id:
            group = group_of.setdefault(call.request_id, len(group_of))
        step, floored = _make_step(index, turn_index, call, results.get(call.call_id), group)
        steps.append(step)
        unpaired += step.status == "unpaired"
        negative += floored
    by_turn: list[list[StepRecord]] = [[] for _ in range(len(main.turns) + len(subs))]
    for step in steps:
        by_turn[step.turn_index].append(step)
    turns = [_turn_record(i, 0, t, by_turn[i], usage) for i, t in enumerate(main.turns)]
    for offset, sub in enumerate(subs):
        index = len(main.turns) + offset
        turn = replace(sub.turns[0], first_ms=sub.first_ms, last_ms=sub.last_ms)
        turns.append(_turn_record(index, 1, turn, by_turn[index], usage))
    line = _session_line(ref, scans, (parse_errors, unpaired, negative, len(turns), len(steps)))
    return SessionData(line, tuple(turns), tuple(steps))


def _session_line(
    ref: SessionRef, scans: list[_Scan], extra: tuple[int, int, int, int, int]
) -> SessionRecord:
    """`extra` = (parse errors, unpaired calls, negative durations, turns, steps)."""
    parse_errors, unpaired, negative, turns, steps = extra
    cwd = ref.cwds[0]
    assert HEX12_RE.fullmatch(short_hash(cwd)), "a hash of the first cwd"
    main = scans[0]
    return SessionRecord(
        schema_version=SCHEMA_VERSION,
        reader_version=READER_VERSION,
        rule_version=RULE_VERSION,
        harness="claude",
        tree="main",
        session_id=ref.session_id,
        harness_version=clean_name(main.version) if main.version else None,
        model_label=clean_name(main.model) if main.model else None,
        workspace_hash=short_hash(cwd),
        workspace_label=clean_name(Path(cwd).name),
        created_day=ref.created_day,
        source_bytes=ref.source_bytes,
        source_mtime_ms=ref.source_mtime_ms,
        parse_errors=parse_errors,
        malformed_events=sum(s.malformed for s in scans),
        unknown_events=sum(s.unknown for s in scans),
        events_used=sum(s.events_used for s in scans),
        events_total=sum(s.events_total for s in scans),
        unpaired_calls=unpaired,
        usage_conflicts=sum(len(s.conflicts) for s in scans),
        negative_durations=negative,
        turns=turns,
        steps=steps,
    )
