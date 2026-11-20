"""Entry point behind bin/eval. Subcommands: static (step 1), run (step 2), report (step 3),
spend (the paid-loop budget guard) and mine (the usage miner)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Final

from lib.dataset import (
    CoverageRow,
    Dataset,
    LintFinding,
    classify_coverage,
    collect_targets,
    discover_world,
    lint_all,
    load_dataset,
    load_limits,
    load_lint_baseline,
    split_baseline,
)
from lib.miner import run_mine
from lib.report import run_report, run_spend
from lib.run_cli import parse_run_args, run_suite
from lib.skill_report import run_skill_report

EVALS_ROOT: Final = Path(__file__).resolve().parent.parent
DEFAULT_KIRO_HOME: Final = Path("~/.kiro").expanduser()
DEFAULT_HOME: Final = Path("~/.claude").expanduser()
USAGE: Final = (
    "usage: eval static [--home DIR]\n"
    "       eval run|report|spend|mine ...  (each prints its own usage)"
)
STATUS_EXIT_BAD_DATA: Final = 1
STATUS_EXIT_USAGE: Final = 2
ARGS_MAX: Final = 4


def format_static(
    rows: list[CoverageRow],
    new_findings: list[LintFinding],
    known_findings: list[LintFinding],
    dataset: Dataset,
) -> list[str]:
    """Report lines: coverage table, lint findings, rejections, then the status line."""
    assert len({(r.target.kind, r.target.name) for r in rows}) == len(rows), "one row per target"
    lines = [f"{row.target.kind:<10} {row.target.name:<32} {row.status}" for row in rows]
    lines += [f"lint {f.rule} {f.path_text}: {f.message}" for f in new_findings]
    lines += [f"lint(baseline) {f.rule} {f.path_text}: {f.message}" for f in known_findings]
    lines += [
        f"rejected {r.file_text} [{r.record_id or '-'}]: {r.reason}" for r in dataset.rejections
    ]
    covered = sum(1 for row in rows if row.status == "covered")
    human_only = sum(1 for row in rows if row.status == "uncovered (human only)")
    uncovered = len(rows) - covered - human_only
    assert covered + human_only + uncovered == len(rows), "every row has exactly one status"
    lines.append(
        f"static: targets={len(rows)} covered={covered} uncovered={uncovered} "
        f"human_only={human_only} lint_new={len(new_findings)} "
        f"lint_baseline={len(known_findings)} rejections={len(dataset.rejections)}"
    )
    assert lines[-1].startswith("static: "), "the last line is always the status line"
    return lines


def run_static(kiro_home: Path, root: Path) -> tuple[list[str], int]:
    """(output lines, exit code). Uncovered targets are reported, not failed; new lint and
    rejections fail, because they mean a file is broken."""
    limits = load_limits(root / "limits.json")
    assert root.is_dir(), "root must be the dataset directory"
    baseline = load_lint_baseline(root / "lint_baseline.json")
    world = discover_world(kiro_home, root, limits)
    dataset = load_dataset(world, limits)
    rows = classify_coverage(collect_targets(world), dataset)
    new, known = split_baseline(lint_all(kiro_home, world, limits), baseline)
    lines = format_static(rows, new, known, dataset)
    bad = bool(new) or bool(dataset.rejections)
    code = STATUS_EXIT_BAD_DATA if bad else 0
    assert lines[-1].startswith("static: "), "status line is last"
    assert code in (0, STATUS_EXIT_BAD_DATA), "run_static exits 0 or 1"
    return lines, code


def parse_args(argv: list[str]) -> Path | str:
    """The source home (~/.claude or ~/.kiro), or a usage error text."""
    assert ARGS_MAX >= 3, "ARGS_MAX must allow `static --home DIR`"
    if not argv or argv[0] != "static" or len(argv) > ARGS_MAX:
        return USAGE
    if len(argv) == 1:
        return DEFAULT_HOME
    if len(argv) == 3 and argv[1] in ("--home", "--kiro-home"):
        assert argv[0] == "static", "only the static subcommand exists"
        return Path(argv[2]).expanduser()
    return USAGE


def main_run(argv: list[str]) -> int:
    """`eval run ...`: prints the plan, the runs and the status line; stderr only for usage."""
    assert all(isinstance(arg, str) for arg in argv), "argv holds strings"
    parsed = parse_run_args(argv)
    if isinstance(parsed, str):
        print(parsed, file=sys.stderr)
        return STATUS_EXIT_USAGE
    try:
        lines, code = run_suite(parsed, EVALS_ROOT)
    except ValueError as err:
        print(f"eval run: {err}", file=sys.stderr)
        return STATUS_EXIT_USAGE
    assert lines, "a run always prints at least the status line"
    print("\n".join(lines))
    return code


def main_report(argv: list[str]) -> int:
    """`eval report steering/<name>`: usage errors go to stderr, the table to stdout."""
    assert all(isinstance(arg, str) for arg in argv), "argv holds strings"
    try:
        if argv and argv[0].startswith("skill/"):
            lines, code = run_skill_report(argv, EVALS_ROOT)
        else:
            lines, code = run_report(argv, EVALS_ROOT, DEFAULT_HOME)
    except ValueError as err:
        print(f"eval report: {err}", file=sys.stderr)
        return STATUS_EXIT_USAGE
    assert lines, "a report always prints at least one line"
    print("\n".join(lines), file=sys.stderr if code == STATUS_EXIT_USAGE else sys.stdout)
    return code


def main_spend(argv: list[str]) -> int:
    """`eval spend <kind>/<name> --model ID --max N`: the paid-loop budget guard."""
    assert all(isinstance(arg, str) for arg in argv), "argv holds strings"
    try:
        lines, code = run_spend(argv, EVALS_ROOT)
    except ValueError as err:  # a bad limits.json: stop, never continue
        lines, code = [f"spend: error {err}"], STATUS_EXIT_BAD_DATA
    assert lines, "the guard always prints a line"
    print("\n".join(lines), file=sys.stderr if code == STATUS_EXIT_USAGE else sys.stdout)
    return code


def main_mine(argv: list[str]) -> int:
    """`eval mine ...`: the usage miner; usage errors to stderr."""
    assert all(isinstance(arg, str) for arg in argv), "argv holds strings"
    lines, code = run_mine(argv, EVALS_ROOT, {"claude": DEFAULT_HOME, "kiro": DEFAULT_KIRO_HOME})
    assert lines, "the miner always prints a line"
    print("\n".join(lines), file=sys.stderr if code == STATUS_EXIT_USAGE else sys.stdout)
    return code


def main(argv: list[str]) -> int:
    assert all(isinstance(arg, str) for arg in argv), "argv holds strings"
    if argv and argv[0] == "mine":
        return main_mine(argv[1:])
    if argv and argv[0] == "run":
        return main_run(argv[1:])
    if argv and argv[0] == "report":
        return main_report(argv[1:])
    if argv and argv[0] == "spend":
        return main_spend(argv[1:])
    parsed = parse_args(argv)
    if isinstance(parsed, str):
        print(parsed, file=sys.stderr)
        return STATUS_EXIT_USAGE
    try:
        lines, code = run_static(parsed, EVALS_ROOT)
    except ValueError as err:
        print(f"eval static: {err}", file=sys.stderr)
        return STATUS_EXIT_USAGE
    assert lines, "a run always prints at least the status line"
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
