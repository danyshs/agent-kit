"""Coverage: the reconciliation table every build and report ends with.

Stage 2 holds the coverage record, its checks and its Markdown.
Stage 3 adds nearest_rank, sum_known, render_fleet, render_session.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields
from typing import Final

from traj.limits import (
    DETECTOR_VERSION,
    RULE_VERSION,
    SCHEMA_VERSION,
    SKIP_REASONS,
    TOP_TURNS,
)
from traj.schema import Finding, SessionData, TurnRecord


@dataclass(frozen=True)
class Coverage:
    """Counts from one build. `seen` is the listed raw sessions; the rows after it sum to it."""

    seen: int
    scanned_from_raw: int
    not_built_yet: int
    skipped: tuple[tuple[str, int], ...]  # every SKIP_REASONS entry, in that order
    events_not_written: int
    # below the sum, not part of it
    events_without_raw_log: int
    filtered_out: int
    malformed_events: int
    unknown_events: int
    parse_errors: int
    unpaired_calls: int
    usage_conflicts: int
    negative_durations: int
    truncated_findings_sessions: int
    zero_step_sessions: int
    # denominators for the quality thresholds, summed over scanned sessions
    events_used: int
    events_total: int


COUNT_FIELDS = tuple(f.name for f in fields(Coverage) if f.name != "skipped")


def coverage_problem(cov: Coverage) -> str | None:
    """None when the rows sum to `seen` and every count is sane, else the first problem."""
    if tuple(reason for reason, _ in cov.skipped) != SKIP_REASONS:
        return "skipped rows are not the skip reasons in order"
    counts = [getattr(cov, name) for name in COUNT_FIELDS] + [n for _, n in cov.skipped]
    if any(type(n) is not int or n < 0 for n in counts):
        return "a coverage count is not a non-negative int"
    total = (
        cov.scanned_from_raw
        + cov.not_built_yet
        + sum(n for _, n in cov.skipped)
        + cov.events_not_written
    )
    if total != cov.seen:
        return "coverage rows do not sum to seen"
    if cov.malformed_events > cov.events_used or cov.events_used > cov.events_total:
        return "event counts do not nest"
    return None


def skipped_total(cov: Coverage) -> int:
    assert len(cov.skipped) == len(SKIP_REASONS), "one row per skip reason"
    return sum(n for _, n in cov.skipped) + cov.events_not_written


def status_line(cov: Coverage) -> str:
    """Last line of build, report and coverage output."""
    assert coverage_problem(cov) is None, "a status line is only printed for sound coverage"
    return (
        f"traj: sessions={cov.seen} built={cov.scanned_from_raw} skipped={skipped_total(cov)} "
        f"malformed={cov.malformed_events} unknown={cov.unknown_events}"
    )


def render_coverage(cov: Coverage) -> list[str]:
    """Markdown. The first table sums to `seen`; the second is outside the sum."""
    assert coverage_problem(cov) is None, "render only sound coverage"
    rows = [
        ("seen", cov.seen),
        ("scanned from raw", cov.scanned_from_raw),
        ("not built yet", cov.not_built_yet),
        *cov.skipped,
        ("events not written", cov.events_not_written),
    ]
    extra = [
        ("events files with no raw log", cov.events_without_raw_log),
        ("sessions outside the filters", cov.filtered_out),
        ("malformed events", cov.malformed_events),
        ("unknown events", cov.unknown_events),
        ("parse errors", cov.parse_errors),
        ("unpaired calls", cov.unpaired_calls),
        ("usage conflicts", cov.usage_conflicts),
        ("negative durations", cov.negative_durations),
        ("sessions with truncated findings", cov.truncated_findings_sessions),
        ("sessions scanned with zero steps", cov.zero_step_sessions),
    ]
    lines = ["## Coverage", "", "| row | sessions |", "|---|---|"]
    lines.extend(f"| {name} | {count} |" for name, count in rows)
    lines.extend(["", "Not part of the sum:", "", "| row | count |", "|---|---|"])
    lines.extend(f"| {name} | {count} |" for name, count in extra)
    return lines


# ---------------------------------------------------------------- stage 3: report and cost


def nearest_rank(values: list[float], percent: float) -> float | None:
    """Nearest-rank percentile: element at index ceil(p/100 * n) - 1 of the sorted list.
    Returns None for an empty list."""
    assert 0.0 <= percent <= 100.0, "a valid percentile"
    n = len(values)
    if n == 0:
        return None
    idx = max(0, math.ceil(percent / 100.0 * n) - 1)
    idx = min(idx, n - 1)
    return sorted(values)[idx]


def sum_known(values: list[float | None]) -> tuple[float, int]:
    """(sum of non-null values, count of null values). Null is n/a, not zero."""
    total = 0.0
    nulls = 0
    for v in values:  # bounded by the caller
        if v is None:
            nulls += 1
        else:
            total += v
    return total, nulls


# ---------------------------------------------------------------- render helpers (stage 3)

_CREDITS_COL: Final = "depth-0 credits as logged"
_TOKENS_COL: Final = "tokens per depth-0 turn, each request counted once"

# Printed limits, one item per line, as designed; stage 4 calibration updates detector entry.
_LIMITS: Final = [
    "Detector thresholds are first guesses; calibration in stage 4 may disable a detector.",
    (
        "Kiro turn = one `executionId`; agent-initiated executions count as turns"
        " (~3% overcount vs prompts)."
    ),
    (
        "Claude turn = one typed prompt; checked against an independent count"
        " (2,078 turns vs 2,077 prompt lines, the extra one an implicit first turn)."
    ),
    "Claude sub-agent turns: their own rows, not linked to the parent turn and not in its tokens.",
    "Kiro sub-execution credits: depth-0 credits as logged may include sub-agent spend.",
    "Kiro parallel dispatch: not recoverable from the log; `parallel_groups` is null.",
    (
        "Permission wait: simple first-pending to first-resolved pairing;"
        " extra consent rounds ignored."
    ),
    "Cross-harness cost: credits and tokens are different units; tables are never mixed.",
    (
        'Claude `denied`: only from `permissionDecision.decision == "reject"`;'
        " `cancelled` never seen live."
    ),
    "Shell edits: `sed -i` and git rewrites reset repeat-read counts; bare `>` does not.",
    (
        "Rule flags: regex-shaped leads; misses (e.g. `git commit -F`);"
        " false positives (quoted separators, `echo` of a trailer)."
    ),
    "Shell exit code: missing when output is offloaded (~248 of 4,787 Kiro results examined).",
    (
        "Shapes: fixed at build vocabulary version;"
        " options added later cannot apply to expired sessions."
    ),
    "Claude `model_label`: from the first `message.model`; may change mid-session.",
]


def _render_header(stamp: str, filters: dict[str, str]) -> list[str]:
    """Header section: stamp, versions, active filters."""
    lines = [
        f"# traj report  {stamp}",
        "",
        f"schema {SCHEMA_VERSION}  detector {DETECTOR_VERSION}  rule {RULE_VERSION}",
        "",
    ]
    if filters:
        parts = "  ".join(f"{k} {v}" for k, v in sorted(filters.items()))
        lines += [f"filters: {parts}", ""]
    return lines


def _render_sessions(datas: list[SessionData]) -> list[str]:
    """Sessions section: count by harness and tree."""
    counts: dict[tuple[str, str], int] = {}
    for data in datas:  # bounded by sessions_max
        key = (data.session.harness, data.session.tree)
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return ["## Sessions", "", "*(none)*", ""]
    lines = ["## Sessions", "", "| harness | tree | sessions |", "|---|---|---|"]
    for (harness, tree), n in sorted(counts.items()):
        lines.append(f"| {harness} | {tree} | {n} |")
    return lines + [""]


def _render_tools(datas: list[SessionData]) -> list[str]:
    """Tools section: per tool name, steps / with result / errors, sorted by steps desc."""
    tool_steps: dict[str, int] = {}
    tool_results: dict[str, int] = {}
    tool_errors: dict[str, int] = {}
    for data in datas:  # bounded by sessions_max
        for step in data.steps:  # bounded by steps_per_session_max
            t = step.tool
            tool_steps[t] = tool_steps.get(t, 0) + 1
            if step.status != "unpaired":
                tool_results[t] = tool_results.get(t, 0) + 1
            if step.status == "error":
                tool_errors[t] = tool_errors.get(t, 0) + 1
    if not tool_steps:
        return ["## Tools", "", "*(none)*", ""]
    rows = sorted(tool_steps.items(), key=lambda kv: (-kv[1], kv[0]))
    lines = ["## Tools", "", "| tool | steps | with result | errors |", "|---|---|---|---|"]
    for tool, steps in rows:
        lines.append(
            f"| {tool} | {steps} | {tool_results.get(tool, 0)} | {tool_errors.get(tool, 0)} |"
        )
    return lines + [""]


def _depth0_credits(datas: list[SessionData], harness: str) -> list[float | None]:
    """Depth-0 credits for one harness, in session order."""
    out: list[float | None] = []
    for data in datas:
        if data.session.harness != harness:
            continue
        for turn in data.turns:
            if turn.depth == 0:
                out.append(turn.credits)
    return out


def _turn_tokens(turn: TurnRecord) -> int | None:
    """Every token the turn's requests reported (input, output, cache read, cache write); None
    when none was reported. Null is n/a, never zero."""
    parts = (
        turn.input_tokens,
        turn.output_tokens,
        turn.cache_read_tokens,
        turn.cache_creation_tokens,
    )
    known = [p for p in parts if p is not None]
    return sum(known) if known else None


def _claude_turns(datas: list[SessionData]) -> list[tuple[str, TurnRecord]]:
    """(session id, turn) for every Claude depth-0 turn. Sub-agent turns are their own rows in
    the events file and are not folded into the parent here."""
    out: list[tuple[str, TurnRecord]] = []
    for data in datas:  # bounded by sessions_max
        if data.session.harness != "claude":
            continue
        out.extend((data.session.session_id, t) for t in data.turns if t.depth == 0)
    return out


def _fmt_int(value: float | None) -> str:
    return "n/a" if value is None else f"{int(value):,}"


def _render_claude_cost(datas: list[SessionData]) -> list[str]:
    turns = _claude_turns(datas)
    if not turns:
        return ["*(no Claude Code sessions)*", ""]
    totals = [_turn_tokens(t) for _, t in turns]
    outputs = [t.output_tokens for _, t in turns]
    known_total = [float(v) for v in totals if v is not None]
    known_out = [float(v) for v in outputs if v is not None]
    sub_turns = sum(1 for d in datas if d.session.harness == "claude" for t in d.turns if t.depth)
    return [
        f"### Claude Code ({_TOKENS_COL})",
        "",
        "| metric | all tokens | output tokens |",
        "|---|---|---|",
        f"| depth-0 turns | {len(turns)} | {len(turns)} |",
        f"| n/a (no usage) | {sum_known([None if v is None else 0.0 for v in totals])[1]} "
        f"| {sum_known([None if v is None else 0.0 for v in outputs])[1]} |",
        f"| p50 | {_fmt_int(nearest_rank(known_total, 50))} "
        f"| {_fmt_int(nearest_rank(known_out, 50))} |",
        f"| p90 | {_fmt_int(nearest_rank(known_total, 90))} "
        f"| {_fmt_int(nearest_rank(known_out, 90))} |",
        f"| max | {_fmt_int(nearest_rank(known_total, 100))} "
        f"| {_fmt_int(nearest_rank(known_out, 100))} |",
        f"| sub-agent turns (not included) | {sub_turns} | {sub_turns} |",
        "",
    ]


def _render_cost(datas: list[SessionData]) -> list[str]:
    """Cost per turn section. Claude Code: tokens. Kiro: depth-0 credits as logged. The units
    differ, so the tables are never mixed."""
    lines = ["## Cost per turn", "", *_render_claude_cost(datas)]
    kiro_credits = _depth0_credits(datas, "kiro")
    if kiro_credits:
        known = [v for v in kiro_credits if v is not None]
        _, na_count = sum_known(kiro_credits)
        p50 = nearest_rank(known, 50)
        p90 = nearest_rank(known, 90)
        mx = nearest_rank(known, 100)

        def _fmt(v: float | None) -> str:
            return "n/a" if v is None else f"{v:.4g}"

        lines += [
            f"### Kiro ({_CREDITS_COL})",
            "",
            "| metric | value |",
            "|---|---|",
            f"| depth-0 turns | {len(kiro_credits)} |",
            f"| n/a (no usage) | {na_count} |",
            f"| p50 | {_fmt(p50)} |",
            f"| p90 | {_fmt(p90)} |",
            f"| max | {_fmt(mx)} |",
            "",
        ]
    return lines


def _render_top_claude(datas: list[SessionData]) -> list[str]:
    ranked = [(v, sid, t) for sid, t in _claude_turns(datas) if (v := _turn_tokens(t)) is not None]
    ranked.sort(key=lambda r: (-r[0], r[1], r[2].turn_index))
    top = ranked[:TOP_TURNS]  # at most TOP_TURNS entries
    if not top:
        return []
    lines = [
        f"### Claude Code top {TOP_TURNS}",
        "",
        "| session | turn | steps | errors | stop | model calls | all tokens | output tokens |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for total, sid, t in top:
        lines.append(
            f"| {sid} | {t.turn_index} | {t.steps} | {t.errors} | {t.stop_reason or 'n/a'} "
            f"| {_fmt_int(t.model_calls)} | {total:,} | {_fmt_int(t.output_tokens)} |"
        )
    return lines + [""]


def _render_top_turns(datas: list[SessionData]) -> list[str]:
    """Top turns section: top TOP_TURNS depth-0 turns by tokens (Claude Code), then by credits
    (Kiro)."""
    claude = _render_top_claude(datas)
    kiro = _render_top_kiro(datas)
    if not claude and not kiro:
        return ["## Top turns", "", "*(no depth-0 turns with cost)*", ""]
    return ["## Top turns", "", *claude, *kiro]


def _render_top_kiro(datas: list[SessionData]) -> list[str]:
    rows: list[tuple[float, str, int, int, int, str | None]] = []
    for data in datas:
        if data.session.harness != "kiro":
            continue
        sid = data.session.session_id
        for turn in data.turns:
            if turn.depth == 0 and turn.credits is not None:
                rows.append(
                    (turn.credits, sid, turn.turn_index, turn.steps, turn.errors, turn.stop_reason)
                )
    rows.sort(key=lambda r: -r[0])
    top = rows[:TOP_TURNS]  # at most TOP_TURNS entries
    if not top:
        return []
    header = f"| session | turn | steps | errors | stop | {_CREDITS_COL} |"
    sep = "|---|---|---|---|---|---|"
    lines = [f"### Kiro top {TOP_TURNS}", "", header, sep]
    for credits, sid, ti, steps, errors, stop in top:
        stop_s = stop if stop else "n/a"
        lines.append(f"| {sid} | {ti} | {steps} | {errors} | {stop_s} | {credits:.4g} |")
    return lines + [""]


def _render_limits() -> list[str]:
    """Limits section: printed rows from the design's Risks and Limits table."""
    lines = ["## Limits", ""]
    for item in _LIMITS:  # bounded by the constant list
        lines.append(f"- {item}")
    return lines + [""]


def render_fleet(
    datas: list[SessionData],
    coverage: Coverage,
    stamp: str,
    filters: dict[str, str],
    findings: list[Finding] | None = None,
) -> list[str]:
    """Full fleet report Markdown. `findings` is empty until stage 4."""
    assert coverage_problem(coverage) is None, "render only sound coverage"
    from traj.quality import check_quality  # avoid circular import at module level

    problems = check_quality(coverage)
    lines: list[str] = []
    lines += _render_header(stamp, filters)
    if problems:
        for p in problems:
            lines += [f"> **DATA QUALITY: {p}**", ""]
    lines += _render_sessions(datas)
    lines += _render_tools(datas)
    lines += _render_cost(datas)
    lines += _render_top_turns(datas)
    # Findings section: stage 4 populates; show count or empty placeholder
    all_findings = findings or []
    if all_findings:
        lines += ["## Findings", ""]
        lines += [
            "| session | turn | steps | detector | count | severity |",
            "|---|---|---|---|---|---|",
        ]
        for f in all_findings:  # bounded by FINDINGS_PER_DETECTOR_MAX * sessions
            lines.append(
                f"| {f.session_id} | {f.turn_index} | {f.first_step}–{f.last_step}"
                f" | {f.detector} | {f.count} | {f.severity} |"
            )
        lines.append("")
    else:
        lines += ["## Findings", "", "*(none)*", ""]
    lines += _render_limits()
    lines += render_coverage(coverage)
    lines += ["", status_line(coverage)]
    return lines


def render_session(
    data: SessionData,
    findings: list[Finding],
    stamp: str,
) -> list[str]:
    """Per-session report: header, turns table, findings."""
    sid = data.session.session_id
    lines = [
        f"# traj report  {stamp}",
        "",
        f"schema {SCHEMA_VERSION}  detector {DETECTOR_VERSION}  rule {RULE_VERSION}",
        "",
        f"## Session {sid}",
        "",
        f"harness {data.session.harness}  tree {data.session.tree}"
        f"  created {data.session.created_day or 'unknown'}",
        "",
        "### Turns",
        "",
        f"| turn | depth | steps | errors | stop | {_CREDITS_COL} | all tokens | model calls |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for turn in data.turns:  # bounded by turns_per_session_max
        credits_s = f"{turn.credits:.4g}" if turn.credits is not None else "n/a"
        model_s = str(turn.model_calls) if turn.model_calls is not None else "n/a"
        stop_s = turn.stop_reason if turn.stop_reason else "n/a"
        lines.append(
            f"| {turn.turn_index} | {turn.depth} | {turn.steps} | {turn.errors}"
            f" | {stop_s} | {credits_s} | {_fmt_int(_turn_tokens(turn))} | {model_s} |"
        )
    lines.append("")
    if findings:
        lines += [
            "### Findings",
            "",
            "| turn | steps | detector | count | note |",
            "|---|---|---|---|---|",
        ]
        for f in findings:  # bounded by FINDINGS_PER_DETECTOR_MAX * sessions
            lines.append(
                f"| {f.turn_index} | {f.first_step}–{f.last_step}"
                f" | {f.detector} | {f.count} | {f.note} |"
            )
        lines.append("")
    else:
        lines += ["### Findings", "", "*(none)*", ""]
    return lines
