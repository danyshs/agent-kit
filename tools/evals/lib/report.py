"""`eval report steering/<name>`: the steering-ablation table from the ledger (design 3.7, 7.1).

Reads ledger/steering_<name>.jsonl only: no dataset, no model calls. Never says "delete": a null
result is "watch" or "non-discriminating" (7.1 outcome rule). Pairs full against minus-<file> and
against none, over the cases that pass in full (the sanity gate).
"""

from __future__ import annotations

import json
import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from lib.run import CLAUDE_DEFAULT_MODEL

FINISHED_STATUSES: Final = ("ok", "denied_only")
FULL_ARM: Final = "full"
NONE_ARM: Final = "none"
MINUS_PREFIX: Final = "minus-"
BOOTSTRAP_SEED: Final = 20261112  # fixed, so a re-run of the same ledger prints the same interval
CI_TAIL: Final = 0.05  # 90% percentile interval
REPORT_USAGE: Final = "usage: eval report steering/<name> [--model ID] [--home DIR]"
REPORT_ARGV_MAX: Final = 5
SPEND_USAGE: Final = "usage: eval spend <steering|skill|value>/<name> --model ID --max CREDITS"
SPEND_ARGV_LEN: Final = 5
EXIT_OK: Final = 0
EXIT_BAD_LEDGER: Final = 1
EXIT_USAGE: Final = 2


@dataclass(frozen=True)
class ReportLimits:
    resamples: int
    min_cases_for_ci: int
    ledger_lines_max: int


@dataclass(frozen=True)
class PairedResult:
    n_cases: int
    mean_diff: float
    ci90_low: float | None  # None when n_cases < min_cases_for_ci
    ci90_high: float | None


@dataclass(frozen=True)
class LedgerRun:
    run_id: str
    case_id: str
    arm: str
    repeat_index: int
    model_id: str
    status: str
    passed: bool
    credits: float | None


@dataclass(frozen=True)
class CaseRow:
    case_id: str
    full: str  # "passed/finished" per arm, "-" when the arm has no finished run
    minus: str
    none: str


@dataclass(frozen=True)
class FileReport:
    suite: str
    model_id: str
    file_bytes: int | None
    rows: tuple[CaseRow, ...]
    broken: tuple[str, ...]  # cases failing in full: "broken case or grader", out of the tally
    changed: tuple[str, ...]  # sanity cases whose pass rate falls when the file is removed
    none_failed: tuple[str, ...]  # sanity cases that fail in the none arm
    vs_minus: PairedResult | None
    vs_none: PairedResult | None
    credits_per_run: float | None
    excluded_runs: int  # runs not finished (timeout, harness_error, budget_abort, contaminated)
    verdict: str


# ---------------------------------------------------------------- limits


def _positive_int(raw: dict[str, object], key: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"limits.json: {key} must be a positive integer")
    assert value > 0, "accepted limits are positive"
    return value


def load_report_limits(path: Path) -> ReportLimits:
    """Report constants from limits.json; ValueError on any bad entry."""
    assert path.suffix == ".json", "limits are a json file"
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as err:
        raise ValueError(f"{path}: {err}") from err
    if not isinstance(value, dict):
        raise ValueError(f"{path}: top level must be an object")
    raw = cast(dict[str, object], value)
    limits = ReportLimits(
        resamples=_positive_int(raw, "BOOTSTRAP_RESAMPLES"),
        min_cases_for_ci=_positive_int(raw, "MIN_CASES_FOR_CI"),
        ledger_lines_max=_positive_int(raw, "LEDGER_LINES_MAX"),
    )
    assert limits.resamples > 0 and limits.min_cases_for_ci > 0, "bounds are positive"
    return limits


# ---------------------------------------------------------------- scoring


def pass_rate(passes: Sequence[bool]) -> float:
    """Fraction of runs that passed; the caller guarantees at least one run."""
    assert len(passes) > 0, "a rate needs at least one run"
    rate = sum(passes) / len(passes)
    assert 0.0 <= rate <= 1.0, "a rate is a fraction"
    return rate


def pass_pow_k(passes: Sequence[bool]) -> float:
    """1.0 when all k runs passed, else 0.0 (3.7). Equals pass_rate at k = 1."""
    assert len(passes) > 0, "pass^k needs at least one run"
    result = 1.0 if all(passes) else 0.0
    assert result <= pass_rate(passes), "pass^k never exceeds the pass rate"
    return result


def paired_diff(
    per_case_with: Sequence[float],
    per_case_without: Sequence[float],
    seed: int,
    *,
    resamples: int,
    min_cases: int,
) -> PairedResult:
    """Mean of with - without over cases, 90% percentile bootstrap resampling cases (3.7).

    The interval is None below min_cases: raw counts only, no verdict.
    """
    n_cases = len(per_case_with)
    assert n_cases == len(per_case_without), "the arms are paired by case"
    assert resamples > 0 and min_cases > 0, "bootstrap bounds are positive"
    if n_cases == 0:
        return PairedResult(0, 0.0, None, None)
    diffs = [a - b for a, b in zip(per_case_with, per_case_without, strict=True)]
    mean_diff = sum(diffs) / n_cases
    if n_cases < min_cases:
        return PairedResult(n_cases, mean_diff, None, None)
    rng = random.Random(seed)
    means = sorted(
        sum(diffs[rng.randrange(n_cases)] for _ in range(n_cases)) / n_cases
        for _ in range(resamples)
    )
    low = means[math.floor(CI_TAIL * resamples)]
    high = means[math.ceil((1 - CI_TAIL) * resamples) - 1]
    assert low <= high, "interval is ordered"
    return PairedResult(n_cases, mean_diff, low, high)


# ---------------------------------------------------------------- ledger


def _field(row: dict[str, object], key: str, kind: type) -> object | None:
    value = row.get(key)
    ok = isinstance(value, kind) and not (kind is int and isinstance(value, bool))
    return value if ok else None


def _parse_line(text: str, line_no: int) -> LedgerRun | str:
    try:
        value: object = json.loads(text)
    except json.JSONDecodeError as err:
        return f"ledger line {line_no}: {err}"
    if not isinstance(value, dict):
        return f"ledger line {line_no}: not an object"
    row = cast(dict[str, object], value)
    strings = {k: _field(row, k, str) for k in ("run_id", "case_id", "arm", "model_id", "status")}
    repeat, passed, credits = row.get("repeat_index"), row.get("case_passed"), row.get("credits")
    credits_ok = credits is None or (
        isinstance(credits, (int, float)) and not isinstance(credits, bool)
    )
    if (
        not all(strings.values())
        or isinstance(repeat, bool)
        or not isinstance(repeat, int)
        or not isinstance(passed, bool)
        or not credits_ok
    ):
        return f"ledger line {line_no}: missing or mistyped field"
    assert isinstance(passed, bool) and isinstance(repeat, int), "types checked above"
    return LedgerRun(
        run_id=cast(str, strings["run_id"]),
        case_id=cast(str, strings["case_id"]),
        arm=cast(str, strings["arm"]),
        repeat_index=repeat,
        model_id=cast(str, strings["model_id"]),
        status=cast(str, strings["status"]),
        passed=passed,
        credits=None if credits is None else float(cast(float, credits)),
    )


def read_ledger(path: Path, lines_max: int) -> list[LedgerRun] | str:
    """Every run in file order, or the first reason the ledger cannot be read (never skips)."""
    assert lines_max > 0, "line bound is positive"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as err:
        return f"cannot read {path}: {err}"
    if len(lines) > lines_max:
        return f"{path}: {len(lines)} lines, limit is {lines_max}"
    runs: list[LedgerRun] = []
    for line_no, text in enumerate(lines, start=1):  # bounded by lines_max above
        if not text.strip():
            continue
        parsed = _parse_line(text, line_no)
        if isinstance(parsed, str):
            return parsed
        runs.append(parsed)
    assert len(runs) <= len(lines), "at most one run per line"
    return runs


def latest_runs(runs: Sequence[LedgerRun], model_id: str) -> list[LedgerRun]:
    """One run per (case, arm, repeat) for this model; a later ledger line replaces an earlier."""
    assert model_id, "the report is for one model"
    latest: dict[tuple[str, str, int], LedgerRun] = {}
    for run in runs:  # bounded: read_ledger capped the list
        if run.model_id == model_id:
            latest[(run.case_id, run.arm, run.repeat_index)] = run
    assert len(latest) <= len(runs), "dedup never adds runs"
    return list(latest.values())


# ---------------------------------------------------------------- report


def _by_arm(runs: Sequence[LedgerRun], arm: str) -> dict[str, list[bool]]:
    """case id -> pass flags of the finished runs of this arm."""
    assert arm, "an arm name"
    result: dict[str, list[bool]] = {}
    for run in runs:
        if run.arm == arm and run.status in FINISHED_STATUSES:
            result.setdefault(run.case_id, []).append(run.passed)
    assert all(flags for flags in result.values()), "a case entry has at least one run"
    return result


def _cell(flags: list[bool] | None) -> str:
    return "-" if not flags else f"{sum(flags)}/{len(flags)}"


def _pair(
    rates_full: dict[str, float], rates_other: dict[str, float], limits: ReportLimits
) -> PairedResult | None:
    """Paired over the cases both arms have; None when the other arm has no runs at all."""
    shared = sorted(set(rates_full) & set(rates_other))
    if not shared:
        return None
    return paired_diff(
        [rates_full[c] for c in shared],
        [rates_other[c] for c in shared],
        BOOTSTRAP_SEED,
        resamples=limits.resamples,
        min_cases=limits.min_cases_for_ci,
    )


def decide(
    sanity_n: int,
    vs_minus: PairedResult | None,
    changed: int,
    none_failed: int,
) -> str:
    """7.1 outcome rule. Order matters: no minus data, no clean case, then the interval."""
    assert sanity_n >= 0 and changed >= 0 and none_failed >= 0, "counts are not negative"
    if sanity_n == 0:
        return "incomplete: no case passes in full"
    if vs_minus is None:
        return "incomplete: no finished minus-arm runs"
    if vs_minus.ci90_low is not None and vs_minus.ci90_low > 0:
        return "keep: removal breaks cases, interval excludes 0"
    if changed:
        return f"watch: removal changed {changed} case(s); raw counts or interval too weak"
    if none_failed:
        return "watch: null result, but the rule fails in the none arm"
    return "non-discriminating: null result and the none arm never failed (not deletable)"


def build_report(
    suite: str,
    model_id: str,
    runs: Sequence[LedgerRun],
    file_bytes: int | None,
    limits: ReportLimits,
) -> FileReport:
    assert suite and model_id, "a suite and a model"
    name = suite.partition("/")[2]
    assert name, "suite is steering/<name>"
    mine = latest_runs(runs, model_id)
    full, minus = _by_arm(mine, FULL_ARM), _by_arm(mine, MINUS_PREFIX + name)
    none = _by_arm(mine, NONE_ARM)
    sanity = {c for c, flags in full.items() if pass_pow_k(flags) == 1.0}
    broken = tuple(sorted(set(full) - sanity))
    rate_full = {c: pass_rate(full[c]) for c in sanity}
    rate_minus = {c: pass_rate(minus[c]) for c in sanity if c in minus}
    rate_none = {c: pass_rate(none[c]) for c in sanity if c in none}
    vs_minus, vs_none = _pair(rate_full, rate_minus, limits), _pair(rate_full, rate_none, limits)
    changed = tuple(sorted(c for c in rate_minus if rate_minus[c] < rate_full[c]))
    none_failed = tuple(sorted(c for c in rate_none if rate_none[c] < 1.0))
    credits = [r.credits for r in mine if r.credits is not None and r.status in FINISHED_STATUSES]
    cases = sorted({r.case_id for r in mine})
    return FileReport(
        suite=suite,
        model_id=model_id,
        file_bytes=file_bytes,
        rows=tuple(
            CaseRow(c, _cell(full.get(c)), _cell(minus.get(c)), _cell(none.get(c))) for c in cases
        ),
        broken=broken,
        changed=changed,
        none_failed=none_failed,
        vs_minus=vs_minus,
        vs_none=vs_none,
        credits_per_run=sum(credits) / len(credits) if credits else None,
        excluded_runs=sum(1 for r in mine if r.status not in FINISHED_STATUSES),
        verdict=decide(len(sanity), vs_minus, len(changed), len(none_failed)),
    )


def _pair_line(label: str, result: PairedResult | None, limits: ReportLimits) -> str:
    if result is None:
        return f"{label}: no runs"
    if result.ci90_low is None or result.ci90_high is None:
        return (
            f"{label}: n={result.n_cases} mean_diff={result.mean_diff:+.3f} "
            f"(n < {limits.min_cases_for_ci}: raw counts only, no interval)"
        )
    return (
        f"{label}: n={result.n_cases} mean_diff={result.mean_diff:+.3f} "
        f"ci90=[{result.ci90_low:+.3f}, {result.ci90_high:+.3f}] "
        "(indicative until the pilot checks coverage, 3.7)"
    )


def render_table(report: FileReport, limits: ReportLimits) -> list[str]:
    """Report lines, status line last. Raw per-case counts first, then the paired results."""
    name = report.suite.partition("/")[2]
    assert name, "suite is steering/<name>"
    size = "?" if report.file_bytes is None else str(report.file_bytes)
    credits = "?" if report.credits_per_run is None else f"{report.credits_per_run:.4f}"
    lines = [
        f"{report.suite} model={report.model_id} file_bytes={size} credits_per_run={credits}",
        f"{'case':<42} {'full':>5} {'minus-' + name:>8} {'none':>5}",
    ]
    lines += [f"{r.case_id:<42} {r.full:>5} {r.minus:>8} {r.none:>5}" for r in report.rows]
    lines += [
        _pair_line(f"full vs minus-{name}", report.vs_minus, limits),
        _pair_line("full vs none", report.vs_none, limits),
        f"broken in full (out of the tally): {', '.join(report.broken) or 'none'}",
        f"changed by removal: {', '.join(report.changed) or 'none'}",
        f"failing in none: {', '.join(report.none_failed) or 'none'}",
        f"unfinished runs excluded: {report.excluded_runs}",
        f"verdict: {report.verdict}",
        f"report: suite={report.suite} model={report.model_id} cases={len(report.rows)} "
        f"verdict={report.verdict.partition(':')[0]}",
    ]
    assert lines[-1].startswith("report: "), "the status line is last"
    return lines


# ---------------------------------------------------------------- cli glue


def parse_report_args(argv: list[str], default_home: Path) -> tuple[str, str, Path] | str:
    """(suite, model id, source home), or the usage error. --kiro-home is --home's old name."""
    assert default_home.is_absolute(), "default home is absolute"
    if not argv or len(argv) > REPORT_ARGV_MAX or not argv[0].startswith("steering/"):
        return REPORT_USAGE
    options = {"--model": CLAUDE_DEFAULT_MODEL, "--home": str(default_home)}
    rest = argv[1:]
    flags = ["--home" if flag == "--kiro-home" else flag for flag in rest[::2]]
    if len(rest) % 2 or any(flag not in options for flag in flags):
        return REPORT_USAGE
    for flag, value in zip(flags, rest[1::2], strict=True):
        options[flag] = value
    if not argv[0].partition("/")[2]:
        return REPORT_USAGE
    return argv[0], options["--model"], Path(options["--home"]).expanduser()


def run_report(argv: list[str], root: Path, default_home: Path) -> tuple[list[str], int]:
    """(output lines, exit code) for `eval report`; a usage error is one line, exit 2."""
    assert root.is_dir(), "root is the dataset directory"
    parsed = parse_report_args(argv, default_home)
    if isinstance(parsed, str):
        return [parsed], EXIT_USAGE
    suite, model_id, kiro_home = parsed
    limits = load_report_limits(root / "limits.json")
    name = suite.partition("/")[2]
    runs = read_ledger(root / "ledger" / f"steering_{name}.jsonl", limits.ledger_lines_max)
    if isinstance(runs, str):
        return [runs, f"report: suite={suite} no ledger"], EXIT_BAD_LEDGER
    steering_file = kiro_home / "steering" / f"{name}.md"
    size = steering_file.stat().st_size if steering_file.is_file() else None
    lines = render_table(build_report(suite, model_id, runs, size, limits), limits)
    assert lines[-1].startswith("report: "), "the status line is last"
    return lines, EXIT_OK


# ---------------------------------------------------------------- spend guard


def total_credits(path: Path, model_id: str, lines_max: int) -> float | str:
    """Credits of every ledger line for this model (a rerun still cost money, so no dedup), or
    the reason the ledger cannot be read."""
    assert model_id, "a model"
    runs = read_ledger(path, lines_max)
    if isinstance(runs, str):
        return runs
    total = sum(r.credits or 0.0 for r in runs if r.model_id == model_id)
    assert total >= 0.0, "credits are not negative"
    return total


def _parse_spend_args(argv: list[str]) -> tuple[str, str, float] | str:
    """(suite, model id, max credits), or why the guard cannot run. Fails closed."""
    if len(argv) != SPEND_ARGV_LEN or "/" not in argv[0]:
        return SPEND_USAGE
    kind, _, name = argv[0].partition("/")
    options = dict(zip(argv[1::2], argv[2::2], strict=True))
    if (
        kind not in ("steering", "skill", "value")
        or not name
        or set(options) != {"--model", "--max"}
    ):
        return SPEND_USAGE
    if not options["--model"]:
        return SPEND_USAGE
    try:
        ceiling = float(options["--max"])
    except ValueError:
        return f"--max {options['--max']!r} is not a number"
    if not math.isfinite(ceiling) or ceiling <= 0.0:
        return f"--max {options['--max']!r} must be a positive number"
    assert ceiling > 0.0, "accepted ceilings are positive"
    assert set(options) == {"--model", "--max"}, "both flags were checked above"
    return argv[0], options["--model"], ceiling


def run_spend(argv: list[str], root: Path) -> tuple[list[str], int]:
    """`eval spend <kind>/<name> --model ID --max N`: exit 1 when the ledger total is over N or
    cannot be read; exit 2 on usage. A guard that cannot measure says stop."""
    assert root.is_dir(), "root is the dataset directory"
    parsed = _parse_spend_args(argv)
    if isinstance(parsed, str):
        code = EXIT_USAGE if parsed == SPEND_USAGE else EXIT_BAD_LEDGER
        return [parsed if code == EXIT_USAGE else f"spend: error {parsed}"], code
    suite, model_id, ceiling = parsed
    lines_max = load_report_limits(root / "limits.json").ledger_lines_max
    total = total_credits(
        root / "ledger" / (suite.replace("/", "_") + ".jsonl"), model_id, lines_max
    )
    if isinstance(total, str):
        return [f"spend: error {total}"], EXIT_BAD_LEDGER
    over = total > ceiling
    line = (
        f"spend: suite={suite} model={model_id} total={total:.4f} max={ceiling:.4f} "
        f"over={'true' if over else 'false'}"
    )
    assert line.startswith("spend: "), "the status line"
    return [line], EXIT_BAD_LEDGER if over else EXIT_OK
