"""`eval report steering/<name>`: the steering-ablation table from the ledger (design 3.7, 7.1).

Reads ledger/steering_<name>.jsonl, and the suite's cases.json only to keep the runs of each case as
it stands now (case_sha); no model calls. Pairs full against minus-<file>, none and skill-instead
(the rule loaded as a skill instead) over every case some arm passes, both ways: a case that passes
more often without the rule counts against it. The test is an exact two-sided sign-flip test over
cases. A null result on a few cases is "watch" or "non-discriminating", since a small suite can't
prove a rule is dead. On enough cases (limits.json) it becomes advice: "demote" a long rule to a
skill, or "prune" one no case needs, one file at a time. A long rule that matters, but holds every
case as a skill, is advised to move to one.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Final, Protocol, cast

from lib.dataset import SUITE_DIRS, case_sha, load_limits
from lib.run import CLAUDE_DEFAULT_MODEL

FINISHED_STATUSES: Final = ("ok", "denied_only")
FULL_ARM: Final = "full"
NONE_ARM: Final = "none"
SKILL_INSTEAD_ARM: Final = "skill-instead"
MINUS_PREFIX: Final = "minus-"
# two-sided; three null suites tested at 0.10 would give a 27% chance of one false keep
P_TEST_MAX: Final = 0.05
SIGN_FLIP_CASES_MAX: Final = 256  # keeps the exact test's table of sums small

REPORT_USAGE: Final = "usage: eval report steering/<name> [--model ID] [--home DIR]"
REPORT_ARGV_MAX: Final = 5
SPEND_USAGE: Final = "usage: eval spend <steering|skill|value>/<name> --model ID --max CREDITS"
SPEND_ARGV_LEN: Final = 5
EXIT_OK: Final = 0
EXIT_BAD_LEDGER: Final = 1
EXIT_USAGE: Final = 2


@dataclass(frozen=True)
class ReportLimits:
    min_cases_for_test: int  # fewer cases can't stand for the rule, whatever p says
    ledger_lines_max: int
    # a null result on this many cases is advice, not noise; starting guesses, not yet checked
    # against what happened after a rule was moved or cut
    demote_words_max: int  # longer than this and no lift: the rule should be a skill
    demote_cases_min: int
    prune_cases_min: int  # at least demote_cases_min: cutting needs more proof than moving


@dataclass(frozen=True)
class PairedResult:
    n_cases: int
    mean_diff: float
    helped: int  # cases the first arm passes more often than the second
    harmed: int  # cases it passes less often
    p_two_sided: float  # exact sign-flip test; 1.0 when no case differs


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
    case_sha: str | None  # None on lines written before cases were versioned


@dataclass(frozen=True)
class CaseRow:
    case_id: str
    full: str  # "passed/finished" per arm, "-" when the arm has no finished run
    minus: str
    none: str
    skill: str  # the skill-instead arm


@dataclass(frozen=True)
class FileReport:
    suite: str
    model_id: str
    file_bytes: int | None
    file_words: int | None
    rows: tuple[CaseRow, ...]
    broken: tuple[str, ...]  # every finished run of every arm fails: the case or grader, out
    helped: tuple[str, ...]  # scored cases that pass less often with the file removed
    harmed: tuple[str, ...]  # scored cases that pass more often with it removed
    none_worse: tuple[str, ...]  # scored cases the none arm passes less often than full
    skill_worse: tuple[str, ...]  # scored cases that pass less often with the rule as a skill
    vs_minus: PairedResult | None
    vs_none: PairedResult | None
    vs_skill: PairedResult | None
    credits_per_run: float | None
    excluded_runs: int  # runs not finished (timeout, harness_error, budget_abort, contaminated)
    set_aside_runs: int | None  # runs of an older or unrecorded case version; None: not checked
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
        min_cases_for_test=_positive_int(raw, "MIN_CASES_FOR_TEST"),
        ledger_lines_max=_positive_int(raw, "LEDGER_LINES_MAX"),
        demote_words_max=_positive_int(raw, "DEMOTE_WORDS_MAX"),
        demote_cases_min=_positive_int(raw, "DEMOTE_CASES_MIN"),
        prune_cases_min=_positive_int(raw, "PRUNE_CASES_MIN"),
    )
    if limits.prune_cases_min < limits.demote_cases_min:
        raise ValueError("limits.json: PRUNE_CASES_MIN must be at least DEMOTE_CASES_MIN")
    assert limits.min_cases_for_test > 0, "bounds are positive"
    return limits


# ---------------------------------------------------------------- scoring


def pass_rate(passes: Sequence[bool]) -> Fraction:
    """Fraction of runs that passed, exact so the test sees true ties; at least one run."""
    assert len(passes) > 0, "a rate needs at least one run"
    rate = Fraction(sum(passes), len(passes))
    assert 0 <= rate <= 1, "a rate is a fraction"
    return rate


def sign_flip_p(diffs: Sequence[Fraction]) -> Fraction:
    """Exact two-sided p of the paired sign-flip test. With no effect, each case's difference is
    as likely to carry either sign, so p is the share of the 2^m sign patterns (m cases that
    differ) whose sum lies at least as far from 0 as the observed one. At one repeat a case's
    difference is -1, 0 or 1 and this is the exact sign test."""
    assert len(diffs) <= SIGN_FLIP_CASES_MAX, "the table of sums is bounded by the case count"
    sizes = [abs(d) for d in diffs if d != 0]
    observed = abs(sum(diffs, Fraction(0)))
    ways: dict[Fraction, int] = {Fraction(0): 1}  # sum -> sign patterns reaching it
    for size in sizes:  # bounded by SIGN_FLIP_CASES_MAX
        step: dict[Fraction, int] = {}
        for total, count in ways.items():
            step[total + size] = step.get(total + size, 0) + count
            step[total - size] = step.get(total - size, 0) + count
        ways = step
    extreme = sum(count for total, count in ways.items() if abs(total) >= observed)
    p = Fraction(extreme, 2 ** len(sizes))
    assert 0 < p <= 1, "the observed pattern is one of the extremes, so p is never 0"
    return p


def paired_diff(
    per_case_with: Sequence[Fraction], per_case_without: Sequence[Fraction]
) -> PairedResult:
    """Mean of with - without over cases, the cases each way, and the exact sign-flip p (3.7)."""
    n_cases = len(per_case_with)
    assert n_cases == len(per_case_without), "the arms are paired by case"
    if n_cases == 0:
        return PairedResult(0, 0.0, 0, 0, 1.0)
    diffs = [a - b for a, b in zip(per_case_with, per_case_without, strict=True)]
    result = PairedResult(
        n_cases=n_cases,
        mean_diff=float(sum(diffs, Fraction(0)) / n_cases),
        helped=sum(1 for d in diffs if d > 0),
        harmed=sum(1 for d in diffs if d < 0),
        p_two_sided=float(sign_flip_p(diffs)),
    )
    assert result.helped + result.harmed <= n_cases, "a case differs one way at most"
    return result


def effect_sign(result: PairedResult | None, limits: ReportLimits) -> int:
    """1 when the first arm is better, -1 when worse, 0 when the test can't tell: it needs
    min_cases_for_test cases and a two-sided p of at most P_TEST_MAX."""
    assert limits.min_cases_for_test > 0, "the case floor is positive"
    if result is None or result.n_cases < limits.min_cases_for_test:
        return 0
    if result.p_two_sided > P_TEST_MAX:
        return 0
    assert result.mean_diff != 0.0, "a small p needs cases that differ, and not evenly"
    return 1 if result.mean_diff > 0 else -1


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
    sha = row.get("case_sha")
    if (
        not all(strings.values())
        or (sha is not None and (not isinstance(sha, str) or not sha))
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
        case_sha=sha,
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
    """One run per (case version, arm, repeat) for this model; a later ledger line replaces an
    earlier one of the same case version, never a run of another version."""
    assert model_id, "the report is for one model"
    latest: dict[tuple[str, str | None, str, int], LedgerRun] = {}
    for run in runs:  # bounded: read_ledger capped the list
        if run.model_id == model_id:
            latest[(run.case_id, run.case_sha, run.arm, run.repeat_index)] = run
    assert len(latest) <= len(runs), "dedup never adds runs"
    return list(latest.values())


class Versioned(Protocol):
    @property
    def case_id(self) -> str: ...
    @property
    def case_sha(self) -> str | None: ...


def current_versions[RunT: Versioned](
    runs: Sequence[RunT], shas: dict[str, str]
) -> tuple[list[RunT], int]:
    """(the runs of each case as cases.json has it now, how many were set aside). A run with no
    case_sha predates versioning and is set aside too: its case may have changed since."""
    kept = [r for r in runs if r.case_sha is not None and shas.get(r.case_id) == r.case_sha]
    assert len(kept) <= len(runs), "a filter never adds runs"
    return kept, len(runs) - len(kept)


def current_case_shas(root: Path, kind: str, name: str) -> dict[str, str] | str | None:
    """case id -> case_sha for <kind dir>/<name>/cases.json as it stands; None when the root has no
    such file (a ledger shipped on its own, as in examples/); a str says why it can't be read."""
    assert kind in SUITE_DIRS and name, "a suite kind and name"
    path = root / SUITE_DIRS[kind] / name / "cases.json"
    if not path.exists():
        return None
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
        limits = load_limits(root / "limits.json")
    except (OSError, UnicodeDecodeError, ValueError) as err:  # JSONDecodeError is a ValueError
        return f"cannot read {path}: {err}"
    items = cast(list[object], value) if isinstance(value, list) else []
    if not items or len(items) > limits.cases_per_suite_max:
        return f"{path}: must be a list of 1 to {limits.cases_per_suite_max} cases"
    shas: dict[str, str] = {}
    for item in items:  # bounded by cases_per_suite_max above
        entry = cast(dict[str, object], item) if isinstance(item, dict) else {}
        case_id = entry.get("id")
        if not isinstance(case_id, str) or not case_id or case_id in shas:
            return f"{path}: every case needs its own id"
        shas[case_id] = case_sha(entry, root, limits.fixture_files_max)
    assert len(shas) == len(items), "one sha per case"
    return shas


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


def _pair(rates_full: dict[str, Fraction], rates_other: dict[str, Fraction]) -> PairedResult | None:
    """Paired over the cases both arms have; None when they share no case."""
    shared = sorted(set(rates_full) & set(rates_other))
    if not shared:
        return None
    return paired_diff([rates_full[c] for c in shared], [rates_other[c] for c in shared])


def _p_text(p: float) -> str:
    assert 0.0 < p <= 1.0, "an exact p is never 0"
    return f"{p:.3f}" if p >= 0.001 else "<0.001"


def skill_held(
    vs_skill: PairedResult | None, skill_worse: int, file_words: int | None, limits: ReportLimits
) -> int | None:
    """Cases a long rule held as a skill, when there are enough to move it; else None."""
    assert skill_worse >= 0, "a count"
    if vs_skill is None or skill_worse:
        return None
    long_rule = file_words is not None and file_words > limits.demote_words_max
    if not long_rule or vs_skill.n_cases < limits.demote_cases_min:
        return None
    assert vs_skill.n_cases > 0, "enough cases is some cases"
    return vs_skill.n_cases


def decide(
    scored_n: int,
    vs_minus: PairedResult | None,
    harmed: Sequence[str],
    none_worse: int,
    file_words: int | None,
    limits: ReportLimits,
    held_as_skill: int | None = None,
) -> str:
    """7.1 outcome rule. Order matters: no data, a rule that does harm, a removal that matters
    (moved to a skill if it held every case as one), the test, a changed case, and only then the
    null result. A keep still names the cases that pass more often without the rule."""
    assert scored_n >= 0 and none_worse >= 0, "counts are not negative"
    if scored_n == 0:
        return "incomplete: no case to score (each is broken in every arm or has no full run)"
    if vs_minus is None:
        return "incomplete: no finished minus-arm runs"
    assert len(harmed) == vs_minus.harmed, "the case list and the count agree"
    sign, p = effect_sign(vs_minus, limits), _p_text(vs_minus.p_two_sided)
    if sign < 0:
        return (
            f"harm: {vs_minus.harmed} case(s) pass more often without the rule and "
            f"{vs_minus.helped} less, p={p} two-sided; better without the rule: "
            f"{', '.join(harmed)}; fix the rule or cut it"
        )
    if (sign > 0 or vs_minus.helped) and held_as_skill is not None:
        return (
            f"demote: {file_words} words loaded every session, and as a skill it held all "
            f"{held_as_skill} cases; move it to a skill and keep a one-line pointer"
        )
    if sign > 0:
        tail = f"; better without the rule: {', '.join(harmed)}" if harmed else ""
        return f"keep: removal breaks {vs_minus.helped} case(s), p={p} two-sided{tail}"
    if vs_minus.helped or vs_minus.harmed:
        return (
            f"watch: removal changed {vs_minus.helped + vs_minus.harmed} case(s), "
            f"{vs_minus.helped} worse and {vs_minus.harmed} better without the rule; "
            f"p={p} over {vs_minus.n_cases} cases: too weak to call"
        )
    return null_verdict(vs_minus.n_cases, none_worse, file_words, limits)


def null_verdict(
    n_cases: int, none_worse: int, file_words: int | None, limits: ReportLimits
) -> str:
    """Removal changed no case. Prune needs the most cases, demote fewer but a long rule; under
    both it stays a watch. Cuts go one file at a time because overlapping rules mask each other."""
    assert n_cases > 0 and none_worse >= 0, "a null result has cases"
    assert limits.demote_cases_min <= limits.prune_cases_min, "the loader checks the order"
    if n_cases >= limits.prune_cases_min:
        if none_worse:
            return (
                f"prune: no lift over {n_cases} cases, yet the none arm fails, so another rule "
                "covers this one; merge them, one file at a time, re-running after each"
            )
        return (
            f"prune: no lift over {n_cases} cases and the model obeys with no rules at all; "
            "cut it, one file at a time, re-running after each"
        )
    long_rule = file_words is not None and file_words > limits.demote_words_max
    if long_rule and n_cases >= limits.demote_cases_min:
        return (
            f"demote: {file_words} words loaded every session and no lift over {n_cases} cases; "
            "move the detail to a skill and keep a short pointer"
        )
    if none_worse:
        return "watch: null result, but the rule fails in the none arm"
    return "non-discriminating: null result and the none arm never failed (too few cases to cut)"


def build_report(
    suite: str,
    model_id: str,
    runs: Sequence[LedgerRun],
    file_bytes: int | None,
    file_words: int | None,
    limits: ReportLimits,
    shas: dict[str, str] | None = None,
) -> FileReport:
    """shas is case id -> case_sha from cases.json; None reports every version, unchecked."""
    assert suite and model_id, "a suite and a model"
    name = suite.partition("/")[2]
    assert name, "suite is steering/<name>"
    mine, set_aside = latest_runs(runs, model_id), None
    if shas is not None:
        mine, set_aside = current_versions(mine, shas)
    arms = [
        _by_arm(mine, arm) for arm in (FULL_ARM, MINUS_PREFIX + name, NONE_ARM, SKILL_INSTEAD_ARM)
    ]
    full, minus, none, skill = arms
    seen = {c for arm in arms for c in arm}
    # a case no arm ever passes is a broken case or grader; a failing full arm alone is evidence
    broken = tuple(sorted(c for c in seen if not any(any(arm.get(c, ())) for arm in arms)))
    scored = [c for c in full if c not in broken]
    rate_full = {c: pass_rate(full[c]) for c in scored}
    rate_minus = {c: pass_rate(minus[c]) for c in scored if c in minus}
    rate_none = {c: pass_rate(none[c]) for c in scored if c in none}
    rate_skill = {c: pass_rate(skill[c]) for c in scored if c in skill}
    vs_minus, vs_none = _pair(rate_full, rate_minus), _pair(rate_full, rate_none)
    vs_skill = _pair(rate_full, rate_skill)
    helped = tuple(sorted(c for c in rate_minus if rate_minus[c] < rate_full[c]))
    harmed = tuple(sorted(c for c in rate_minus if rate_minus[c] > rate_full[c]))
    none_worse = tuple(sorted(c for c in rate_none if rate_none[c] < rate_full[c]))
    skill_worse = tuple(sorted(c for c in rate_skill if rate_skill[c] < rate_full[c]))
    held = skill_held(vs_skill, len(skill_worse), file_words, limits)
    credits = [r.credits for r in mine if r.credits is not None and r.status in FINISHED_STATUSES]
    cases = sorted({r.case_id for r in mine})
    return FileReport(
        suite=suite,
        model_id=model_id,
        file_bytes=file_bytes,
        file_words=file_words,
        rows=tuple(
            CaseRow(
                c, _cell(full.get(c)), _cell(minus.get(c)), _cell(none.get(c)), _cell(skill.get(c))
            )
            for c in cases
        ),
        broken=broken,
        helped=helped,
        harmed=harmed,
        none_worse=none_worse,
        skill_worse=skill_worse,
        vs_minus=vs_minus,
        vs_none=vs_none,
        vs_skill=vs_skill,
        credits_per_run=sum(credits) / len(credits) if credits else None,
        excluded_runs=sum(1 for r in mine if r.status not in FINISHED_STATUSES),
        set_aside_runs=set_aside,
        verdict=decide(len(scored), vs_minus, harmed, len(none_worse), file_words, limits, held),
    )


def _pair_line(label: str, result: PairedResult | None, limits: ReportLimits) -> str:
    if result is None:
        return f"{label}: no runs"
    few = result.n_cases < limits.min_cases_for_test
    note = f"; n < {limits.min_cases_for_test}, no verdict" if few else ""
    return (
        f"{label}: n={result.n_cases} mean_diff={result.mean_diff:+.3f} "
        f"helped={result.helped} harmed={result.harmed} p={_p_text(result.p_two_sided)} "
        f"(exact two-sided sign-flip{note})"
    )


def _cases_text(cases: Sequence[str]) -> str:
    return ", ".join(cases) or "none"


def render_table(report: FileReport, limits: ReportLimits) -> list[str]:
    """Report lines, status line last. Raw per-case counts first, then the paired results."""
    name = report.suite.partition("/")[2]
    assert name, "suite is steering/<name>"
    size = "?" if report.file_bytes is None else str(report.file_bytes)
    words = "?" if report.file_words is None else str(report.file_words)
    credits = "?" if report.credits_per_run is None else f"{report.credits_per_run:.4f}"
    versions = (
        "case versions: not checked, no cases.json beside the ledger"
        if report.set_aside_runs is None
        else f"older case versions set aside: {report.set_aside_runs}"
    )
    lines = [
        f"{report.suite} model={report.model_id} file_bytes={size} file_words={words} "
        f"credits_per_run={credits}",
        f"{'case':<42} {'full':>5} {'minus-' + name:>8} {'none':>5} {SKILL_INSTEAD_ARM:>13}",
    ]
    lines += [
        f"{r.case_id:<42} {r.full:>5} {r.minus:>8} {r.none:>5} {r.skill:>13}" for r in report.rows
    ]
    lines += [
        _pair_line(f"full vs minus-{name}", report.vs_minus, limits),
        _pair_line("full vs none", report.vs_none, limits),
        _pair_line(f"full vs {SKILL_INSTEAD_ARM}", report.vs_skill, limits),
        f"broken in every arm (out of the tally): {_cases_text(report.broken)}",
        f"worse without the rule: {_cases_text(report.helped)}",
        f"better without the rule: {_cases_text(report.harmed)}",
        f"worse in none: {_cases_text(report.none_worse)}",
        f"worse as a skill: {_cases_text(report.skill_worse)}",
        f"unfinished runs excluded: {report.excluded_runs}",
        versions,
        f"verdict: {report.verdict}",
        f"report: suite={report.suite} model={report.model_id} cases={len(report.rows)} "
        f"versions={'unchecked' if report.set_aside_runs is None else 'checked'} "
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


def rule_size(path: Path) -> tuple[int | None, int | None]:
    """(bytes, words) of the rule file; None for what can't be read, so the report says "?"."""
    if not path.is_file():
        return None, None
    size = path.stat().st_size
    try:
        words = len(path.read_text(encoding="utf-8").split())
    except (OSError, UnicodeDecodeError):
        return size, None
    assert size >= 0 and words >= 0, "sizes are not negative"
    return size, words


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
    shas = current_case_shas(root, "steering", name)
    if isinstance(shas, str):
        return [shas, f"report: suite={suite} unreadable cases"], EXIT_BAD_LEDGER
    size, words = rule_size(kiro_home / "steering" / f"{name}.md")
    report = build_report(suite, model_id, runs, size, words, limits, shas)
    lines = render_table(report, limits)
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
