"""`eval mine`: the usage miner (design 3.9, 9). Read-only over ~/.claude/projects (the default)
or ~/.kiro/sessions (`--harness kiro`).

Emits counts only: per tool, per skill, per steering file. Message text never leaves the
normalizers (`_recs_interactive`, `_recs_headless`, `_recs_claude`): they keep a name, a hash, an
exit code and a credit number, and drop everything else. Eval traffic is skipped by session cwd,
not by content.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final, cast

from lib.run import CLAUDE_TOOL_NAMES

MINER_SESSIONS_MAX: Final = 5_000
MINER_LINES_PER_FILE_MAX: Final = 200_000
MINER_FILE_BYTES_MAX: Final = 67_108_864
MINER_RETRY_WINDOW_CALLS: Final = 3  # same tool + same args hash within this many prior calls
MINER_SUB_FILES_MAX: Final = 512
MINER_PATHS_PER_CALL_MAX: Final = 64
MINER_DOCS_PER_EVENT_MAX: Final = 256
MINER_PARTS_PER_MESSAGE_MAX: Final = 256
MINER_SUMMARIES_PER_EVENT_MAX: Final = 64
NAME_RE: Final = re.compile(r"^[a-z0-9_][a-z0-9_.:-]{0,63}$")
EXIT_TAIL_RE: Final = re.compile(r"Exit Code: (\d{1,5})\s*$")
EXIT_TAIL_CHARS: Final = 64  # the code is the last line; never read more of the output than this
SKILL_FILE_RE: Final = re.compile(r"/skills/([^/]+)/SKILL\.md$")
SHELL_TOOLS: Final = frozenset({"execute_bash", "shell"})
SKILL_LOAD_TOOL: Final = "disclose_context"
FILE_READ_TOOLS: Final = frozenset({"read_file", "read"})
OTHER: Final = "(other)"
HEADLESS_EXEC: Final = "headless"  # the cli log has no turn ids: one execution per session
EXIT_OK: Final = 0
EXIT_BAD: Final = 1
EXIT_USAGE: Final = 2
MINE_USAGE: Final = (
    "usage: eval mine --stamp TEXT [--harness claude|kiro] [--sessions DIR] [--home DIR] "
    "[--out FILE] [--lines-max N] [--bytes-max N]"
)
MINE_FLAGS: Final = frozenset(
    {
        "--stamp",
        "--harness",
        "--sessions",
        "--home",
        "--kiro-home",
        "--out",
        "--lines-max",
        "--bytes-max",
    }
)
HARNESSES: Final = ("claude", "kiro")
SKIP_NO_MESSAGES: Final = "no messages file"
SKIP_NO_CWD: Final = "headless without cwd"
SKIP_CLAUDE_NO_CWD: Final = "no cwd"
SKIP_EVAL: Final = "eval traffic (cwd under /tmp)"
CLAUDE_TREE: Final = "claude"
CLAUDE_EXEC: Final = "claude"  # transcripts carry no execution id; the session is one window
CLAUDE_HEAD_LINES_MAX: Final = 64  # cwd and timestamp sit on the first prompt line
CLAUDE_PROJECTS_MAX: Final = 5_000
CLAUDE_STEM_RE: Final = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


@dataclass(frozen=True)
class SessionRef:
    tree: str  # "interactive" | "workflow" | "headless"
    path: Path  # the events file: messages.jsonl, or cli/<id>.jsonl
    sub_paths: tuple[Path, ...]
    cwds: tuple[str, ...]
    created_day: str  # YYYY-MM-DD from the session metadata, "" if unknown
    skip_reason: str = ""  # set by find_sessions when the session cannot be mined


@dataclass(frozen=True)
class SessionFacts:
    tree: str
    created_day: str
    tool_calls: dict[str, int]
    tool_paired: dict[str, int]
    tool_fails: dict[str, int]
    tool_retries: dict[str, int]
    unpaired: int
    parse_errors: int
    credits: float
    skill_loads: dict[str, int]
    skill_denied: dict[str, int]
    skill_file_reads: dict[str, int]
    skill_credits: dict[str, float]
    steering_events: dict[str, int]
    steering_listed: frozenset[str]


@dataclass(frozen=True)
class UsageTotals:
    mined: int
    mined_by_tree: dict[str, int]
    skipped: dict[str, int]
    first_day: str
    last_day: str
    tool_calls: dict[str, int]
    tool_paired: dict[str, int]
    tool_fails: dict[str, int]
    tool_retries: dict[str, int]
    unpaired: int
    parse_errors: int
    credits: float
    credits_by_tree: dict[str, float]
    skill_loads: dict[str, int]
    skill_denied: dict[str, int]
    skill_sessions: dict[str, int]
    skill_file_reads: dict[str, int]
    skill_credits: dict[str, float]
    steering_events: dict[str, int]
    steering_sessions: dict[str, int]


@dataclass(frozen=True)
class _Rec:
    """One normalized event. No text field exists, so none can leak."""

    kind: str  # "call" | "result" | "usage" | "steer"
    call_id: str = ""
    tool: str = ""
    exec_id: str = ""
    args_hash: str = ""
    status: str = ""
    skill: str | None = None
    skill_files: tuple[str, ...] = ()
    success: bool = True
    exit_code: int | None = None
    credits: float = 0.0
    names: tuple[str, ...] = ()


@dataclass
class _Pending:
    tool: str
    exec_id: str


@dataclass
class _State:
    pending: dict[str, _Pending] = field(default_factory=lambda: {})
    seen_ids: set[str] = field(default_factory=lambda: set())
    recent: dict[str, list[tuple[str, str]]] = field(default_factory=lambda: {})
    calls: Counter[str] = field(default_factory=lambda: Counter())
    paired: Counter[str] = field(default_factory=lambda: Counter())
    fails: Counter[str] = field(default_factory=lambda: Counter())
    retries: Counter[str] = field(default_factory=lambda: Counter())
    loads: Counter[str] = field(default_factory=lambda: Counter())
    denied: Counter[str] = field(default_factory=lambda: Counter())
    file_reads: Counter[str] = field(default_factory=lambda: Counter())
    skill_execs: set[tuple[str, str]] = field(default_factory=lambda: set())
    exec_credits: dict[str, float] = field(default_factory=lambda: {})
    steering: Counter[str] = field(default_factory=lambda: Counter())
    parse_errors: int = 0


# ---------------------------------------------------------------- small typed helpers


def _dict(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _list(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else []


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _clean_name(raw: str, allowed: frozenset[str] | None) -> str:
    """A name that is safe to print: in the allowed set, or matches NAME_RE, else (other)."""
    assert OTHER, "the fallback label exists"
    if allowed is not None:
        return raw if raw in allowed else OTHER
    return raw if NAME_RE.match(raw) else OTHER


def _args_hash(args: dict[str, object]) -> str:
    text = json.dumps(args, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _exit_code(content: str) -> int | None:
    """The trailing `Exit Code: N` of a shell result. Only that number is read."""
    match = EXIT_TAIL_RE.search(content[-EXIT_TAIL_CHARS:])
    return int(match.group(1)) if match else None


def _skill_from_args(args: dict[str, object]) -> str:
    """`{name: x}` or `{id: "skill::x"}` (both shapes seen in v3 logs); "" if neither."""
    name = _str(args.get("name"))
    if name:
        return name
    ident = _str(args.get("id"))
    return ident.removeprefix("skill::") if ident.startswith("skill::") else ""


def _skill_files(args: dict[str, object]) -> tuple[str, ...]:
    """Skill names whose SKILL.md this read call targets (`path`, or `operations[].path`)."""
    texts = [_str(args.get("path"))]
    for op in _list(args.get("operations"))[:MINER_PATHS_PER_CALL_MAX]:
        texts.append(_str(_dict(op).get("path")))
    found = [m.group(1) for t in texts if (m := SKILL_FILE_RE.search(t))]
    assert len(texts) <= MINER_PATHS_PER_CALL_MAX + 1, "paths are bounded"
    assert len(found) <= len(texts), "at most one match per path"
    return tuple(found)


def _call_rec(call_id: str, tool: str, exec_id: str, status: str, args: dict[str, object]) -> _Rec:
    assert call_id, "a call has an id"
    assert isinstance(args, dict), "args is a dict"
    skill = _skill_from_args(args) if tool == SKILL_LOAD_TOOL else None
    files = _skill_files(args) if tool in FILE_READ_TOOLS else ()
    return _Rec(
        kind="call",
        call_id=call_id,
        tool=tool,
        exec_id=exec_id,
        args_hash=_args_hash(args),
        status=status,
        skill=skill,
        skill_files=files,
    )


# ---------------------------------------------------------------- normalizers


def _steering_name(doc: object) -> str:
    if isinstance(doc, str):
        return doc.rsplit("/", 1)[-1].removesuffix(".md")
    return _str(_dict(doc).get("displayName"))


def _recs_interactive(event: dict[str, object]) -> list[_Rec]:
    """Records for the interactive/workflow/sub-execution envelope `{payload: {type, ...}}`."""
    payload = _dict(event.get("payload"))
    assert isinstance(payload, dict), "a missing payload reads as an empty dict"
    kind = _str(payload.get("type"))
    exec_id = _str(payload.get("executionId"))
    assert isinstance(exec_id, str), "exec id is text"
    if kind == "tool_call":
        args = _dict(payload.get("args"))
        call_id, tool = _str(payload.get("toolCallId")), _str(payload.get("toolName"))
        return (
            [_call_rec(call_id, tool, exec_id, _str(payload.get("status")), args)]
            if call_id
            else []
        )
    if kind == "tool_result":
        code = _exit_code(_str(payload.get("content")))
        ok = payload.get("success") is not False
        return [_Rec("result", call_id=_str(payload.get("toolCallId")), success=ok, exit_code=code)]
    if kind == "usage_summary":
        total = 0.0
        for item in _list(payload.get("promptTurnSummaries"))[:MINER_SUMMARIES_PER_EVENT_MAX]:
            usage = _dict(item).get("usage")
            if isinstance(usage, int | float) and not isinstance(usage, bool):
                total += float(usage)
        return [_Rec("usage", exec_id=exec_id, credits=total)]
    if kind == "steering_inclusion":
        docs = _list(payload.get("documents"))[:MINER_DOCS_PER_EVENT_MAX]
        return [_Rec("steer", names=tuple(_steering_name(d) for d in docs))]
    return []


def _recs_headless(event: dict[str, object]) -> list[_Rec]:
    """Records for the cli log `{kind, data: {content: [{kind: toolUse|toolResult, data}]}}`."""
    recs: list[_Rec] = []
    parts = _list(_dict(event.get("data")).get("content"))[:MINER_PARTS_PER_MESSAGE_MAX]
    assert len(parts) <= MINER_PARTS_PER_MESSAGE_MAX, "parts are bounded"
    for part in parts:
        part_kind, data = _str(_dict(part).get("kind")), _dict(_dict(part).get("data"))
        if part_kind == "toolUse":
            args = _dict(data.get("input"))
            call_id, tool = _str(data.get("toolUseId")), _str(data.get("name"))
            if call_id:
                recs.append(_call_rec(call_id, tool, HEADLESS_EXEC, "", args))
        elif part_kind == "toolResult":
            ok = _str(data.get("status")) == "success"
            recs.append(_Rec("result", call_id=_str(data.get("toolUseId")), success=ok))
    assert all(r.kind in ("call", "result") for r in recs), "headless has calls and results only"
    return recs


def _claude_call(item: dict[str, object]) -> _Rec | None:
    """A Claude `tool_use` block as a call, named as the cases name tools (Bash: execute_bash)."""
    call_id, raw = _str(item.get("id")), _str(item.get("name"))
    if not call_id:
        return None
    args = _dict(item.get("input"))
    skill = _str(args.get("skill")).rsplit(":", 1)[-1] if raw == "Skill" else None
    match = SKILL_FILE_RE.search(_str(args.get("file_path"))) if raw == "Read" else None
    rec = _Rec(
        "call",
        call_id=call_id,
        tool=CLAUDE_TOOL_NAMES.get(raw, raw.lower()),
        exec_id=CLAUDE_EXEC,
        args_hash=_args_hash(args),
        skill=skill,
        skill_files=(match.group(1),) if match else (),
    )
    assert rec.kind == "call" and rec.call_id, "a call with an id"
    return rec


def _recs_claude(event: dict[str, object]) -> list[_Rec] | str:
    """Records for one Claude Code transcript line; a str means a message is over the bound."""
    kind = _str(event.get("type"))
    if kind not in ("assistant", "user"):
        return []
    blocks = _list(_dict(event.get("message")).get("content"))
    if len(blocks) > MINER_PARTS_PER_MESSAGE_MAX:
        return f"a message has over {MINER_PARTS_PER_MESSAGE_MAX} parts"  # cutting hides calls
    recs: list[_Rec] = []
    for block in blocks:
        item = _dict(block)
        part = _str(item.get("type"))
        if kind == "assistant" and part == "tool_use":
            rec = _claude_call(item)
            recs += [rec] if rec else []
        elif kind == "user" and part == "tool_result":
            ok = item.get("is_error") is not True  # a failed Bash exit is is_error too
            recs.append(_Rec("result", call_id=_str(item.get("tool_use_id")), success=ok))
    assert len(recs) <= len(blocks), "at most one record per block"
    return recs


# ---------------------------------------------------------------- tally


def _apply_call(state: _State, rec: _Rec, known_skills: frozenset[str]) -> None:
    assert rec.kind == "call", "calls only"
    if rec.call_id in state.seen_ids:
        return  # a repeated event for the same call is not a second call
    state.seen_ids.add(rec.call_id)
    tool = _clean_name(rec.tool, None)
    state.calls[tool] += 1
    state.pending[rec.call_id] = _Pending(tool, rec.exec_id)
    window = state.recent.setdefault(rec.exec_id, [])
    if (tool, rec.args_hash) in window:
        state.retries[tool] += 1
    window.append((tool, rec.args_hash))
    del window[:-MINER_RETRY_WINDOW_CALLS]
    if rec.skill is not None:
        skill = _clean_name(rec.skill, known_skills)
        if rec.status == "denied":
            state.denied[skill] += 1
        else:
            state.loads[skill] += 1
            state.skill_execs.add((rec.exec_id, skill))
    for raw in rec.skill_files:
        state.file_reads[_clean_name(raw, known_skills)] += 1
    assert len(state.pending) <= sum(state.calls.values()), "pending never exceeds calls"


def _apply_result(state: _State, rec: _Rec) -> None:
    assert rec.kind == "result", "results only"
    pending = state.pending.pop(rec.call_id, None)
    if pending is None:
        return  # a result with no call is ignored here; unpaired calls are counted at the end
    state.paired[pending.tool] += 1
    bad = (not rec.success) or (
        pending.tool in SHELL_TOOLS and rec.exit_code is not None and rec.exit_code != 0
    )
    if bad:
        state.fails[pending.tool] += 1
    assert state.fails[pending.tool] <= state.paired[pending.tool], "fails are a subset of paired"


def _apply(
    state: _State, rec: _Rec, known_skills: frozenset[str], known_steering: frozenset[str]
) -> None:
    assert rec.kind in ("call", "result", "usage", "steer"), "four record kinds"
    if rec.kind == "call":
        _apply_call(state, rec, known_skills)
    elif rec.kind == "result":
        _apply_result(state, rec)
    elif rec.kind == "usage":
        state.exec_credits[rec.exec_id] = state.exec_credits.get(rec.exec_id, 0.0) + rec.credits
    else:
        assert rec.kind == "steer", "four record kinds"
        for raw in rec.names:
            state.steering[_clean_name(raw, known_steering)] += 1


# ---------------------------------------------------------------- reading


def read_events(
    path: Path, lines_max: int, bytes_max: int
) -> tuple[list[dict[str, object]], int] | str:
    """(events, parse-error count), or the reason the file is over a bound. Never partial."""
    assert lines_max > 0 and bytes_max > 0, "bounds are positive"
    try:
        size = path.stat().st_size
    except OSError as err:
        return f"cannot stat {path.name}: {err.strerror}"
    if size > bytes_max:
        return f"{path.name} is {size} bytes, over the {bytes_max} byte bound"
    events: list[dict[str, object]] = []
    parse_errors = 0
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for count, line in enumerate(handle, start=1):
                if count > lines_max:
                    return f"{path.name} has over {lines_max} lines"
                if not line.strip():
                    continue
                try:
                    value: object = json.loads(line)
                except ValueError:
                    parse_errors += 1
                    continue
                if isinstance(value, dict):
                    events.append(cast(dict[str, object], value))
                else:
                    parse_errors += 1
    except OSError as err:
        return f"cannot read {path.name}: {err.strerror}"
    assert parse_errors >= 0 and len(events) <= lines_max, "counts are consistent"
    return events, parse_errors


def _finish(state: _State, ref: SessionRef) -> SessionFacts:
    skill_credits: dict[str, float] = {}
    for exec_id, skill in sorted(state.skill_execs):
        skill_credits[skill] = skill_credits.get(skill, 0.0) + state.exec_credits.get(exec_id, 0.0)
    assert all(v >= 0.0 for v in skill_credits.values()), "credits are not negative"
    assert sum(state.paired.values()) <= sum(state.calls.values()), "paired never exceeds calls"
    return SessionFacts(
        tree=ref.tree,
        created_day=ref.created_day,
        tool_calls=dict(state.calls),
        tool_paired=dict(state.paired),
        tool_fails=dict(state.fails),
        tool_retries=dict(state.retries),
        unpaired=len(state.pending),
        parse_errors=state.parse_errors,
        credits=sum(state.exec_credits.values()),
        skill_loads=dict(state.loads),
        skill_denied=dict(state.denied),
        skill_file_reads=dict(state.file_reads),
        skill_credits=skill_credits,
        steering_events=dict(state.steering),
        steering_listed=frozenset(state.steering),
    )


def _normalize(tree: str, event: dict[str, object]) -> list[_Rec] | str:
    if tree == CLAUDE_TREE:
        return _recs_claude(event)
    return _recs_headless(event) if tree == "headless" else _recs_interactive(event)


def mine_session(
    ref: SessionRef,
    known_skills: frozenset[str],
    known_steering: frozenset[str],
    lines_max: int = MINER_LINES_PER_FILE_MAX,
    bytes_max: int = MINER_FILE_BYTES_MAX,
) -> SessionFacts | str:
    """Counters for one session (main log plus sub-executions), or why it is unreadable."""
    assert ref.tree in ("interactive", "workflow", "headless", CLAUDE_TREE), "known tree"
    state = _State()
    for path in (ref.path, *ref.sub_paths):
        loaded = read_events(path, lines_max, bytes_max)
        if isinstance(loaded, str):
            return loaded
        events, parse_errors = loaded
        state.parse_errors += parse_errors
        for event in events:
            recs = _normalize(ref.tree, event)
            if isinstance(recs, str):
                return f"{path.name}: {recs}"
            for rec in recs:
                _apply(state, rec, known_skills, known_steering)
    assert state.parse_errors >= 0, "a count"
    return _finish(state, ref)


# ---------------------------------------------------------------- discovery


def is_eval_traffic(cwds: Sequence[str], eval_roots: Sequence[Path]) -> bool:
    """True when any cwd resolves to or under an eval root (macOS /tmp is /private/tmp)."""
    assert all(root.is_absolute() for root in eval_roots), "roots are absolute"
    assert all(isinstance(c, str) for c in cwds), "cwds are text"
    for cwd in cwds:
        real = Path(os.path.realpath(cwd))
        if any(real == root or real.is_relative_to(root) for root in eval_roots):
            return True
    return False


def _read_json_dict(path: Path) -> dict[str, object] | str:
    assert path.suffix == ".json", "a json file"
    assert path.name, "a named file"
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        return f"cannot read {path.name}: {err}"
    return (
        cast(dict[str, object], value)
        if isinstance(value, dict)
        else f"{path.name} is not an object"
    )


def _interactive_ref(sess: Path) -> SessionRef | str:
    assert sess.name.startswith("sess_"), "a session dir"
    assert sess.is_dir(), "listed as a directory"
    meta = _read_json_dict(sess / "session.json")
    if isinstance(meta, str):
        return meta
    mode = _str(meta.get("agentMode"))
    cwds = tuple(_str(p) for p in _list(meta.get("workspacePaths")) + _list(meta.get("rootPaths")))
    subs = sorted((sess / "sub-executions").glob("*.jsonl"))
    if len(subs) > MINER_SUB_FILES_MAX:
        return f"{sess.name} has over {MINER_SUB_FILES_MAX} sub-executions"
    messages = sess / "messages.jsonl"
    return SessionRef(
        tree="workflow" if mode.startswith("wf-") else "interactive",
        path=messages,
        sub_paths=tuple(subs),
        cwds=tuple(c for c in cwds if c),
        created_day=_str(meta.get("createdAt"))[:10],
        skip_reason="" if messages.is_file() else SKIP_NO_MESSAGES,
    )


def _headless_ref(log: Path) -> SessionRef | str:
    assert log.suffix == ".jsonl", "a cli log"
    assert log.parent.name == "cli", "inside the cli dir"
    sidecar = log.with_suffix(".json")
    if not sidecar.is_file():
        return SessionRef("headless", log, (), (), "", SKIP_NO_CWD)
    meta = _read_json_dict(sidecar)
    if isinstance(meta, str):
        return meta
    cwd = _str(meta.get("cwd"))
    return SessionRef(
        "headless",
        log,
        (),
        (cwd,) if cwd else (),
        _str(meta.get("created_at"))[:10],
        "" if cwd else SKIP_NO_CWD,
    )


def find_sessions(sessions_dir: Path, sessions_max: int) -> list[SessionRef] | str:
    """Every session under the store, or why the store cannot be listed. Over the bound is a str."""
    assert sessions_max > 0, "a positive bound"
    if not sessions_dir.is_dir():
        return f"{sessions_dir.name} is not a directory"
    refs: list[SessionRef] = []
    for group in sorted(sessions_dir.iterdir()):
        if not group.is_dir():
            continue
        found = (
            [_headless_ref(p) for p in sorted(group.glob("*.jsonl"))]
            if group.name == "cli"
            else [_interactive_ref(p) for p in sorted(group.glob("sess_*")) if p.is_dir()]
        )
        for ref in found:
            if isinstance(ref, str):
                return ref
            refs.append(ref)
        if len(refs) > sessions_max:
            return f"over {sessions_max} sessions"
    assert len(refs) <= sessions_max, "bound held"
    return refs


def _claude_head(path: Path) -> tuple[str, str] | str:
    """(cwd, YYYY-MM-DD) from the first lines of a transcript, "" where absent."""
    cwd, day = "", ""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for _, line in zip(range(CLAUDE_HEAD_LINES_MAX), handle, strict=False):
                try:
                    event = _dict(json.loads(line))
                except ValueError:
                    continue
                cwd, day = cwd or _str(event.get("cwd")), day or _str(event.get("timestamp"))[:10]
                if cwd and day:
                    break
    except OSError as err:
        return f"cannot read {path.name}: {err.strerror}"
    assert len(day) <= 10, "a date at most"
    return cwd, day


def _claude_ref(path: Path) -> SessionRef | str:
    assert CLAUDE_STEM_RE.match(path.stem), "a session file"
    head = _claude_head(path)
    if isinstance(head, str):
        return head
    cwd, day = head
    subs = sorted((path.parent / path.stem / "subagents").glob("*.jsonl"))
    if len(subs) > MINER_SUB_FILES_MAX:
        return f"{path.stem} has over {MINER_SUB_FILES_MAX} sub-agent files"
    skip = "" if cwd else SKIP_CLAUDE_NO_CWD
    return SessionRef(CLAUDE_TREE, path, tuple(subs), (cwd,) if cwd else (), day, skip)


def find_claude_sessions(projects_dir: Path, sessions_max: int) -> list[SessionRef] | str:
    """Every `<project>/<uuid>.jsonl` transcript, or why the store cannot be listed."""
    assert sessions_max > 0, "a positive bound"
    if not projects_dir.is_dir():
        return f"{projects_dir.name} is not a directory"
    projects = sorted(p for p in projects_dir.iterdir() if p.is_dir())
    if len(projects) > CLAUDE_PROJECTS_MAX:
        return f"over {CLAUDE_PROJECTS_MAX} project folders"
    refs: list[SessionRef] = []
    for project in projects:
        for path in sorted(project.glob("*.jsonl")):
            if not CLAUDE_STEM_RE.match(path.stem):
                continue
            ref = _claude_ref(path)
            if isinstance(ref, str):
                return ref
            refs.append(ref)
        if len(refs) > sessions_max:
            return f"over {sessions_max} sessions"
    assert len(refs) <= sessions_max, "bound held"
    return refs


# ---------------------------------------------------------------- merge


def _sum_into(target: Counter[str], source: dict[str, int]) -> None:
    for key, value in source.items():
        target[key] += value


def merge(facts: Sequence[SessionFacts]) -> UsageTotals:
    """Add per-session counters up. `seen` and `skipped` are filled in by the caller."""
    assert len(facts) <= MINER_SESSIONS_MAX, "bounded by the session bound"
    c = {
        n: Counter[str]()
        for n in (
            "calls",
            "paired",
            "fails",
            "retries",
            "loads",
            "denied",
            "ssess",
            "freads",
            "esess",
            "events",
        )
    }
    trees: Counter[str] = Counter()
    credits_by_tree: dict[str, float] = {}
    skill_credits: dict[str, float] = {}
    unpaired = parse_errors = 0
    days: list[str] = []
    for f in facts:
        trees[f.tree] += 1
        credits_by_tree[f.tree] = credits_by_tree.get(f.tree, 0.0) + f.credits
        _sum_into(c["calls"], f.tool_calls)
        _sum_into(c["paired"], f.tool_paired)
        _sum_into(c["fails"], f.tool_fails)
        _sum_into(c["retries"], f.tool_retries)
        _sum_into(c["loads"], f.skill_loads)
        _sum_into(c["denied"], f.skill_denied)
        _sum_into(c["freads"], f.skill_file_reads)
        _sum_into(c["events"], f.steering_events)
        for skill in f.skill_loads:
            c["ssess"][skill] += 1
        for name in f.steering_listed:
            c["esess"][name] += 1
        for skill, value in f.skill_credits.items():
            skill_credits[skill] = skill_credits.get(skill, 0.0) + value
        unpaired += f.unpaired
        parse_errors += f.parse_errors
        if f.created_day:
            days.append(f.created_day)
    total = UsageTotals(
        mined=len(facts),
        mined_by_tree=dict(trees),
        skipped={},
        first_day=min(days, default=""),
        last_day=max(days, default=""),
        tool_calls=dict(c["calls"]),
        tool_paired=dict(c["paired"]),
        tool_fails=dict(c["fails"]),
        tool_retries=dict(c["retries"]),
        unpaired=unpaired,
        parse_errors=parse_errors,
        credits=sum(f.credits for f in facts),
        credits_by_tree=dict(credits_by_tree),
        skill_loads=dict(c["loads"]),
        skill_denied=dict(c["denied"]),
        skill_sessions=dict(c["ssess"]),
        skill_file_reads=dict(c["freads"]),
        skill_credits=dict(skill_credits),
        steering_events=dict(c["events"]),
        steering_sessions=dict(c["esess"]),
    )
    assert sum(total.mined_by_tree.values()) == len(facts), "every session counted once"
    return total


# ---------------------------------------------------------------- report


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    assert all(len(r) == len(header) for r in rows), "rectangular table"
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    return out + ["| " + " | ".join(r) + " |" for r in rows]


def _rate(part: int, whole: int) -> str:
    return f"{part / whole:.1%}" if whole else "n/a"


def _tool_rows(t: UsageTotals) -> list[list[str]]:
    order = sorted(t.tool_calls, key=lambda n: (-t.tool_calls[n], n))
    return [
        [
            f"`{n}`",
            str(t.tool_calls[n]),
            str(t.tool_paired.get(n, 0)),
            str(t.tool_fails.get(n, 0)),
            _rate(t.tool_fails.get(n, 0), t.tool_paired.get(n, 0)),
            str(t.tool_retries.get(n, 0)),
        ]
        for n in order
    ]


def _skill_rows(t: UsageTotals, available: Sequence[str]) -> list[list[str]]:
    names = sorted(
        set(available) | set(t.skill_loads) | set(t.skill_file_reads) | set(t.skill_denied)
    )
    return [
        [
            f"`{n}`",
            str(t.skill_loads.get(n, 0)),
            str(t.skill_sessions.get(n, 0)),
            str(t.skill_denied.get(n, 0)),
            f"{t.skill_credits.get(n, 0.0):.2f}",
            str(t.skill_file_reads.get(n, 0)),
        ]
        for n in names
    ]


def _steering_rows(t: UsageTotals, available: Sequence[str]) -> list[list[str]]:
    names = sorted(set(available) | set(t.steering_events))
    return [
        [f"`{n}`", str(t.steering_events.get(n, 0)), str(t.steering_sessions.get(n, 0))]
        for n in names
    ]


def render_report(
    totals: UsageTotals,
    stamp: str,
    skills_available: Sequence[str],
    steering_available: Sequence[str],
    harness: str,
) -> list[str]:
    """Markdown lines. Only counts, ratios, dates and names from the skills and steering dirs."""
    assert stamp.strip() and harness in HARNESSES, "a stamp and a known harness"
    store = "Claude Code transcripts" if harness == "claude" else "Kiro session store"
    t = totals
    shell_paired = sum(t.tool_paired.get(n, 0) for n in SHELL_TOOLS)
    shell_bad = sum(t.tool_fails.get(n, 0) for n in SHELL_TOOLS)
    skipped_total = sum(t.skipped.values())
    out = [f"# Usage report ({stamp})", ""]
    out += [
        f"Produced by `eval mine` (read-only over the {store}). Counts only: no message text,",
        "args, commands, paths or titles are read into the output.",
        "",
        "## Sessions",
        "",
        *_table(
            ["Measure", "Count"],
            [
                ["seen", str(t.mined + skipped_total)],
                ["mined", str(t.mined)],
                *[[f"mined, {k}", str(v)] for k, v in sorted(t.mined_by_tree.items())],
                *[[f"skipped, {k}", str(v)] for k, v in sorted(t.skipped.items())],
                ["first session day", t.first_day or "n/a"],
                ["last session day", t.last_day or "n/a"],
                ["parse errors (corrupt lines)", str(t.parse_errors)],
                ["unpaired tool calls (no result)", str(t.unpaired)],
            ],
        ),
        "",
        "## Tool calls",
        "",
        f"Shell (`execute_bash`) error rate: {shell_bad}/{shell_paired} = "
        f"{_rate(shell_bad, shell_paired)}. " + _SHELL_ERROR_NOTE[harness],
        "",
        *_table(["Tool", "Calls", "With result", "Errors", "Error rate", "Retries"], _tool_rows(t)),
        "",
        "Retries: same tool and same argument hash within the last "
        f"{MINER_RETRY_WINDOW_CALLS} calls of one turn.",
        "",
    ]
    out += _render_skills_and_rest(t, stamp, skills_available, steering_available, harness)
    assert out[-1].startswith("mine: "), "the status line is last"
    return out


def _render_skills_and_rest(
    t: UsageTotals,
    stamp: str,
    skills_available: Sequence[str],
    steering_available: Sequence[str],
    harness: str,
) -> list[str]:
    assert stamp.strip() and harness in HARNESSES, "a stamp and a known harness"
    never = sorted(s for s in skills_available if t.skill_loads.get(s, 0) == 0)
    skipped_total = sum(t.skipped.values())
    out = [
        "## Skills",
        "",
        _SKILLS_NOTE[harness],
        "",
        *_table(
            ["Skill", "Loads", "Sessions", "Denied", "Turn credits", "File reads"],
            _skill_rows(t, skills_available),
        ),
        "",
        f"Never loaded ({len(never)} of {len(skills_available)}): "
        + (", ".join(f"`{s}`" for s in never) if never else "none"),
        "",
        *_rest_lines(t, steering_available, harness, skipped_total),
    ]
    assert out[0].startswith("## Skills"), "starts with the skills section"
    assert out[-1].startswith("mine: "), "ends with the status line"
    return out


def _kiro_rest(t: UsageTotals, steering_available: Sequence[str], skipped_total: int) -> list[str]:
    assert skipped_total >= 0, "a count"
    return [
        "## Steering files",
        "",
        *_table(["File", "Inclusion events", "Sessions"], _steering_rows(t, steering_available)),
        "",
        "## Credits",
        "",
        *_table(
            ["Tree", "Credits"], [[k, f"{v:.2f}"] for k, v in sorted(t.credits_by_tree.items())]
        ),
        "",
        f"Total credits over mined sessions: {t.credits:.2f}.",
        "",
        "## Limits of this data",
        "",
        "- Credits per skill are the full turn's credits, not the skill's own cost.",
        "- A `steering_inclusion` event says the file was listed, not that its text arrived.",
        "- Eval traffic is skipped by session cwd under `/tmp` (macOS `/private/tmp`); real work "
        "done in `/tmp` is skipped with it. The headless tree holds probe sessions run in `/tmp`.",
        "- Workflow and interactive sessions are reported apart; sub-executions are folded into "
        "their parent session.",
        "- Model routing is `Auto`, so no per-model split is possible.",
        "- Sub-execution credits are added to their session as logged; whether they overlap the "
        "parent turn's own credits is not verified.",
        "- Steering names outside `~/.kiro/steering` (for example a workspace `project-rules`) "
        f"print as `{OTHER}`.",
        "- A tool name outside `[a-z0-9_.:-]` and an unknown skill or steering name print as "
        f"`{OTHER}`. A lowercase tool name that matches the pattern prints as is, so a "
        "model-invented tool name can appear.",
        "",
        f"mine: mined={t.mined} skipped={skipped_total} parse_errors={t.parse_errors} "
        f"unpaired={t.unpaired} credits={t.credits:.2f}",
    ]


def _claude_rest(t: UsageTotals, skipped_total: int) -> list[str]:
    assert skipped_total >= 0 and t.credits == 0.0, "Claude transcripts carry no credits"
    return [
        "## Steering files",
        "",
        "n/a: Claude Code loads every always-on rule in every session (CLAUDE.md imports), so "
        "there is no per-file inclusion event to count.",
        "",
        "## Credits",
        "",
        "n/a: transcripts record tokens, not cost. The trajectory tool reports tokens per turn.",
        "",
        "## Limits of this data",
        "",
        "- Eval traffic is skipped by session cwd under `/tmp` (macOS `/private/tmp`); real work "
        "done in `/tmp` is skipped with it.",
        "- Sub-agent transcripts are folded into their parent session.",
        "- A permission denial is not recorded on the call, so a denied `Skill` call counts as "
        "a load.",
        "- Tool names map onto the names the eval cases use (`Bash` is `execute_bash`); a tool "
        "with no mapping, MCP tools included, prints lowercased.",
        f"- A tool name outside `[a-z0-9_.:-]` and an unknown skill name print as `{OTHER}`.",
        "",
        f"mine: mined={t.mined} skipped={skipped_total} parse_errors={t.parse_errors} "
        f"unpaired={t.unpaired} credits=n/a",
    ]


def _rest_lines(
    t: UsageTotals, steering_available: Sequence[str], harness: str, skipped_total: int
) -> list[str]:
    """Steering, credits, limits and the status line, which differ by harness."""
    assert harness in HARNESSES, "a known harness"
    out = (
        _claude_rest(t, skipped_total)
        if harness == "claude"
        else _kiro_rest(t, steering_available, skipped_total)
    )
    assert out[-1].startswith("mine: "), "the status line is last"
    return out


_SHELL_ERROR_NOTE: Final = {
    "kiro": "An error is `success == false` or a trailing `Exit Code: N` with N != 0; fish "
    "messages merely quoted inside output do not count.",
    "claude": "An error is a result marked `is_error`, which a non-zero exit sets.",
}
_SKILLS_NOTE: Final = {
    "kiro": "Loads are `disclose_context` calls not denied. `file reads` are `read_file` of "
    "`skills/<name>/SKILL.md`, kept apart. Credits are the whole turn's credits for every "
    "turn that loaded the skill (approximate).",
    "claude": "Loads are `Skill` calls. `file reads` are `Read` calls of "
    "`skills/<name>/SKILL.md`, kept apart. Transcripts carry no cost, so turn credits are 0.",
}


# ---------------------------------------------------------------- command


def _parse_mine_args(argv: list[str]) -> dict[str, str] | str:
    if len(argv) % 2:
        return MINE_USAGE
    options = dict(zip(argv[::2], argv[1::2], strict=True))
    if len(options) != len(argv) // 2 or not set(options) <= MINE_FLAGS:
        return MINE_USAGE
    if not options.get("--stamp", "").strip():
        return MINE_USAGE
    if "--home" in options and "--kiro-home" in options:
        return "give --home or --kiro-home, not both"
    for flag in ("--lines-max", "--bytes-max"):
        if flag in options and not (options[flag].isdigit() and int(options[flag]) > 0):
            return f"{flag} must be a positive integer"
    assert "--stamp" in options, "the stamp is required"
    assert set(options) <= MINE_FLAGS, "only known flags survive"
    return options


def _list_names(directory: Path, want_dirs: bool) -> list[str]:
    assert isinstance(want_dirs, bool), "a flag"
    assert directory.name, "a named directory"
    if not directory.is_dir():
        return []
    if want_dirs:
        return sorted(
            p.name for p in directory.iterdir() if p.is_dir() and not p.name.startswith(".")
        )
    return sorted(p.name.removesuffix(".md") for p in directory.glob("*.md"))


def _mine_all(
    refs: list[SessionRef],
    skills: frozenset[str],
    steering: frozenset[str],
    lines_max: int,
    bytes_max: int,
    eval_roots: Sequence[Path],
) -> tuple[list[SessionFacts], dict[str, int]] | str:
    facts: list[SessionFacts] = []
    assert lines_max > 0 and bytes_max > 0, "bounds are positive"
    assert len(refs) <= MINER_SESSIONS_MAX, "find_sessions enforced the session bound"
    skipped: Counter[str] = Counter()
    for ref in refs:
        reason = ref.skip_reason or (SKIP_EVAL if is_eval_traffic(ref.cwds, eval_roots) else "")
        if reason:
            skipped[reason] += 1
            continue
        mined = mine_session(ref, skills, steering, lines_max, bytes_max)
        if isinstance(mined, str):
            return mined  # fail closed: a partial count would look like a real one
        facts.append(mined)
    assert len(facts) + sum(skipped.values()) == len(refs), "every session accounted for"
    return facts, dict(skipped)


def _find_refs(harness: str, home: Path, sessions: str | None) -> list[SessionRef] | str:
    assert harness in HARNESSES, "a known harness"
    if harness == "claude":
        projects = Path(sessions).expanduser() if sessions else home / "projects"
        return find_claude_sessions(projects, MINER_SESSIONS_MAX)
    store = Path(sessions).expanduser() if sessions else home / "sessions"
    return find_sessions(store, MINER_SESSIONS_MAX)


def run_mine(argv: list[str], root: Path, homes: dict[str, Path]) -> tuple[list[str], int]:
    """`eval mine`: (output lines, exit code). Exit 1 on an unreadable store or over-bound file
    (no report written) and on parse errors (report written, status says so). homes maps each
    harness to its default config folder."""
    assert root.is_dir() and set(homes) == set(HARNESSES), "root is a dir, a home per harness"
    options = _parse_mine_args(argv)
    if isinstance(options, str):
        return [options], EXIT_USAGE
    harness = options.get("--harness", "claude")
    if harness not in HARNESSES:
        return [MINE_USAGE], EXIT_USAGE
    given = options.get("--home", options.get("--kiro-home"))
    home = Path(given).expanduser() if given else homes[harness]
    refs = _find_refs(harness, home, options.get("--sessions"))
    if isinstance(refs, str):
        return [f"mine: error {refs}"], EXIT_BAD
    skills_available, steering_available = (
        _list_names(home / "skills", True),
        _list_names(home / "steering", False),
    )
    mined = _mine_all(
        refs,
        frozenset(skills_available),
        frozenset(steering_available),
        int(options.get("--lines-max", MINER_LINES_PER_FILE_MAX)),
        int(options.get("--bytes-max", MINER_FILE_BYTES_MAX)),
        [Path("/tmp").resolve()],
    )
    if isinstance(mined, str):
        return [f"mine: error {mined}"], EXIT_BAD
    facts, skipped = mined
    totals = replace(merge(facts), skipped=skipped)
    lines = render_report(totals, options["--stamp"], skills_available, steering_available, harness)
    if "--out" in options:
        out = Path(options["--out"]).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        lines = [lines[-1]]
    code = EXIT_BAD if totals.parse_errors else EXIT_OK
    assert lines[-1].startswith("mine: "), "the status line is last"
    return lines, code
