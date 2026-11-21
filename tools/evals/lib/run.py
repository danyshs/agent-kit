"""One kiro-cli v3 invocation per turn, stream parse, session-log join, contamination check,
budget check (design 3.2, 3.3, 3.8, 5.2, 5.3).

Expected failures (bad stream, missing session log, timeout) become RunStatus values or a str
reason; programmer errors are asserts. The subprocess is injected (`Invoker`) so tests replay
recorded streams without a model.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final, Literal, cast

from lib.dataset import Arm, Case
from lib.home import HomeDir
from lib.isolation import isolated_environ, sandbox_settings, scoped_allowed_tools

Verdict = Literal["PASS", "FAIL", "UNKNOWN"]
RunStatus = Literal["ok", "timeout", "harness_error", "budget_abort", "denied_only", "contaminated"]
Tier = Literal["smoke", "standard"]

ENGINE: Final = "v3"  # the Kiro engine; kept as the spec's engine text so old ledgers still read
KIRO_ENGINE: Final = ENGINE
CLAUDE_ENGINE: Final = "claude"
ENGINES: Final = (CLAUDE_ENGINE, KIRO_ENGINE)
COST_UNITS: Final = {CLAUDE_ENGINE: "usd", KIRO_ENGINE: "credits"}
CLAUDE_DEFAULT_MODEL: Final = "claude-haiku-5-5"
CLAUDE_SESSION_ID_RE: Final = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
# Claude Code tool names in the cases' tool vocabulary, which came first (Kiro's); unmapped names
# pass through, so a grader asking for a Kiro name never matches a tool Claude has no twin for
CLAUDE_TOOL_NAMES: Final = {
    "Bash": "execute_bash",
    "Read": "read_file",
    "Write": "fs_write",
    "Edit": "str_replace",
    "MultiEdit": "str_replace",
    "Glob": "code",
    "Grep": "code",
    "WebFetch": "web_fetch",
    "WebSearch": "remote_web_search",
    "Skill": "disclose_context",
    "Agent": "invoke_sub_agent",
    "Task": "invoke_sub_agent",
}
# case tool name -&gt; the --allowedTools name; same keys as trust_names.json
CLAUDE_TRUST_NAMES: Final = {
    "execute_bash": "Bash",
    "read_file": "Read",
    "fs_write": "Write",
    "str_replace": "Edit",
    "disclose_context": "Skill",
}
# rate_limit_event: the subscription's quota state, seen on a real run (claude 2026-11-20); ignored
CLAUDE_LINE_TYPES: Final = frozenset({"system", "assistant", "user", "result", "rate_limit_event"})
CLAUDE_CAPPED_SUBTYPE: Final = "error_max_budget_usd"  # claude -p stopped at --max-budget-usd
CLAUDE_PATH_KEYS: Final = ("file_path", "path", "notebook_path")
CLAUDE_BLOCKS_MAX: Final = 256  # content blocks per message
CLAUDE_DENIALS_MAX: Final = 256
CLAUDE_MD_ENV: Final = "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"  # read CLAUDE.md in --add-dir
CLAUDE_BUDGET_USD_MIN: Final = 0.01  # a turn always gets some cap, never 0 (= no cap)
# no user/project settings, CLAUDE.md, hooks, plugins or MCP: the arm's home is the only input
CLAUDE_ISOLATION: Final = (
    "--setting-sources",
    "",
    "--strict-mcp-config",
    "--mcp-config",
    '{"mcpServers":{}}',
    "--permission-mode",
    "dontAsk",  # anything not in --allowedTools is denied, never prompted
)
SKILL_ID_PREFIX: Final = "skill::"
# the stream's ACP toolId names a skill load; step 4 pilot: the session log named that same call
# differently (the model called it load_resource), so for these the stream name wins
SKILL_LOAD_TOOLS: Final = ("disclose_context",)
TIERS: Final = ("smoke", "standard")
DEFAULT_MODEL: Final = "claude-haiku-5.5"
SESSION_ID_RE: Final = re.compile(r"sess_[0-9a-f-]{8,64}")
VERSION_RE: Final = re.compile(r"\d+\.\d+\.\d+")
SKILL_REF_PATH_RE: Final = re.compile(r"/skills/([^/]+)/references/")
VERSION_TIMEOUT_S: Final = 10.0
MS_PER_S: Final = 1000
# sessionUpdate kinds seen on v3 (probe_results.md); anything else fails the parse (3.2)
UPDATE_KINDS: Final = frozenset(
    "agent_message_chunk agent_thought_chunk tool_call tool_call_update session_info_update "
    "config_option_update available_commands_update".split()
)
TERMINAL_STATUSES: Final = ("completed", "failed")
ARGS_PATHS_MAX: Final = 64  # locations read per tool call
FAILURE_STATUSES: Final = ("harness_error", "timeout", "budget_abort")


@dataclass(frozen=True)
class TierLimits:
    repeats: int
    run_credits_max: float
    run_wall_ms_max: int
    suite_credits_max: float


@dataclass(frozen=True)
class RunLimits:
    stream_bytes_max: int
    harness_errors_max: int
    home_files_max: int
    user_turns_max: int
    probe_credits_max: float
    probe_wall_ms_max: int
    tiers: dict[str, TierLimits]
    model_run_credits_max: dict[str, float] = field(default_factory=lambda: {})
    cost_unit: str = "credits"  # "usd" for Claude: every *credits* field then holds dollars


@dataclass(frozen=True)
class Event:
    index: int
    kind: str  # tool_call tool_result skill_read skill_ref_read assistant_text usage run_end
    name: str | None
    args_json: str | None
    status: str | None
    text: str | None
    call_id: str | None
    paths: tuple[str, ...]  # locations[].path and rawInput.path of a tool_call, as the stream said


@dataclass(frozen=True)
class RunSpec:
    run_id: str
    case_id: str
    arm: Arm
    repeat_index: int
    engine: str
    model_id: str
    cli_version: str
    home_sha: str
    trust_tools: tuple[str, ...]  # the --trust-tools names, already mapped
    credits_max: float
    wall_ms_max: int


@dataclass(frozen=True)
class Grade:
    assertion_id: str
    verdict: Verdict
    grader: str
    evidence: str


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    case_id: str
    arm: Arm
    repeat_index: int
    engine: str
    model_id: str
    cli_version: str
    home_sha: str
    status: RunStatus
    wall_ms: int
    credits: float | None
    context_pct_last: float | None
    tool_call_count: int
    denied_count: int
    skills_read: tuple[str, ...]
    session_id: str | None
    stream_path: str
    grades: tuple[Grade, ...]
    case_passed: bool
    note: str  # why a run is harness_error or aborted; empty otherwise


@dataclass(frozen=True)
class RunView:
    spec: RunSpec
    status: RunStatus
    events: tuple[Event, ...]
    final_text: str
    stderr_text: str
    home_dir: str
    cwd: str


@dataclass(frozen=True)
class Ceiling:
    run_count: int
    credits_max_total: float
    wall_ms_max_total: int


@dataclass(frozen=True)
class Turn:
    """One parsed invocation."""

    events: tuple[Event, ...]
    final_text: str
    session_id: str
    credits: float | None
    context_pct: float | None
    capped: bool = False  # the CLI itself stopped at its budget cap


@dataclass(frozen=True)
class LogCall:
    tool_name: str
    status: str  # approved denied completed


@dataclass(frozen=True)
class InvokeResult:
    stdout_text: str
    stderr_text: str
    exit_code: int | None
    timed_out: bool
    wall_ms: int


Invoker = Callable[[list[str], Mapping[str, str], Path, float], InvokeResult]


def arm_text(arm: Arm) -> str:
    """The arm as the dataset spells it: 'with', 'full', 'minus-<file>'."""
    assert (arm.kind == "minus") == (arm.minus_file is not None), "minus_file set iff minus"
    text = f"minus-{arm.minus_file}" if arm.minus_file else arm.kind
    assert text, "an arm always has text"
    return text


# ---------------------------------------------------------------- limits and planning


def _obj(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    raw = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in raw):
        return None
    return cast(dict[str, object], raw)


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _positive_int(raw: dict[str, object], key: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"limits.json: {key} must be a positive integer")
    return value


def _optional_positive(raw: dict[str, object], key: str, *, whole: bool) -> float | None:
    """null stays None (unset until the user sets it); anything else must be positive."""
    value = raw.get(key)
    if value is None:
        return None
    number = _num(value)
    if number is None or number <= 0 or (whole and number != int(number)):
        raise ValueError(f"limits.json: {key} must be null or a positive number")
    assert number > 0, "an accepted value is positive"
    assert not whole or number == int(number), "a whole-number value has no fraction"
    return number


def _required_positive(raw: dict[str, object], key: str, tier: str, *, whole: bool) -> float:
    """A tier cap: every tier ships with numbers, so a null or missing cap is an error."""
    number = _optional_positive(raw, key, whole=whole)
    if number is None:
        raise ValueError(f"limits.json: tiers.{tier}.{key} must be set; every tier has a cap")
    assert number > 0, "a required cap is positive"
    return number


def _load_tier(name: str, raw: object, cost: str) -> TierLimits:
    """cost is the key word for the engine's unit: CREDITS (Kiro) or USD (Claude)."""
    entry = _obj(raw)
    if entry is None:
        raise ValueError(f"limits.json: tiers.{name} must be an object")
    tier = TierLimits(
        repeats=_positive_int(entry, "repeats"),
        run_credits_max=_required_positive(entry, f"RUN_{cost}_MAX", name, whole=False),
        run_wall_ms_max=int(_required_positive(entry, "RUN_WALL_MS_MAX", name, whole=True)),
        suite_credits_max=_required_positive(entry, f"SUITE_{cost}_MAX", name, whole=False),
    )
    assert tier.repeats > 0, "repeats is positive"
    assert tier.run_credits_max > 0 and tier.run_wall_ms_max > 0, "run caps are positive"
    return tier


def _load_model_caps(raw: object, key: str) -> dict[str, float]:
    """Per-model run-cap overrides (3.8); absent means none."""
    if raw is None:
        return {}
    caps = _obj(raw)
    if caps is None:
        raise ValueError(f"limits.json: {key} must be an object")
    out: dict[str, float] = {}
    for model, value in caps.items():
        number = _num(value)
        if number is None or number <= 0:
            raise ValueError(f"limits.json: {key}.{model} must be positive")
        out[model] = number
    assert all(cap > 0 for cap in out.values()), "every model cap is positive"
    return out


def load_run_limits(path: Path, engine: str = KIRO_ENGINE) -> RunLimits:
    """Step-2 constants from limits.json; ValueError on any bad entry. Claude's dollar caps and
    tiers live in its own "claude" object; the byte and count bounds are shared."""
    assert path.suffix == ".json", "limits are a json file"
    assert engine in ENGINES, "engine is validated by the parser"
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as err:
        raise ValueError(f"{path}: {err}") from err
    raw = _obj(value)
    cost_raw = (_obj(raw.get(CLAUDE_ENGINE)) if engine == CLAUDE_ENGINE else raw) if raw else None
    cost = "USD" if engine == CLAUDE_ENGINE else "CREDITS"
    tiers_raw = _obj(cost_raw.get("tiers")) if cost_raw else None
    if raw is None or cost_raw is None or tiers_raw is None:
        raise ValueError(f"{path}: top level, {engine} limits and tiers must be objects")
    probe_credits = _optional_positive(cost_raw, f"PROBE_RUN_{cost}_MAX", whole=False)
    probe_wall = _optional_positive(cost_raw, "PROBE_RUN_WALL_MS_MAX", whole=True)
    if probe_credits is None or probe_wall is None:
        raise ValueError(f"{path}: the PROBE_RUN_* floors must be set")
    limits = RunLimits(
        stream_bytes_max=_positive_int(raw, "STREAM_BYTES_MAX"),
        harness_errors_max=_positive_int(raw, "CONSECUTIVE_HARNESS_ERRORS_MAX"),
        home_files_max=_positive_int(raw, "HOME_FILES_MAX"),
        user_turns_max=_positive_int(raw, "MAX_USER_TURNS"),
        probe_credits_max=probe_credits,
        probe_wall_ms_max=int(probe_wall),
        tiers={name: _load_tier(name, item, cost) for name, item in tiers_raw.items()},
        model_run_credits_max=_load_model_caps(
            cost_raw.get(f"MODEL_RUN_{cost}_MAX"), f"MODEL_RUN_{cost}_MAX"
        ),
        cost_unit=COST_UNITS[engine],
    )
    assert set(limits.tiers) >= set(TIERS), "smoke and standard tiers are defined"
    assert limits.probe_credits_max > 0, "probe cap is positive"
    return limits


def plan_runs(
    cases: list[Case],
    tier: Tier,
    model_id: str,
    cli_version: str,
    limits: RunLimits,
    *,
    trust_names: Mapping[str, str],
    stamp_s: int,
    probe: bool,
    arm_filter: str | None,
    case_filter: str | None = None,
    repeats_override: int | None = None,
    engine: str = KIRO_ENGINE,
) -> list[RunSpec]:
    """One RunSpec per case x arm x repeat. home_sha is filled when the home is built."""
    assert tier in limits.tiers, "tier is defined in limits.json"
    assert model_id and cli_version, "model and cli version are pinned"
    tier_limits = limits.tiers[tier]
    credits_max = limits.probe_credits_max if probe else tier_limits.run_credits_max
    if not probe and model_id in limits.model_run_credits_max:
        credits_max = limits.model_run_credits_max[model_id]
    wall_ms_max = limits.probe_wall_ms_max if probe else tier_limits.run_wall_ms_max
    specs: list[RunSpec] = []
    for case in cases:
        if case.tier not in (None, tier):
            continue
        if case_filter is not None and not fnmatch.fnmatchcase(case.id, case_filter):
            continue  # glob; an exact id is a glob that matches itself
        repeats = (
            tier_limits.repeats if case.repeats is None else min(case.repeats, tier_limits.repeats)
        )
        if repeats_override is not None:
            repeats = repeats_override
        trust = tuple(trust_names[tool] for tool in case.trust_tools)
        for arm in case.arms:
            if arm_filter is not None and arm_text(arm) != arm_filter:
                continue
            for repeat_index in range(repeats):
                run_id = f"{case.id}--{arm_text(arm)}--r{repeat_index}--{stamp_s}"
                specs.append(
                    RunSpec(
                        run_id,
                        case.id,
                        arm,
                        repeat_index,
                        engine,
                        model_id,
                        cli_version,
                        "",
                        trust,
                        credits_max,
                        wall_ms_max,
                    )
                )
    assert len({spec.run_id for spec in specs}) == len(specs), "run ids are unique"
    assert engine in ENGINES, "every spec carries a known engine"
    return specs


def worst_case(specs: list[RunSpec]) -> Ceiling:
    ceiling = Ceiling(
        run_count=len(specs),
        credits_max_total=sum(spec.credits_max for spec in specs),
        wall_ms_max_total=sum(spec.wall_ms_max for spec in specs),
    )
    assert ceiling.run_count >= 0 and ceiling.credits_max_total >= 0, "ceiling is not negative"
    assert ceiling.wall_ms_max_total >= 0, "wall ceiling is not negative"
    return ceiling


def check_budget(
    ceiling: Ceiling, tier: Tier, limits: RunLimits, *, probe: bool, model_id: str = ""
) -> str | None:
    """None when the plan may start, else the reason it may not (3.8)."""
    assert tier in limits.tiers, "tier is defined in limits.json"
    assert ceiling.run_count >= 0, "run count is not negative"
    if ceiling.run_count == 0:
        return "the plan has no runs"
    if probe:
        # --probe-run: the 0.2-credit cap is TOTAL across arms, enforced by the run loop
        return None if tier == "smoke" else "--probe-run is for the smoke tier only"
    tier_limits = limits.tiers[tier]
    if model_id in limits.model_run_credits_max:
        return None  # suite cap = model run cap x planned runs: the ceiling by construction
    if ceiling.credits_max_total > tier_limits.suite_credits_max:
        unit = limits.cost_unit
        return f"ceiling {ceiling.credits_max_total} {unit} is over SUITE_{unit.upper()}_MAX"
    return None


# ---------------------------------------------------------------- stream parse


@dataclass
class _Acc:
    events: list[Event] = field(default_factory=lambda: [])
    chunks: list[str] = field(default_factory=lambda: [])
    credits: float | None = None
    context_pct: float | None = None
    engine: str | None = None
    finished: dict[str, object] | None = None


def _kiro_meta(update: dict[str, object]) -> dict[str, object]:
    meta = _obj(update.get("_meta"))
    kiro = _obj(meta.get("kiro")) if meta else None
    return kiro or {}


def _event_paths(update: dict[str, object]) -> tuple[str, ...]:
    paths: list[str] = []
    locations = update.get("locations")
    for item in (
        cast(list[object], locations)[:ARGS_PATHS_MAX] if isinstance(locations, list) else []
    ):
        location = _obj(item)
        if location and isinstance(location.get("path"), str):
            paths.append(cast(str, location["path"]))
    raw_input = _obj(update.get("rawInput"))
    if raw_input and isinstance(raw_input.get("path"), str):
        paths.append(cast(str, raw_input["path"]))
    assert all(isinstance(path, str) and path for path in paths), "paths are non-empty strings"
    assert len(paths) <= 2 * ARGS_PATHS_MAX, "a call names a handful of paths"
    return tuple(dict.fromkeys(paths))


def _tool_event(update: dict[str, object], *, is_call: bool) -> Event | None:
    call_id, status = update.get("toolCallId"), update.get("status")
    assert is_call or status is None or isinstance(status, str), "updates carry a string status"
    if not isinstance(call_id, str):
        return None
    raw_input = update.get("rawInput")
    args_json = json.dumps(raw_input, sort_keys=True) if isinstance(raw_input, dict) else None
    if is_call:
        tool_id = _kiro_meta(update).get("toolId")  # ACP-origin tools name themselves here
        name = tool_id if isinstance(tool_id, str) else None
        return Event(0, "tool_call", name, args_json, None, None, call_id, _event_paths(update))
    if status not in TERMINAL_STATUSES:
        return None  # pending / in_progress updates carry no outcome
    raw_output = _obj(update.get("rawOutput"))
    message = raw_output.get("message") if raw_output else None
    text = message if isinstance(message, str) else None
    assert status in TERMINAL_STATUSES, "only terminal updates become results"
    return Event(0, "tool_result", None, None, cast(str, status), text, call_id, ())


def _take_info(update: dict[str, object], acc: _Acc) -> None:
    kiro = _kiro_meta(update)
    kind = kiro.get("kind")
    events_before = len(acc.events)
    if kind == "context_usage":
        usage = _obj(kiro.get("contextUsage"))
        pct = _num(usage.get("usagePercentage")) if usage else None
        acc.context_pct = pct if pct is not None else acc.context_pct
    elif kind == "turn_completion":
        summaries = kiro.get("promptTurnSummaries")
        first = (
            _obj(cast(list[object], summaries)[0])
            if isinstance(summaries, list) and summaries
            else None
        )
        credits = _num(first.get("usage")) if first else None
        if credits is not None:
            acc.credits = (acc.credits or 0.0) + credits
        info = {"credits": credits, "elapsed_ms": kiro.get("elapsedTime")}
        acc.events.append(
            Event(
                0,
                "usage",
                "turn_completion",
                json.dumps(info, sort_keys=True),
                None,
                None,
                None,
                (),
            )
        )
    assert len(acc.events) - events_before <= 1, "one info update adds at most one event"
    assert acc.credits is None or acc.credits >= 0, "credits never go negative"


def _take_update(update: dict[str, object], acc: _Acc) -> str | None:
    """Fold one sessionUpdate into acc; a str is why the shape is unknown."""
    kind = update.get("sessionUpdate")
    assert isinstance(update, dict), "update is a decoded object"
    if kind not in UPDATE_KINDS:
        return f"unknown sessionUpdate {kind!r}"
    if kind in ("tool_call", "tool_call_update"):
        event = _tool_event(update, is_call=kind == "tool_call")
        if event is None and kind == "tool_call":
            return "tool_call without toolCallId"
        if event is not None:
            acc.events.append(event)
    elif kind == "agent_message_chunk":
        content = _obj(update.get("content"))
        text = content.get("text") if content else None
        acc.chunks.append(text if isinstance(text, str) else "")
    elif kind == "session_info_update":
        _take_info(update, acc)
    assert kind in UPDATE_KINDS, "only known kinds reach here"
    return None


def _take_event(obj: dict[str, object], acc: _Acc) -> str | None:
    kind, data = obj.get("type"), _obj(obj.get("data"))
    assert isinstance(obj, dict), "obj is a decoded object"
    assert acc.finished is None or kind != "runStarted", "runStarted comes before runFinished"
    if data is None:
        return "event without a data object"
    if kind == "runStarted":
        engine = data.get("engine")
        acc.engine = engine if isinstance(engine, str) else None
        return None if acc.engine == ENGINE else f"engine {engine!r}, expected {ENGINE!r}"
    if kind == "sessionUpdate":
        update = _obj(data.get("update"))
        return "sessionUpdate without update" if update is None else _take_update(update, acc)
    if kind == "runFinished":
        acc.finished = data
        return None
    if kind == "runError":
        return f"runError stage={data.get('stage')!r}: {data.get('message')!r}"
    return f"unknown event type {kind!r}"


def parse_stream(stdout_text: str) -> Turn | str:
    """JSON Lines of one invocation into a Turn; a str is why it is not a clean v3 stream.

    The caller bounds stdout_text by STREAM_BYTES_MAX, which bounds the line loop.
    """
    lines = [line for line in stdout_text.splitlines() if line.strip()]
    assert isinstance(stdout_text, str), "stdout is decoded text"
    acc = _Acc()
    for number, line in enumerate(lines, start=1):
        try:
            obj = _obj(json.loads(line))
        except json.JSONDecodeError:
            obj = None
        if obj is None:
            return f"line {number}: not a json object"
        problem = _take_event(obj, acc)
        if problem:
            return f"line {number}: {problem}"
    if acc.engine != ENGINE or acc.finished is None:
        return "stream has no runStarted(v3) or no runFinished"
    session_id, status = acc.finished.get("sessionId"), acc.finished.get("status")
    final = acc.finished.get("finalText")
    if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
        return f"bad sessionId {session_id!r}"
    if status != "success" or not isinstance(final, str):
        return f"runFinished status {status!r}"
    if acc.finished.get("finalTextTruncated") is True and len("".join(acc.chunks)) > len(final):
        final = "".join(acc.chunks)
    acc.events.append(Event(0, "assistant_text", None, None, None, final, None, ()))
    acc.events.append(
        Event(0, "run_end", None, None, str(status), str(acc.finished.get("stopReason")), None, ())
    )
    assert any(e.kind == "run_end" for e in acc.events), "a clean stream ends with run_end"
    return Turn(tuple(acc.events), final, session_id, acc.credits, acc.context_pct)


# ---------------------------------------------------------------- claude stream parse


@dataclass
class _ClaudeAcc:
    events: list[Event] = field(default_factory=lambda: [])
    names: dict[str, str] = field(default_factory=lambda: {})  # call id -> case tool name
    session_id: str | None = None
    result: dict[str, object] | None = None


def _claude_paths(tool_input: dict[str, object]) -> tuple[str, ...]:
    paths = [tool_input[key] for key in CLAUDE_PATH_KEYS if isinstance(tool_input.get(key), str)]
    assert len(paths) <= len(CLAUDE_PATH_KEYS), "at most one path per key"
    return tuple(dict.fromkeys(cast(list[str], [p for p in paths if p])))


def _claude_result_text(content: object) -> str | None:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    blocks = [_obj(item) for item in cast(list[object], content)[:CLAUDE_BLOCKS_MAX]]
    texts = [b.get("text") for b in blocks if b and b.get("type") == "text"]
    return "\n".join(text for text in texts if isinstance(text, str))


def _take_claude_block(block: dict[str, object], acc: _ClaudeAcc) -> str | None:
    kind = block.get("type")
    if kind == "tool_use":
        call_id, name, tool_input = block.get("id"), block.get("name"), _obj(block.get("input"))
        if not isinstance(call_id, str) or not isinstance(name, str) or tool_input is None:
            return "tool_use without id, name or input"
        mapped = CLAUDE_TOOL_NAMES.get(name, name)
        acc.names[call_id] = mapped
        args_json = json.dumps(tool_input, sort_keys=True)
        paths = _claude_paths(tool_input)
        acc.events.append(Event(0, "tool_call", mapped, args_json, None, None, call_id, paths))
    elif kind == "tool_result":
        call_id = block.get("tool_use_id")
        if not isinstance(call_id, str):
            return "tool_result without tool_use_id"
        status = "failed" if block.get("is_error") is True else "completed"
        text = _claude_result_text(block.get("content"))
        name = acc.names.get(call_id)
        acc.events.append(Event(0, "tool_result", name, None, status, text, call_id, ()))
    assert kind != "tool_use" or acc.events[-1].kind == "tool_call", "a call became an event"
    return None  # text and thinking blocks: the final text comes from the result line


def _take_claude_line(obj: dict[str, object], acc: _ClaudeAcc) -> str | None:
    """Fold one stream-json line into acc; a str is why the stream is not trusted."""
    kind = obj.get("type")
    if kind not in CLAUDE_LINE_TYPES:
        return f"unknown line type {kind!r}"
    if acc.result is not None:
        return "a line after the result line"
    session_id = obj.get("session_id")
    if isinstance(session_id, str):
        if acc.session_id not in (None, session_id):
            return "session_id changed mid-stream"
        acc.session_id = session_id
    if kind == "result":
        acc.result = obj
        assert acc.result is obj, "the result line is kept as the run's verdict"
        return None
    message = _obj(obj.get("message")) if kind in ("assistant", "user") else None
    content = message.get("content") if message else None
    if not isinstance(content, list):
        return None  # system lines, and a user line that echoes a plain prompt
    blocks = cast(list[object], content)
    if len(blocks) > CLAUDE_BLOCKS_MAX:
        return (
            f"over {CLAUDE_BLOCKS_MAX} content blocks in one message"  # cutting could hide a call
        )
    for item in blocks:
        block = _obj(item)
        problem = _take_claude_block(block, acc) if block else "a content block is not an object"
        if problem:
            return problem
    return None


def _denied_ids(result: dict[str, object]) -> frozenset[str]:
    denials = result.get("permission_denials")
    items = cast(list[object], denials)[:CLAUDE_DENIALS_MAX] if isinstance(denials, list) else []
    ids = [d.get("tool_use_id") for d in (_obj(item) for item in items) if d]
    return frozenset(i for i in ids if isinstance(i, str))


def _mark_denied(events: list[Event], denied: frozenset[str]) -> tuple[Event, ...]:
    """Call status as Kiro's session log gives it (approved/denied); denied results say so."""
    out: list[Event] = []
    for event in events:
        if event.kind == "tool_call":
            event = replace(event, status="denied" if event.call_id in denied else "approved")
        elif event.kind == "tool_result" and event.call_id in denied:
            event = replace(event, status="denied")
        out.append(event)
    assert len(out) == len(events), "marking changes events, never their count"
    return tuple(out)


def parse_claude_stream(stdout_text: str) -> Turn | str:
    """claude -p stream-json lines into a Turn; a str is why it is not a clean stream. The cost
    is dollars (total_cost_usd), carried in Turn.credits. The caller bounds stdout_text."""
    lines = [line for line in stdout_text.splitlines() if line.strip()]
    acc = _ClaudeAcc()
    for number, line in enumerate(lines, start=1):
        try:
            obj = _obj(json.loads(line))
        except json.JSONDecodeError:
            obj = None
        problem = _take_claude_line(obj, acc) if obj is not None else "not a json object"
        if problem:
            return f"line {number}: {problem}"
    result, session_id = acc.result, acc.session_id
    if result is None:
        return "stream has no result line"
    if not isinstance(session_id, str) or not CLAUDE_SESSION_ID_RE.fullmatch(session_id):
        return f"bad session_id {session_id!r}"
    subtype, final = result.get("subtype"), result.get("result")
    capped = subtype == CLAUDE_CAPPED_SUBTYPE
    if not capped and (subtype != "success" or result.get("is_error") is True):
        return f"result subtype {subtype!r}"
    if not capped and not isinstance(final, str):
        return "result line has no text"
    denials = result.get("permission_denials")
    if isinstance(denials, list) and len(cast(list[object], denials)) > CLAUDE_DENIALS_MAX:
        return f"over {CLAUDE_DENIALS_MAX} permission denials"  # cutting would mark some approved
    cost = _num(result.get("total_cost_usd"))
    events = list(_mark_denied(acc.events, _denied_ids(result)))
    info = {"cost_usd": cost, "duration_ms": result.get("duration_ms")}
    events.append(
        Event(0, "usage", "result", json.dumps(info, sort_keys=True), None, None, None, ())
    )
    text = final if isinstance(final, str) else ""
    events.append(Event(0, "assistant_text", None, None, None, text, None, ()))
    events.append(Event(0, "run_end", None, None, str(subtype), None, None, ()))
    assert events[-1].kind == "run_end", "a parsed stream ends with run_end"
    return Turn(tuple(events), text, session_id, cost, None, capped)


# ---------------------------------------------------------------- session-log join


def read_session_log(home_dir: Path, session_id: str, bytes_max: int) -> dict[str, LogCall] | str:
    """tool_call entries of <home>/sessions/*/<session_id>/messages.jsonl by toolCallId."""
    assert SESSION_ID_RE.fullmatch(session_id), "session id is validated before it hits a path"
    matches = sorted(home_dir.glob(f"sessions/*/{session_id}/messages.jsonl"))
    if len(matches) != 1:
        return f"expected one session log for {session_id}, found {len(matches)}"
    if matches[0].stat().st_size > bytes_max:
        return f"session log over {bytes_max} bytes"
    calls: dict[str, LogCall] = {}
    for line in matches[0].read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = _obj(json.loads(line))
        except json.JSONDecodeError:
            continue  # a torn last line must not hide the earlier calls
        payload = _obj(entry.get("payload")) if entry else None
        if payload is None or payload.get("type") != "tool_call":
            continue
        call_id, name, status = (
            payload.get("toolCallId"),
            payload.get("toolName"),
            payload.get("status"),
        )
        if isinstance(call_id, str) and isinstance(name, str) and isinstance(status, str):
            calls[call_id] = LogCall(name, status)
    assert all(call.tool_name for call in calls.values()), "every logged call has a name"
    return calls


def apply_log(events: tuple[Event, ...], calls: Mapping[str, LogCall]) -> tuple[Event, ...] | str:
    """Names and denial status from the session log; skill events derived after (3.2)."""
    named: dict[str, tuple[str, str | None]] = {}
    out: list[Event] = []
    for event in events:
        if event.kind != "tool_call":
            out.append(event)
            continue
        assert event.call_id is not None, "tool_call events carry a call id"
        logged = calls.get(event.call_id)
        stream_wins = event.name in SKILL_LOAD_TOOLS  # ACP toolId beats the log's alias
        name = event.name if stream_wins or not logged else logged.tool_name
        if name is None:
            return f"tool call {event.call_id} is not in the session log"
        status = logged.status if logged else None
        named[event.call_id] = (name, status)
        out.append(replace(event, name=name, status=status))
    joined: list[Event] = []
    for event in out:
        if event.kind == "tool_result" and event.call_id in named:
            name, status = named[event.call_id]
            shown = "denied" if status == "denied" else event.status
            event = replace(event, name=name, status=shown)
        joined.append(event)
    assert len(joined) == len(events), "the join changes events, never their count"
    return tuple(joined)


def _loaded_skill_name(args: dict[str, object]) -> str | None:
    """Args of a skill load: Kiro {name: "x"} or {id: "skill::x"}; Claude {skill: "[ns:]x"}."""
    assert isinstance(args, dict), "rawInput is an object"
    name, ident, skill = args.get("name"), args.get("id"), args.get("skill")
    if isinstance(skill, str) and skill.rsplit(":", 1)[-1]:
        return skill.rsplit(":", 1)[-1]  # Claude's Skill tool; a plugin skill is "plugin:name"
    if isinstance(name, str) and name:
        return name
    if (
        isinstance(ident, str)
        and ident.startswith(SKILL_ID_PREFIX)
        and len(ident) > len(SKILL_ID_PREFIX)
    ):
        return ident[len(SKILL_ID_PREFIX) :]
    assert SKILL_ID_PREFIX.endswith("::"), "the prefix is a namespace"
    return None


def derive_skill_events(events: tuple[Event, ...]) -> tuple[Event, ...]:
    """skill_read after each disclose_context call; skill_ref_read after a reference read."""
    assert all(event.kind != "skill_read" for event in events), "derive only once"
    out: list[Event] = []
    for event in events:
        out.append(event)
        if event.kind != "tool_call":
            continue
        if event.name in SKILL_LOAD_TOOLS and event.args_json:
            skill = _loaded_skill_name(cast(dict[str, object], json.loads(event.args_json)))
            if skill is not None:
                out.append(
                    Event(0, "skill_read", skill, None, event.status, None, event.call_id, ())
                )
        if event.name == "read_file":
            for path in event.paths:
                match = SKILL_REF_PATH_RE.search(path)
                if match:
                    out.append(
                        Event(
                            0,
                            "skill_ref_read",
                            match.group(1),
                            None,
                            event.status,
                            None,
                            event.call_id,
                            (),
                        )
                    )
    assert len(out) >= len(events), "deriving only adds events"
    return tuple(out)


def reindex(events: list[Event]) -> tuple[Event, ...]:
    result = tuple(replace(event, index=position) for position, event in enumerate(events))
    assert [e.index for e in result] == list(range(len(events))), "indexes are 0..n-1"
    assert len(result) == len(events), "reindex keeps every event"
    return result


# ---------------------------------------------------------------- contamination


def _is_inside(real_dir: str, real_path: str) -> bool:
    assert os.path.isabs(real_dir) and os.path.isabs(real_path), "both sides are absolute"
    return real_path == real_dir or real_path.startswith(real_dir + os.sep)


def outside_paths(events: tuple[Event, ...], cwd: str, home_dir: str) -> list[str]:
    """Paths of tool calls outside both cwd and home_dir; realpath on both sides (3.3).

    Shell command text is never checked: execute_bash has no path field.
    """
    real_cwd, real_home = os.path.realpath(cwd), os.path.realpath(home_dir)
    assert cwd and home_dir, "both directories are given"
    found: list[str] = []
    for event in events:
        if event.kind != "tool_call" or event.status == "denied":
            continue  # a denied call outside is the isolation working, not a leak
        for path in event.paths:
            real = os.path.realpath(path if os.path.isabs(path) else os.path.join(real_cwd, path))
            if not (_is_inside(real_cwd, real) or _is_inside(real_home, real)):
                found.append(path)
    assert all(path in {p for e in events for p in e.paths} for path in found), "from events"
    return found


def foreign_skills(events: tuple[Event, ...], home_dir: Path) -> list[str]:
    """Skills loaded that the arm's home does not hold: isolation failed. A load counts only when
    its tool_result completed; a failed or unanswered call ("Unknown skill") loaded nothing."""
    shipped = {p.name for p in (home_dir / "skills").glob("*") if p.is_dir()}
    done = {e.call_id for e in events if e.kind == "tool_result" and e.status == "completed"}
    loaded = [e.name for e in events if e.kind == "skill_read" and e.name and e.call_id in done]
    found = sorted({name for name in loaded if name not in shipped})
    assert all(name not in shipped for name in found), "only names the home lacks"
    return found


def mark_contaminated(events: tuple[Event, ...], cwd: str, home_dir: str) -> bool:
    flagged = bool(outside_paths(events, cwd, home_dir))
    assert isinstance(flagged, bool), "a yes/no answer"
    return flagged


def contamination_note(
    events: tuple[Event, ...], cwd: str, home_dir: str, foreign: list[str]
) -> str:
    """What made a run contaminated, kept in its note because its streams get deleted. Counts,
    not paths: a path outside could itself be private."""
    outside = len(outside_paths(events, cwd, home_dir))
    denied = sum(1 for e in events if e.kind == "tool_call" and e.status == "denied" and e.paths)
    note = f"contaminated: foreign_skills={','.join(foreign) or '-'} outside_paths={outside}"
    note += f" denied_path_calls={denied}"
    assert note.startswith("contaminated: "), "the note names its kind"
    return note


# ---------------------------------------------------------------- invocation


def _text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else ""


def invoke_kiro(
    argv: list[str], env: Mapping[str, str], cwd: Path, timeout_s: float
) -> InvokeResult:
    """Run argv with a timeout; no shell, so prompts and paths are never interpreted."""
    assert argv and timeout_s > 0, "argv is non-empty and the timeout positive"
    started_s = time.monotonic()
    try:
        done = subprocess.run(
            argv, cwd=cwd, env=dict(env), capture_output=True, timeout=timeout_s, check=False
        )
    except subprocess.TimeoutExpired as err:
        wall_ms = int((time.monotonic() - started_s) * MS_PER_S)
        return InvokeResult(_text(err.stdout), _text(err.stderr), None, True, wall_ms)
    wall_ms = int((time.monotonic() - started_s) * MS_PER_S)
    assert wall_ms >= 0, "wall time is not negative"
    return InvokeResult(_text(done.stdout), _text(done.stderr), done.returncode, False, wall_ms)


def cli_version(invoke: Invoker = invoke_kiro, engine: str = KIRO_ENGINE) -> str | None:
    """'3.2.1' from `kiro-cli --version` (or `claude --version`), None when unreadable."""
    assert engine in ENGINES, "engine is validated by the parser"
    binary = "claude" if engine == CLAUDE_ENGINE else "kiro-cli"
    result = invoke([binary, "--version"], os.environ, Path.cwd(), VERSION_TIMEOUT_S)
    match = VERSION_RE.search(result.stdout_text)
    assert result.wall_ms >= 0, "wall time is not negative"
    return match.group(0) if match and not result.timed_out else None


def build_argv(
    model_id: str, trust_tools: tuple[str, ...], prompt: str, resume_id: str | None
) -> list[str]:
    """The headless v3 command for one turn (probe_results.md, v3 base command)."""
    assert model_id and prompt.strip(), "model and prompt are non-empty"
    argv = ["kiro-cli", "chat", "--no-interactive", "--output-format", "stream-json", "--v3"]
    argv += ["--model", model_id, "--trust-tools=" + ",".join(trust_tools)]
    if resume_id is not None:
        assert SESSION_ID_RE.fullmatch(resume_id), "resume id is a v3 session id"
        argv += ["--resume-id", resume_id]
    if prompt.startswith("-"):
        argv.append("--")  # a prompt must not be read as a flag
    argv.append(prompt)
    assert argv[-1] == prompt, "the prompt is the last argument"
    return argv


def claude_rules_text(home_dir: Path) -> str:
    """The arm's steering files, appended to Claude's system prompt: with no user settings
    loaded there is no CLAUDE.md to import them, so they arrive here instead."""
    steering = sorted((home_dir / "steering").glob("*.md"))
    parts = [f"Contents of {path.name}:\n\n{path.read_text(encoding='utf-8')}" for path in steering]
    assert len(parts) == len(steering), "one part per steering file"
    return "\n\n".join(parts)


def build_claude_argv(
    spec: RunSpec,
    prompt: str,
    resume_id: str | None,
    budget_usd: float,
    home_dir: Path,
    *,
    cwd: Path,
    settings: str,
) -> list[str]:
    """One headless claude -p turn. Skills load from the home as a plugin. Rules load from the
    home's CLAUDE.md as memory when it has one, else as system prompt text. File tools are
    path-scoped and settings sandbox Bash (lib/isolation.py). --allowedTools is variadic, so
    "--" ends it before the prompt."""
    assert spec.model_id and prompt.strip(), "model and prompt are non-empty"
    assert budget_usd > 0, "every turn carries a positive dollar cap"
    assert settings.strip(), "every turn carries isolation settings"
    argv = ["claude", "-p", "--output-format", "stream-json", "--verbose"]
    argv += ["--model", spec.model_id, "--max-budget-usd", f"{budget_usd:.4f}", *CLAUDE_ISOLATION]
    argv += ["--settings", settings]
    if (home_dir / "skills").is_dir():
        argv += ["--plugin-dir", str(home_dir)]
    if (home_dir / "CLAUDE.md").is_file():
        argv += ["--add-dir", str(home_dir)]  # read as memory, with CLAUDE_MD_ENV in the env
    else:
        rules = claude_rules_text(home_dir)
        if rules:
            argv += ["--append-system-prompt", rules]
    if resume_id is not None:
        assert CLAUDE_SESSION_ID_RE.fullmatch(resume_id), "resume id is a claude session id"
        argv += ["--resume", resume_id]
    if spec.trust_tools:
        argv += ["--allowedTools", ",".join(scoped_allowed_tools(spec.trust_tools, cwd, home_dir))]
    argv += ["--", prompt]
    assert argv[-1] == prompt, "the prompt is the last argument"
    return argv


# ---------------------------------------------------------------- run


@dataclass
class _Progress:
    events: list[Event] = field(default_factory=lambda: [])
    stderr_parts: list[str] = field(default_factory=lambda: [])
    final_text: str = ""
    session_id: str | None = None
    credits: float | None = 0.0
    context_pct: float | None = None
    wall_ms: int = 0
    status: RunStatus = "ok"
    note: str = ""
    turns_done: int = 0


def _absorb_turn(
    prog: _Progress, result: InvokeResult, home_dir: Path, limits: RunLimits, engine: str
) -> bool:
    """Fold one invocation into prog. True when the run must stop (status/note say why)."""
    assert prog.status == "ok", "a stopped run is never absorbed into"
    assert engine in ENGINES, "engine is pinned by the spec"
    assert limits.stream_bytes_max > 0, "stream bound is positive"

    def fail(status: RunStatus, note: str) -> bool:
        prog.status, prog.note = status, note
        return True

    if result.timed_out:
        prog.credits = None  # credits are only known at turn_completion (3.8)
        return fail("timeout", "invocation hit the wall limit")
    if len(result.stdout_text.encode()) > limits.stream_bytes_max:
        return fail("harness_error", "stream over STREAM_BYTES_MAX")
    turn = _claude_turn(result) if engine == CLAUDE_ENGINE else _kiro_turn(result, home_dir, limits)
    if isinstance(turn, str):
        return fail("harness_error", turn)
    if prog.session_id not in (None, turn.session_id):
        return fail("harness_error", "resume changed the session id")
    prog.events += turn.events
    prog.session_id, prog.final_text = turn.session_id, turn.final_text
    prog.credits = (
        None if prog.credits is None or turn.credits is None else prog.credits + turn.credits
    )
    prog.context_pct = turn.context_pct
    prog.turns_done += 1
    assert prog.session_id is not None, "a parsed turn has a session id"
    if turn.capped:
        return fail("budget_abort", "claude stopped at --max-budget-usd")
    return False


def _kiro_turn(result: InvokeResult, home_dir: Path, limits: RunLimits) -> Turn | str:
    """Parse, then take names and denials from the session log, then derive skill events."""
    if result.exit_code != 0:
        return f"exit code {result.exit_code}"
    turn = parse_stream(result.stdout_text)
    if isinstance(turn, str):
        return turn
    calls = read_session_log(home_dir, turn.session_id, limits.stream_bytes_max)
    joined = apply_log(turn.events, calls) if not isinstance(calls, str) else calls
    if isinstance(joined, str):
        return joined
    return replace(turn, events=derive_skill_events(joined))


def _claude_turn(result: InvokeResult) -> Turn | str:
    """The stream names tools and denials itself. A capped run exits non-zero but parses."""
    turn = parse_claude_stream(result.stdout_text)
    if isinstance(turn, str):
        return turn if result.exit_code == 0 else f"exit code {result.exit_code}: {turn}"
    if result.exit_code != 0 and not turn.capped:
        return f"exit code {result.exit_code}"
    assert turn.capped or result.exit_code == 0, "a kept turn ran clean or hit its cap"
    return replace(turn, events=derive_skill_events(turn.events))


def pick_status(failure: RunStatus, events: tuple[Event, ...], contaminated: bool) -> RunStatus:
    """Precedence (5.2): harness_error, timeout, budget_abort, contaminated, denied_only, ok."""
    assert failure == "ok" or failure in FAILURE_STATUSES, "failure is ok or a failure status"
    if failure != "ok":
        return failure  # one failure status is set per run, the loop stops at the first
    if contaminated:
        return "contaminated"
    calls = [e for e in events if e.kind == "tool_call"]
    denied = [e for e in events if e.kind == "tool_result" and e.status == "denied"]
    assert len(denied) <= len(events), "denials are a subset of events"
    if calls and len(denied) == len(calls):
        return "denied_only"
    return "ok"


def _over_budget(prog: _Progress, spec: RunSpec) -> bool:
    over_credits = prog.credits is not None and prog.credits > spec.credits_max
    return over_credits or prog.wall_ms > spec.wall_ms_max


def prompts_for(case: Case, arm: Arm) -> tuple[str, ...]:
    """The prompts one run sends: the arm's own prompt (6.3), else the case's turns."""
    if case.arm_prompts:
        mine = tuple(text for name, text in case.arm_prompts if name == arm_text(arm))
        assert len(mine) <= 1, "arm_prompts has one prompt per arm"
        return mine
    return case.user_turns or ((case.prompt,) if case.prompt else ())


def run_case(
    spec: RunSpec,
    case: Case,
    home: HomeDir,
    cwd: Path,
    limits: RunLimits,
    runs_dir: Path,
    invoke: Invoker = invoke_kiro,
    cache_dir: Path | None = None,
) -> tuple[RunRecord, RunView]:
    """All turns of one run. Returns the ungraded record and the view graders read. A Claude
    run needs cache_dir: the one folder its sandboxed Bash may write outside cwd."""
    prompts = prompts_for(case, spec.arm)
    assert prompts and len(prompts) <= limits.user_turns_max, "1..MAX_USER_TURNS prompts"
    assert spec.engine in ENGINES and spec.wall_ms_max > 0, "engine pinned, wall limit set"
    home_dir, real_cwd = Path(home.dir_text).resolve(), cwd.resolve()
    claude = spec.engine == CLAUDE_ENGINE
    assert not claude or cache_dir is not None, "a Claude run has a cache dir"
    settings = sandbox_settings(cache_dir) if cache_dir is not None else ""
    if claude and cache_dir is not None:
        env = {**isolated_environ(os.environ, cache_dir), CLAUDE_MD_ENV: "1"}
    else:
        env = {**os.environ, "KIRO_HOME": str(home_dir)}
    runs_dir.mkdir(parents=True, exist_ok=True)
    prog = _Progress()
    for turn_index, prompt in enumerate(prompts):
        remaining_ms = max(spec.wall_ms_max - prog.wall_ms, 1)
        if claude:
            left_usd = max(spec.credits_max - (prog.credits or 0.0), CLAUDE_BUDGET_USD_MIN)
            argv = build_claude_argv(
                spec, prompt, prog.session_id, left_usd, home_dir, cwd=real_cwd, settings=settings
            )
        else:
            argv = build_argv(spec.model_id, spec.trust_tools, prompt, prog.session_id)
        result = invoke(argv, env, real_cwd, remaining_ms / MS_PER_S)
        prog.wall_ms += result.wall_ms
        prog.stderr_parts.append(result.stderr_text)
        (runs_dir / f"{spec.run_id}.t{turn_index}.jsonl").write_text(
            result.stdout_text, encoding="utf-8"
        )
        if _absorb_turn(prog, result, home_dir, limits, spec.engine):
            break
        if _over_budget(prog, spec):
            prog.status, prog.note = "budget_abort", "over RUN_CREDITS_MAX or RUN_WALL_MS_MAX"
            break
    return _finish_run(spec, prog, str(home_dir), str(real_cwd), runs_dir)


def _finish_run(
    spec: RunSpec, prog: _Progress, home_dir: str, cwd: str, runs_dir: Path
) -> tuple[RunRecord, RunView]:
    events = reindex(prog.events)
    contaminated = mark_contaminated(events, cwd, home_dir)
    foreign = foreign_skills(events, Path(home_dir)) if spec.engine == CLAUDE_ENGINE else []
    if foreign:
        contaminated = True  # a skill the arm did not ship leaked in from the user's setup
    status = pick_status(prog.status, events, contaminated)
    if status == "contaminated":
        prog.note = contamination_note(events, cwd, home_dir, foreign)  # streams get deleted
    skills = tuple(sorted({e.name for e in events if e.kind == "skill_read" and e.name}))
    record = RunRecord(
        run_id=spec.run_id,
        case_id=spec.case_id,
        arm=spec.arm,
        repeat_index=spec.repeat_index,
        engine=spec.engine,
        model_id=spec.model_id,
        cli_version=spec.cli_version,
        home_sha=spec.home_sha,
        status=status,
        wall_ms=prog.wall_ms,
        credits=prog.credits,
        context_pct_last=prog.context_pct,
        tool_call_count=sum(1 for e in events if e.kind == "tool_call"),
        denied_count=sum(1 for e in events if e.kind == "tool_result" and e.status == "denied"),
        skills_read=skills,
        session_id=prog.session_id,
        stream_path=f"{runs_dir.name}/{spec.run_id}",  # relative to the evals root
        grades=(),
        case_passed=False,
        note=prog.note,
    )
    view = RunView(spec, status, events, prog.final_text, "".join(prog.stderr_parts), home_dir, cwd)
    assert record.denied_count <= record.tool_call_count, "denials are a subset of calls"
    assert view.status == record.status, "view and record agree on status"
    return record, view


def drop_streams(record: RunRecord, turns_max: int, root: Path) -> int:
    """Delete the kept streams of a run (contaminated runs, 3.6); returns files removed."""
    assert turns_max > 0, "turns_max is positive"
    stream = root / record.stream_path
    paths = sorted(stream.parent.glob(f"{stream.name}.t*.jsonl"))
    assert len(paths) <= turns_max, "a run never keeps more streams than it has turns"
    for path in paths:
        path.unlink()
    return len(paths)
