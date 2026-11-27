"""`eval run <suite>`: plan, check budget, run each spec, grade, append the ledger (5.1, 3.8).

--probe-run (decision, step 2): the 0.2-credit PROBE_RUN_CREDITS_MAX is a TOTAL across every run
of the command. Each run gets the remainder as its credits_max; the loop starts no run once the
total is spent, so the overshoot is at most one run's. Smoke tier only.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from lib.dataset import (
    SUITE_DIRS,
    SUITE_KINDS,
    Case,
    Rejection,
    Target,
    World,
    attach_rule_skill,
    discover_world,
    load_cases,
    load_limits,
    load_rules,
)
from lib.grade import append_run, graded
from lib.home import HomeDir, build_home
from lib.isolation_probe import PROBE_ATTEMPTS_MAX, probe_isolation
from lib.run import (
    CLAUDE_DEFAULT_MODEL,
    CLAUDE_ENGINE,
    CLAUDE_TRUST_NAMES,
    COST_UNITS,
    DEFAULT_MODEL,
    FAILURE_STATUSES,
    KIRO_ENGINE,
    TIERS,
    Ceiling,
    Invoker,
    RunLimits,
    RunRecord,
    RunSpec,
    Tier,
    check_budget,
    cli_version,
    drop_streams,
    invoke_kiro,
    load_run_limits,
    plan_runs,
    run_case,
    worst_case,
)

RUN_USAGE: Final = (
    "usage: eval run <steering|skill|value>/<name> [--engine claude|kiro] [--tier smoke|standard] "
    "[--model ID] [--arm ARM] [--case ID] [--repeats N] [--probe-run] [--home DIR] "
    "[--rules system-prompt|claude-md]"
)
SCRATCH_PARENT: Final = "/tmp"  # throwaway homes and cwds live here, never under the repo
ENGINE_FLAGS: Final = {"claude": CLAUDE_ENGINE, "kiro": KIRO_ENGINE}
DEFAULT_HOMES: Final = {
    CLAUDE_ENGINE: Path("~/.claude").expanduser(),
    KIRO_ENGINE: Path("~/.kiro").expanduser(),
}
RULES_FLAGS: Final = ("system-prompt", "claude-md")
DEFAULT_MODELS: Final = {CLAUDE_ENGINE: CLAUDE_DEFAULT_MODEL, KIRO_ENGINE: DEFAULT_MODEL}
ARGV_MAX: Final = 16
REPEATS_MAX: Final = 5  # = MAX_RUNS_PER_CASE
EXIT_OK: Final = 0
EXIT_BAD_RUN: Final = 1
EXIT_USAGE: Final = 2


@dataclass(frozen=True)
class RunArgs:
    suite_kind: str
    suite_name: str
    tier: Tier
    model_id: str
    arm: str | None
    probe: bool
    kiro_home: Path  # the source home the arm copies from: ~/.claude or ~/.kiro
    case_id: str | None = None
    repeats: int | None = None
    engine: str = KIRO_ENGINE
    rules_claude_md: bool = False  # Claude: rules via a CLAUDE.md in the home, not system prompt


def parse_run_args(argv: list[str], default_home: Path | None = None) -> RunArgs | str:
    """argv after `run`; a str is the usage error. The engine picks the default home and
    model; default_home overrides the engine's (tests). --kiro-home is --home's old name."""
    assert default_home is None or default_home.is_absolute(), "default home is absolute"
    if not argv or len(argv) > ARGV_MAX or "/" not in argv[0]:
        return RUN_USAGE
    kind, _, name = argv[0].partition("/")
    options: dict[str, str] = {"--tier": "smoke", "--engine": "claude", "--rules": "claude-md"}
    arm: str | None = None
    case_id: str | None = None
    repeats: int | None = None
    probe = False
    rest, index = argv[1:], 0
    while index < len(rest):  # bounded by ARGV_MAX
        flag = rest[index]
        if flag == "--probe-run":
            probe, index = True, index + 1
        elif flag in (
            "--tier",
            "--model",
            "--engine",
            "--rules",
            "--home",
            "--kiro-home",
            "--arm",
            "--case",
            "--repeats",
        ) and index + 1 < len(rest):
            if flag == "--arm":
                arm = rest[index + 1]
            elif flag == "--case":
                case_id = rest[index + 1]
            elif flag == "--repeats":
                value = rest[index + 1]
                if not (value.isascii() and value.isdigit()) or not 1 <= int(value) <= REPEATS_MAX:
                    return RUN_USAGE
                repeats = int(value)
            else:
                options["--home" if flag == "--kiro-home" else flag] = rest[index + 1]
            index += 2
        else:
            return RUN_USAGE
    if kind not in SUITE_KINDS or not name or options["--tier"] not in TIERS:
        return RUN_USAGE
    engine = ENGINE_FLAGS.get(options["--engine"])
    if engine is None or options["--rules"] not in RULES_FLAGS:
        return RUN_USAGE
    assert index == len(rest), "every argument was consumed"
    home = options.get("--home")
    return RunArgs(
        kind,
        name,
        options["--tier"],
        options.get("--model") or DEFAULT_MODELS[engine],
        arm,
        probe,
        Path(home).expanduser() if home else (default_home or DEFAULT_HOMES[engine]),
        case_id,
        repeats,
        engine,
        options["--rules"] == "claude-md",
    )


def load_suite(root: Path, args: RunArgs, world: World) -> tuple[list[Case], list[Rejection]]:
    """The suite's cases (whole file or nothing) and every rejection."""
    assert args.suite_kind in SUITE_KINDS, "suite kind is validated by the parser"
    assert args.suite_name and "/" not in args.suite_name, "suite name is one path segment"
    limits = load_limits(root / "limits.json")
    suite_dir = root / SUITE_DIRS[args.suite_kind] / args.suite_name
    cases_path = suite_dir / "cases.json"
    if not cases_path.is_file():
        return [], [Rejection(str(cases_path), None, "suite has no cases.json")]
    rule_ids: frozenset[str] = frozenset()
    rejections: list[Rejection] = []
    if args.suite_kind == "steering":
        rules, rejections = load_rules(suite_dir / "rules.json", world, limits)
        rule_ids = frozenset(rule.id for rule in rules)
    cases, case_rejections = load_cases(cases_path, args.suite_kind, rule_ids, world, limits)
    if args.suite_kind == "steering":
        cases, sidecar = attach_rule_skill(suite_dir, cases, limits)
        case_rejections = [*case_rejections, *([] if sidecar is None else [sidecar])]
    assert not (cases and (rejections or case_rejections)), "a rejected file yields no cases"
    return list(cases), rejections + list(case_rejections)


def _error_record(spec: RunSpec, runs_dir: Path, note: str) -> RunRecord:
    assert note, "an error record says why"
    assert spec.case_id, "the spec names its case"
    return RunRecord(
        run_id=spec.run_id,
        case_id=spec.case_id,
        arm=spec.arm,
        repeat_index=spec.repeat_index,
        engine=spec.engine,
        model_id=spec.model_id,
        cli_version=spec.cli_version,
        home_sha=spec.home_sha or "unbuilt",
        case_sha=spec.case_sha,
        status="harness_error",
        wall_ms=0,
        credits=None,
        context_pct_last=None,
        tool_call_count=0,
        denied_count=0,
        skills_read=(),
        session_id=None,
        stream_path=f"{runs_dir.name}/{spec.run_id}",
        grades=(),
        case_passed=False,
        note=note,
    )


def execute_spec(
    spec: RunSpec,
    case: Case,
    world: World,
    args: RunArgs,
    limits: RunLimits,
    root: Path,
    invoke: Invoker,
) -> RunRecord:
    """Build the home and cwd under /tmp, run, grade, delete both. Never raises on a bad arm."""
    assert spec.case_id == case.id, "the spec belongs to this case"
    assert spec.engine == args.engine and limits.home_files_max > 0, "engine pinned, bound set"
    runs_dir = root / "runs"
    home_path = Path(tempfile.mkdtemp(prefix="evhome-", dir=SCRATCH_PARENT))
    cwd_path = Path(tempfile.mkdtemp(prefix="evcwd-", dir=SCRATCH_PARENT))
    cache_path = Path(tempfile.mkdtemp(prefix="evcache-", dir=SCRATCH_PARENT))  # sandbox writes
    try:
        built = build_home(
            spec.arm,
            Target(case.target, case.target_kind),
            None,
            case.extra_steering,
            case.extra_skills,
            src_home=args.kiro_home,
            dest=home_path,
            always_on=world.always_on_names,
            files_max=limits.home_files_max,
            plugin=spec.engine == CLAUDE_ENGINE,
            claude_md=spec.engine == CLAUDE_ENGINE and args.rules_claude_md,
            rule_skill_description=case.rule_skill_description,
        )
        if isinstance(built, str):
            return _error_record(spec, runs_dir, built)
        assert isinstance(built, HomeDir), "a built home"
        if case.fixture:
            shutil.copytree(root / case.fixture, cwd_path, dirs_exist_ok=True)
        pinned = replace(spec, home_sha=built.home_sha)
        record, view = run_case(
            pinned, case, built, cwd_path, limits, runs_dir, invoke, cache_dir=cache_path
        )
        return graded(record, view, case.assertions, root)
    finally:
        shutil.rmtree(home_path, ignore_errors=True)
        shutil.rmtree(cwd_path, ignore_errors=True)
        shutil.rmtree(cache_path, ignore_errors=True)


def _run_line(record: RunRecord) -> str:
    credits = "null" if record.credits is None else f"{record.credits:.4f}"
    unit = COST_UNITS.get(record.engine, "credits")
    return (
        f"run {record.run_id} status={record.status} {unit}={credits} wall_ms={record.wall_ms} "
        f"tools={record.tool_call_count} denied={record.denied_count} passed={record.case_passed}"
        + (f" note={record.note}" if record.note else "")
    )


def run_specs(
    specs: list[RunSpec],
    cases: dict[str, Case],
    world: World,
    args: RunArgs,
    limits: RunLimits,
    root: Path,
    ledger_path: Path,
    invoke: Invoker,
) -> tuple[list[str], int]:
    """(output lines, exit code). Starts no run once the probe total is spent or three
    consecutive runs are harness errors."""
    lines: list[str] = []
    spent_credits, harness_streak, executed, passed, aborted = 0.0, 0, 0, 0, 0
    for spec in specs:
        remaining = limits.probe_credits_max - spent_credits
        if args.probe and remaining <= 0:
            lines.append("probe total spent: remaining runs not started")
            break
        if harness_streak >= limits.harness_errors_max:
            lines.append("consecutive harness errors: suite aborted")
            break
        capped = replace(spec, credits_max=min(spec.credits_max, remaining)) if args.probe else spec
        record = execute_spec(capped, cases[spec.case_id], world, args, limits, root, invoke)
        failure = append_run(ledger_path, record)
        if failure:
            lines.append(failure)
            return lines, EXIT_BAD_RUN
        if record.status == "contaminated":
            drop_streams(record, limits.user_turns_max, root)
        executed += 1
        passed += record.case_passed
        aborted += record.status in FAILURE_STATUSES
        harness_streak = harness_streak + 1 if record.status == "harness_error" else 0
        spent_credits += record.credits or 0.0
        lines.append(_run_line(record))
    failed = executed - passed - aborted
    lines.append(
        f"run: suite={args.suite_kind}/{args.suite_name} tier={args.tier} runs={executed} "
        f"passed={passed} failed={failed} aborted={aborted} "
        f"{limits.cost_unit}={spent_credits:.4f}"
    )
    assert lines[-1].startswith("run: "), "the status line is last"
    assert executed <= len(specs), "never runs more than planned"
    return lines, EXIT_BAD_RUN if aborted else EXIT_OK


def with_isolation_probe(ceiling: Ceiling, specs: list[RunSpec], engine: str) -> Ceiling:
    """A Claude suite also pays for the isolation probe's attempts, at the first run's caps."""
    if engine != CLAUDE_ENGINE or not specs:
        return ceiling
    extra = PROBE_ATTEMPTS_MAX
    out = Ceiling(
        ceiling.run_count + extra,
        ceiling.credits_max_total + extra * specs[0].credits_max,
        ceiling.wall_ms_max_total + extra * specs[0].wall_ms_max,
    )
    assert out.run_count > ceiling.run_count, "the probe adds runs"
    return out


def run_suite(
    args: RunArgs, root: Path, invoke: Invoker = invoke_kiro, stamp_s: int | None = None
) -> tuple[list[str], int]:
    """Plan, refuse over budget, then run. Rejections or a missing cli version stop it early."""
    assert args.tier in ("smoke", "standard"), "tier is validated by the parser"
    assert root.is_dir(), "root is the dataset directory"
    limits = load_limits(root / "limits.json")
    run_limits = load_run_limits(root / "limits.json", args.engine)
    world = discover_world(args.kiro_home, root, limits)
    cases, rejections = load_suite(root, args, world)
    if rejections or not cases:
        reasons = [f"rejected {r.file_text} [{r.record_id or '-'}]: {r.reason}" for r in rejections]
        return reasons + ["run: suite has no valid cases"], EXIT_BAD_RUN
    version = cli_version(invoke, args.engine)
    if version is None:
        binary = "claude" if args.engine == CLAUDE_ENGINE else "kiro-cli"
        return [f"run: cannot read the {binary} version"], EXIT_BAD_RUN
    trust = CLAUDE_TRUST_NAMES if args.engine == CLAUDE_ENGINE else world.trust_names
    specs = plan_runs(
        cases,
        args.tier,
        args.model_id,
        version,
        run_limits,
        trust_names=trust,
        stamp_s=int(time.time()) if stamp_s is None else stamp_s,
        probe=args.probe,
        arm_filter=args.arm,
        case_filter=args.case_id,
        repeats_override=args.repeats,
        engine=args.engine,
    )
    ceiling = worst_case(specs)
    assert ceiling.run_count == len(specs), "the ceiling counts every planned run"
    ceiling = with_isolation_probe(ceiling, specs, args.engine)
    assert all(spec.engine == args.engine for spec in specs), "every run is pinned to one engine"
    plan_line = (
        f"plan: engine={args.engine} runs={ceiling.run_count} "
        f"{run_limits.cost_unit}_max_total={ceiling.credits_max_total} "
        f"wall_ms_max_total={ceiling.wall_ms_max_total}"
        + (" probe_total_cap" if args.probe else "")
    )
    refusal = check_budget(ceiling, args.tier, run_limits, probe=args.probe, model_id=args.model_id)
    if refusal:
        return [plan_line, f"run: refused: {refusal}"], EXIT_BAD_RUN
    if args.engine == CLAUDE_ENGINE:
        probe_refusal = probe_isolation(specs[0], invoke)
        if probe_refusal:
            return [plan_line, f"run: refused: isolation probe: {probe_refusal}"], EXIT_BAD_RUN
    ledger_path = root / "ledger" / f"{args.suite_kind}_{args.suite_name}.jsonl"
    lines, code = run_specs(
        specs, {c.id: c for c in cases}, world, args, run_limits, root, ledger_path, invoke
    )
    return [plan_line, *lines], code
