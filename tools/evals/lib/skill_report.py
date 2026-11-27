"""`eval report skill/<name>`: raw counts per case and arm for a skill suite (design 6, 7.2 step 4).

Reads ledger/skill_<name>.jsonl, and the suite's cases.json only to keep the runs of each case as
it stands now (case_sha); no model calls. Never says "delete": a verdict is watch, keep, harm or
non-discriminating. The test is an exact two-sided sign-flip test over cases, both ways, and a
verdict needs MIN_CASES_FOR_TEST cases.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Final, cast

from lib.report import (
    EXIT_BAD_LEDGER,
    EXIT_OK,
    EXIT_USAGE,
    FINISHED_STATUSES,
    P_TEST_MAX,
    ReportLimits,
    current_case_shas,
    current_versions,
    effect_sign,
    load_report_limits,
    paired_diff,
)
from lib.run import CLAUDE_DEFAULT_MODEL

SKILL_REPORT_USAGE: Final = "usage: eval report skill/<name> [--model ID]"
WITH_ARM: Final = "with"
WITHOUT_ARM: Final = "without"
TARGET_SKILL: Final = "tiger-style"
ACTIVATION_COMPONENTS: Final = ("trig-pos-dev", "trig-pos-held", "trig-neg-dev", "trig-neg-held")
COMPONENT_PREFIXES: Final = (  # first match wins
    ("trig-pos-dev", "trig-pos-dev"),
    ("trig-pos-held", "trig-pos-held"),
    ("trig-neg-dev", "trig-neg-dev"),
    ("trig-neg-held", "trig-neg-held"),
    ("rev-py-clean", "rev-clean"),
    ("rev-", "rev-fire"),
    ("wr-py", "wr-py"),
    ("wr-", "wr-other"),
)
RECALL_RE: Final = re.compile(r"in-category (\d+)/(\d+), outside (\d+)/(\d+)")
RATIO_RE: Final = re.compile(r"; (\d+)/(\d+)$")
LOUD_RE: Final = re.compile(r"^(\d+) P0/P1 findings of (\d+)")
WRITE_GRADERS: Final = ("length", "assertions", "loops", "units", "pyright")
LINES_PER_FILE_MAX: Final = 100_000


@dataclass(frozen=True)
class SkillRun:
    case_id: str
    arm: str
    model_id: str
    repeat_index: int
    status: str
    passed: bool
    credits: float | None
    skills_read: tuple[str, ...]
    grades: tuple[tuple[str, str, str], ...]  # (assertion id, verdict, evidence)
    case_sha: str | None = None  # None on lines written before cases were versioned


@dataclass(frozen=True)
class ReviewTotals:
    in_found: int
    in_total: int
    out_found: int
    out_total: int
    prec_matched: int
    prec_findings: int
    sev_ok: int
    sev_matched: int
    loud_clean: int  # P0/P1 findings on the clean control
    parse_failures: int


def _grade_tuples(raw: object) -> tuple[tuple[str, str, str], ...] | None:
    if not isinstance(raw, list):
        return None
    result: list[tuple[str, str, str]] = []
    for item in cast(list[object], raw):
        row = cast(dict[str, object], item) if isinstance(item, dict) else {}
        ident, verdict, evidence = row.get("assertion_id"), row.get("verdict"), row.get("evidence")
        if not (isinstance(ident, str) and isinstance(verdict, str) and isinstance(evidence, str)):
            return None
        result.append((ident, verdict, evidence))
    assert len(result) == len(cast(list[object], raw)), "every grade parsed or none returned"
    assert all(isinstance(v, str) for _, v, _ in result), "verdicts are text"
    return tuple(result)


def _skill_run(text: str, line_no: int) -> SkillRun | str:
    assert line_no >= 1, "line numbers are 1-based"
    assert text.strip(), "blank lines are skipped by the caller"
    try:
        value: object = json.loads(text)
    except json.JSONDecodeError as err:
        return f"ledger line {line_no}: {err}"
    row = cast(dict[str, object], value) if isinstance(value, dict) else {}
    grades, skills = _grade_tuples(row.get("grades")), row.get("skills_read")
    credits, repeat = row.get("credits"), row.get("repeat_index")
    texts = [row.get(k) for k in ("case_id", "arm", "status", "model_id")]
    sha = row.get("case_sha")
    credits_ok = credits is None or (
        isinstance(credits, (int, float)) and not isinstance(credits, bool)
    )
    if (
        not all(isinstance(t, str) and t for t in texts)
        or not isinstance(row.get("case_passed"), bool)
        or isinstance(repeat, bool)
        or not isinstance(repeat, int)
        or not credits_ok
        or grades is None
        or not isinstance(skills, list)
        or (sha is not None and (not isinstance(sha, str) or not sha))
    ):
        return f"ledger line {line_no}: missing or mistyped field"
    return SkillRun(
        case_id=cast(str, texts[0]),
        arm=cast(str, texts[1]),
        model_id=cast(str, texts[3]),
        repeat_index=repeat,
        status=cast(str, texts[2]),
        passed=cast(bool, row["case_passed"]),
        credits=None if credits is None else float(cast(float, credits)),
        skills_read=tuple(str(s) for s in cast(list[object], skills)),
        grades=grades,
        case_sha=sha,
    )


def read_skill_ledger(path: Path, lines_max: int) -> list[SkillRun] | str:
    """Every run in file order, or the first reason the ledger cannot be read (never skips)."""
    assert lines_max > 0, "line bound is positive"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as err:
        return f"cannot read {path}: {err}"
    if len(lines) > lines_max:
        return f"{path}: {len(lines)} lines, limit is {lines_max}"
    runs: list[SkillRun] = []
    for line_no, text in enumerate(lines, start=1):  # bounded by lines_max above
        if not text.strip():
            continue
        parsed = _skill_run(text, line_no)
        if isinstance(parsed, str):
            return parsed
        runs.append(parsed)
    assert len(runs) <= len(lines), "at most one run per line"
    return runs


def component_of(case_id: str) -> str:
    assert case_id, "a case id"
    assert len(COMPONENT_PREFIXES) > 0, "the prefix table is not empty"
    for prefix, component in COMPONENT_PREFIXES:
        if case_id.startswith(prefix):
            return component
    raise AssertionError(f"unknown case id {case_id}: add it to COMPONENT_PREFIXES")


def latest(runs: Sequence[SkillRun]) -> list[SkillRun]:
    """One run per (case version, arm, repeat); a later ledger line replaces an earlier one of
    the same case version."""
    seen: dict[tuple[str, str | None, str, int], SkillRun] = {}
    for run in runs:  # bounded: read_skill_ledger capped the list
        seen[(run.case_id, run.case_sha, run.arm, run.repeat_index)] = run
    assert len(seen) <= len(runs), "dedup never adds runs"
    assert not runs or seen, "a non-empty ledger keeps at least one run"
    return list(seen.values())


def finished(runs: Sequence[SkillRun], arm: str) -> list[SkillRun]:
    assert arm, "an arm"
    done = [r for r in runs if r.arm == arm and r.status in FINISHED_STATUSES]
    assert len(done) <= len(runs), "a filter never adds runs"
    return done


def activation_counts(runs: Sequence[SkillRun], arm: str) -> dict[str, tuple[int, int]]:
    """component -> (runs that loaded the skill, finished runs) for the four trigger cells."""
    assert arm, "an arm"
    counts = {name: (0, 0) for name in ACTIVATION_COMPONENTS}
    for run in finished(runs, arm):
        component = component_of(run.case_id)
        if component in counts:
            loaded, total = counts[component]
            counts[component] = (loaded + (TARGET_SKILL in run.skills_read), total + 1)
    assert all(a <= b for a, b in counts.values()), "loaded never exceeds total"
    return counts


def _grade(run: SkillRun, assertion_id: str) -> tuple[str, str] | None:
    assert assertion_id, "an assertion id"
    assert len({g[0] for g in run.grades}) == len(run.grades), "assertion ids are unique"
    for ident, verdict, evidence in run.grades:
        if ident == assertion_id:
            return verdict, evidence
    return None


def review_totals(runs: Sequence[SkillRun], arm: str) -> ReviewTotals:
    """Sums over the finished review runs of this arm, from the grader evidence strings."""
    sums = [0] * 8  # in, in_total, out, out_total, prec, findings, sev, sev_matched
    loud, parse_failures = 0, 0
    for run in finished(runs, arm):
        if not component_of(run.case_id).startswith("rev-"):
            continue
        recall = _grade(run, "recall")
        if recall is not None and recall[1].startswith("no parse"):
            parse_failures += 1
        for assertion, pattern in (("recall", RECALL_RE), ("precision", RATIO_RE)):
            got = _grade(run, assertion)
            match = pattern.search(got[1]) if got else None
            if match and assertion == "recall":
                sums[0:4] = [s + int(g) for s, g in zip(sums[0:4], match.groups(), strict=True)]
            elif match:
                sums[4:6] = [s + int(g) for s, g in zip(sums[4:6], match.groups(), strict=True)]
        severity = _grade(run, "severity")
        sev_match = RATIO_RE.search(severity[1]) if severity else None
        if sev_match:
            sums[6:8] = [s + int(g) for s, g in zip(sums[6:8], sev_match.groups(), strict=True)]
        clean = _grade(run, "clean_quiet")
        loud_match = LOUD_RE.match(clean[1]) if clean else None
        loud += int(loud_match.group(1)) if loud_match else 0
    assert all(s >= 0 for s in sums) and loud >= 0, "counts are not negative"
    assert sums[0] <= sums[1] and sums[2] <= sums[3], "found never exceeds planted"
    return ReviewTotals(*sums, loud_clean=loud, parse_failures=parse_failures)


def write_totals(runs: Sequence[SkillRun], arm: str) -> dict[str, tuple[int, int]]:
    """grader name -> (PASS, finished python write runs); a missing grade counts as not passed."""
    totals = {name: (0, 0) for name in WRITE_GRADERS}
    for run in finished(runs, arm):
        if component_of(run.case_id) != "wr-py":
            continue
        for name in WRITE_GRADERS:
            got = _grade(run, name)
            passed, total = totals[name]
            totals[name] = (passed + bool(got and got[0] == "PASS"), total + 1)
    assert all(a <= b for a, b in totals.values()), "passes never exceed runs"
    assert len({b for _, b in totals.values()}) == 1, "every grader counts the same runs"
    return totals


def case_flags(
    runs: Sequence[SkillRun], arm: str, components: Sequence[str], assertion_ids: Sequence[str]
) -> dict[str, float]:
    """case id -> 1.0 when every named assertion passed in every finished run, else 0.0."""
    flags: dict[str, list[bool]] = {}
    for run in finished(runs, arm):
        if component_of(run.case_id) not in components:
            continue
        ok = all((_grade(run, a) or ("", ""))[0] == "PASS" for a in assertion_ids)
        flags.setdefault(run.case_id, []).append(ok)
    result = {case: float(all(f)) for case, f in flags.items()}
    assert len(result) <= len(flags), "one flag per case"
    assert all(v in (0.0, 1.0) for v in result.values()), "flags are 0 or 1"
    return result


def verdict_for(
    with_flags: dict[str, float], without_flags: dict[str, float], limits: ReportLimits
) -> str:
    """keep | harm | watch | non-discriminating. keep and harm need n >= MIN_CASES_FOR_TEST and a
    two-sided p <= P_TEST_MAX, one each way; non-discriminating when the without arm never failed
    and the skill never cost a case. Never drop or delete."""
    shared = sorted(set(with_flags) & set(without_flags))
    assert len(shared) <= min(len(with_flags), len(without_flags)), "shared is a subset"
    assert limits.min_cases_for_test > 0, "the case floor is positive"
    if not shared:
        return "watch"  # no paired data
    result = paired_diff(
        [Fraction(with_flags[c]) for c in shared], [Fraction(without_flags[c]) for c in shared]
    )
    sign = effect_sign(result, limits)
    if sign != 0:
        return "keep" if sign > 0 else "harm"
    if result.harmed == 0 and all(without_flags[c] == 1.0 for c in shared):
        return "non-discriminating"
    return "watch"


def activation_verdict(counts: dict[str, tuple[int, int]]) -> str:
    """No without arm exists for a trigger case, so: watch, or non-discriminating when positives
    load no more often than negatives. Never keep (no paired comparison)."""
    pos = [counts[c] for c in ACTIVATION_COMPONENTS if "pos" in c]
    neg = [counts[c] for c in ACTIVATION_COMPONENTS if "neg" in c]
    pos_n, neg_n = sum(t for _, t in pos), sum(t for _, t in neg)
    assert pos_n >= 0 and neg_n >= 0, "totals are not negative"
    assert set(counts) >= set(ACTIVATION_COMPONENTS), "every trigger cell is counted"
    if pos_n == 0 or neg_n == 0:
        return "watch"
    pos_loaded, neg_loaded = sum(a for a, _ in pos), sum(a for a, _ in neg)
    return "non-discriminating" if pos_loaded * neg_n <= neg_loaded * pos_n else "watch"


# ---------------------------------------------------------------- render


def _case_rows(runs: Sequence[SkillRun]) -> list[str]:
    cases = sorted({r.case_id for r in runs})
    lines = [f"{'case':<24} {'with':>6} {'without':>8}"]
    for case in cases:
        cells: list[str] = []
        for arm in (WITH_ARM, WITHOUT_ARM):
            done = [r for r in finished(runs, arm) if r.case_id == case]
            cells.append("-" if not done else f"{sum(r.passed for r in done)}/{len(done)}")
        lines.append(f"{case:<24} {cells[0]:>6} {cells[1]:>8}")
    assert len(lines) == len(cases) + 1, "a header and one row per case"
    assert len(cases) <= len(runs), "a case row needs a run"
    return lines


def _status_counts(runs: Sequence[SkillRun], all_lines: Sequence[SkillRun]) -> str:
    """Status counts over the latest runs; credits over every ledger line (reruns cost too)."""
    latest_credits = sum(r.credits or 0.0 for r in runs)
    ledger_credits = sum(r.credits or 0.0 for r in all_lines)
    pieces = {
        "contaminated": sum(r.status == "contaminated" for r in runs),
        "harness_errors": sum(r.status == "harness_error" for r in runs),
        "aborted": sum(r.status in ("timeout", "budget_abort") for r in runs),
    }
    assert all(v >= 0 for v in pieces.values()), "counts are not negative"
    assert ledger_credits >= latest_credits - 1e-9, "the latest runs are a subset of the ledger"
    return (
        " ".join(f"{k}={v}" for k, v in pieces.items())
        + f" credits_latest_runs={latest_credits:.4f} credits_all_lines={ledger_credits:.4f}"
    )


def _compare(
    runs: Sequence[SkillRun], components: list[str], ids: list[str], limits: ReportLimits
) -> str:
    """Verdict over cases scored on the assertion ids each case carries (a case that lacks an
    assertion, such as precision on the clean control, is not scored on it)."""

    def flags(arm: str) -> dict[str, float]:
        assert arm in (WITH_ARM, WITHOUT_ARM), "a skill arm"
        merged: dict[str, float] = {}
        for ident in ids:
            carried = {r.case_id for r in runs if _grade(r, ident) is not None}
            for case, value in case_flags(runs, arm, components, [ident]).items():
                if case in carried:
                    merged[case] = min(merged.get(case, 1.0), value)
        assert all(v in (0.0, 1.0) for v in merged.values()), "flags are 0 or 1"
        return merged

    assert ids and components, "something to compare"
    assert all(c in {p[1] for p in COMPONENT_PREFIXES} for c in components), "known components"
    return verdict_for(flags(WITH_ARM), flags(WITHOUT_ARM), limits)


def _review_line(arm: str, r: ReviewTotals) -> str:
    assert arm in (WITH_ARM, WITHOUT_ARM), "a skill arm"
    assert r.in_found <= r.in_total and r.out_found <= r.out_total, "found within planted"
    return (
        f"review[{arm}]: in-category {r.in_found}/{r.in_total} outside {r.out_found}/{r.out_total} "
        f"precision {r.prec_matched}/{r.prec_findings} severity {r.sev_ok}/{r.sev_matched} "
        f"clean-control P0/P1={r.loud_clean} parse_failures={r.parse_failures}"
    )


def _cells(counts: dict[str, tuple[int, int]]) -> str:
    assert counts, "at least one cell"
    assert all(a <= b for a, b in counts.values()), "a part never exceeds its total"
    return " ".join(f"{k}={a}/{b}" for k, (a, b) in counts.items())


def render_skill_report(
    suite: str,
    model_id: str,
    raw: Sequence[SkillRun],
    limits: ReportLimits,
    shas: dict[str, str] | None = None,
) -> list[str]:
    """shas is case id -> case_sha from cases.json; None scores every version, unchecked."""
    assert suite.startswith("skill/"), "a skill suite"
    assert all(r.model_id == model_id for r in raw), "the caller filtered to one model"
    runs, set_aside = latest(raw), None
    if shas is not None:
        runs, set_aside = current_versions(runs, shas)
    act = activation_counts(runs, WITH_ARM)
    lines = [f"{suite} model={model_id} runs={len(runs)} ledger_lines={len(raw)}"]
    lines += _case_rows(runs)
    lines.append("activation loaded/total: " + _cells(act))
    lines += [_review_line(arm, review_totals(runs, arm)) for arm in (WITH_ARM, WITHOUT_ARM)]
    lines += [
        f"write[{arm}] PASS/total: " + _cells(write_totals(runs, arm))
        for arm in (WITH_ARM, WITHOUT_ARM)
    ]
    lines.append(
        f"verdicts: exact two-sided sign-flip test, keep or harm at n >= "
        f"{limits.min_cases_for_test} cases and p <= {P_TEST_MAX}"
    )
    lines.append("status: " + _status_counts(runs, raw))
    lines.append(
        "case versions: not checked, no cases.json beside the ledger"
        if set_aside is None
        else f"older case versions set aside: {set_aside}"
    )
    verdicts = {
        "activation": activation_verdict(act),
        "review-recall": _compare(runs, ["rev-fire"], ["recall"], limits),
        "review-precision-clean": _compare(
            runs, ["rev-fire", "rev-clean"], ["precision", "clean_quiet"], limits
        ),
        "write-mechanical": _compare(runs, ["wr-py"], list(WRITE_GRADERS), limits),
    }
    lines += [f"verdict {name}: {value}" for name, value in verdicts.items()]
    lines.append(
        f"report: suite={suite} model={model_id} runs={len(runs)} "
        f"versions={'unchecked' if set_aside is None else 'checked'} "
        + " ".join(f"{k}={v}" for k, v in verdicts.items())
    )
    assert lines[-1].startswith("report: "), "the status line is last"
    return lines


def run_skill_report(argv: list[str], root: Path) -> tuple[list[str], int]:
    """(output lines, exit code) for `eval report skill/<name> [--model ID]`."""
    assert root.is_dir(), "root is the dataset directory"
    options = dict(zip(argv[1::2], argv[2::2], strict=False))
    bad = not argv or len(argv) % 2 == 0 or not argv[0].startswith("skill/")
    if bad or not argv[0].partition("/")[2] or set(options) - {"--model"}:
        return [SKILL_REPORT_USAGE], EXIT_USAGE
    model_id = options.get("--model", CLAUDE_DEFAULT_MODEL)
    limits = load_report_limits(root / "limits.json")
    ledger = root / "ledger" / (argv[0].replace("/", "_") + ".jsonl")
    runs = read_skill_ledger(ledger, limits.ledger_lines_max)
    if isinstance(runs, str):
        return [runs, f"report: suite={argv[0]} no ledger"], EXIT_BAD_LEDGER
    shas = current_case_shas(root, "skill", argv[0].partition("/")[2])
    if isinstance(shas, str):
        return [shas, f"report: suite={argv[0]} unreadable cases"], EXIT_BAD_LEDGER
    mine = [r for r in runs if r.model_id == model_id]
    lines = render_skill_report(argv[0], model_id, mine, limits, shas)
    assert lines[-1].startswith("report: "), "the status line is last"
    return lines, EXIT_OK
