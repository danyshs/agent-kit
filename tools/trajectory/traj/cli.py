"""Command line: `build`, `report`, `coverage`, `corrections`. Claude Code is the default
source; Kiro is read too when its store exists.

`python -m traj.cli <command> ...` is what `bin/traj` runs. Output always ends with the status line.
"""

from __future__ import annotations

import sys
from collections import Counter
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Final

from traj.common import NAME_RE, SESSION_ID_RE
from traj.corrections import counts_line, render, scan
from traj.limits import (
    BACKUP_DIR_DEFAULT,
    CLAUDE_PROJECTS_DEFAULT,
    CLI_BYTES_MAX,
    CLI_LINES_MAX,
    FILE_BYTES_MAX,
    HARNESSES,
    LINES_PER_FILE_MAX,
    SESSIONS_MAX,
    SKIP_REASONS,
    STAMP_CHARS_MAX,
)
from traj.quality import check_quality
from traj.reader_claude import find_claude_sessions, read_claude_session
from traj.reader_kiro import find_kiro_sessions, read_kiro_session
from traj.report import Coverage, render_coverage, render_fleet, render_session, status_line
from traj.schema import SessionData, SessionRecord, SessionRef, Skip
from traj.store import (
    backup_events,
    count_without_raw,
    list_stored,
    load_coverage,
    load_session,
    load_session_line,
    needs_rebuild,
    write_coverage,
    write_session,
    write_skips,
)

EXIT_OK: Final = 0
EXIT_BAD: Final = 1
EXIT_USAGE: Final = 2
DEFAULT_EVENTS_DIR: Final = Path(__file__).resolve().parents[1] / "out" / "events"
DEFAULT_HOME: Final = "~/.kiro"
HARNESS_CHOICES: Final = (*HARNESSES, "all")  # Claude Code is the primary source, Kiro the second
STAMP_CHARS_MAX_: Final = STAMP_CHARS_MAX  # re-export for tests

BUILD_USAGE: Final = (
    "usage: traj build [--harness claude|kiro|all] [--force] [--projects DIR] [--home DIR] "
    "[--events-dir DIR] "
    "[--state-dir DIR] [--backup-dir DIR] [--sessions-max N] [--lines-max N] [--bytes-max N] "
    "[--stamp TEXT]"
)
REPORT_USAGE: Final = (
    "usage: traj report (--fleet | --session UUID) [--harness claude|kiro|all] "
    "[--detectors default|all] [--since YYYY-MM-DD] [--workspace LABEL] "
    "[--events-dir DIR] [--out FILE] [--stamp TEXT]"
)
COVERAGE_USAGE: Final = (
    "usage: traj coverage [--harness claude|kiro|all] [--events-dir DIR] [--stamp TEXT]"
)
CORRECTIONS_USAGE: Final = (
    "usage: traj corrections --out FILE [--projects DIR] [--since YYYY-MM-DD]"
)
CORRECTIONS_DAYS_DEFAULT: Final = 7

BUILD_VALUE_FLAGS: Final = frozenset(
    "--harness --projects --home --events-dir --state-dir --backup-dir --sessions-max --lines-max "
    "--bytes-max --stamp".split()
)
BUILD_INT_FLAG_CAPS: Final = {
    "--sessions-max": SESSIONS_MAX,
    "--lines-max": CLI_LINES_MAX,
    "--bytes-max": CLI_BYTES_MAX,
}
REPORT_VALUE_FLAGS: Final = frozenset(
    "--session --harness --detectors --since --workspace --events-dir --out --stamp".split()
)
COVERAGE_VALUE_FLAGS: Final = frozenset("--harness --events-dir --stamp".split())
CORRECTIONS_VALUE_FLAGS: Final = frozenset("--out --projects --since".split())


def _flag_pairs(argv: list[str], value_flags: frozenset[str]) -> dict[str, str] | str:
    """Flags to values (`--force` and `--fleet` have value "1"). Unknown or valueless -> USAGE."""
    options: dict[str, str] = {}
    position = 0
    while position < len(argv):  # bounded by len(argv)
        flag = argv[position]
        bare = flag in ("--force", "--fleet")
        if flag in options or not (flag in value_flags or bare):
            return "unknown or repeated flag"
        if bare:
            options[flag] = "1"
            position += 1
            continue
        if position + 1 >= len(argv):
            return "missing value"
        options[flag] = argv[position + 1]
        position += 2
    return options


def parse_build_args(argv: list[str]) -> dict[str, str] | str:
    """Validated options for `build`, or the usage line. Argument validation is the CLI's job."""
    options = _flag_pairs(argv, BUILD_VALUE_FLAGS)
    if isinstance(options, str):
        return BUILD_USAGE
    if options.get("--harness", "all") not in HARNESS_CHOICES:
        return BUILD_USAGE
    for flag, cap in BUILD_INT_FLAG_CAPS.items():
        text = options.get(flag)
        if text is not None and not (text.isascii() and text.isdigit() and 0 < int(text) <= cap):
            return f"{flag} must be an integer from 1 to {cap}"
    stamp = options.get("--stamp")
    if stamp is not None and not (stamp.strip() and len(stamp) <= STAMP_CHARS_MAX):
        return f"--stamp must be non-blank and at most {STAMP_CHARS_MAX} characters"
    return options


def parse_report_args(argv: list[str]) -> dict[str, str] | str:
    """Validated options for `report`, or the usage line. Exactly one of --fleet / --session."""
    options = _flag_pairs(argv, REPORT_VALUE_FLAGS)
    if isinstance(options, str):
        return REPORT_USAGE
    has_fleet = "--fleet" in options
    has_session = "--session" in options
    if has_fleet == has_session:  # neither or both
        return REPORT_USAGE
    if has_session:
        uuid = options["--session"]
        if SESSION_ID_RE.fullmatch(uuid) is None:
            return f"--session must be a uuid matching {SESSION_ID_RE.pattern}"
    if options.get("--harness", "all") not in HARNESS_CHOICES:
        return REPORT_USAGE
    detectors = options.get("--detectors", "default")
    if detectors not in ("default", "all"):
        return "--detectors must be default or all"
    since = options.get("--since")
    if since is not None:
        try:
            date.fromisoformat(since)
        except ValueError:
            return "--since must be a real date YYYY-MM-DD"
    workspace = options.get("--workspace")
    if workspace is not None and NAME_RE.fullmatch(workspace) is None:
        return f"--workspace must match {NAME_RE.pattern}"
    stamp = options.get("--stamp")
    if stamp is not None and not (stamp.strip() and len(stamp) <= STAMP_CHARS_MAX):
        return f"--stamp must be non-blank and at most {STAMP_CHARS_MAX} characters"
    return options


def parse_corrections_args(argv: list[str]) -> dict[str, str] | str:
    """Validated options for `corrections`, or the usage line. --out is required."""
    options = _flag_pairs(argv, CORRECTIONS_VALUE_FLAGS)
    if isinstance(options, str) or "--fleet" in options or "--force" in options:
        return CORRECTIONS_USAGE
    if not options.get("--out"):
        return CORRECTIONS_USAGE
    since = options.get("--since")
    if since is not None:
        try:
            date.fromisoformat(since)
        except ValueError:
            return "--since must be a real date YYYY-MM-DD"
    return options


def parse_coverage_args(argv: list[str]) -> dict[str, str] | str:
    """Validated options for `coverage`, or the usage line."""
    options = _flag_pairs(argv, COVERAGE_VALUE_FLAGS)
    if isinstance(options, str):
        return COVERAGE_USAGE
    if options.get("--harness", "all") not in HARNESS_CHOICES:
        return COVERAGE_USAGE
    stamp = options.get("--stamp")
    if stamp is not None and not (stamp.strip() and len(stamp) <= STAMP_CHARS_MAX):
        return f"--stamp must be non-blank and at most {STAMP_CHARS_MAX} characters"
    return options


def default_stamp() -> str:
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %z")
    assert 0 < len(stamp) <= STAMP_CHARS_MAX, "a default stamp passes its own bound"
    return stamp


# ---------------------------------------------------------------- build


def _count_record(counts: Counter[str], rec: SessionRecord) -> None:
    assert rec.events_used <= rec.events_total, "a validated session line nests its counts"
    counts["scanned"] += 1
    counts["zero_steps"] += rec.steps == 0
    for key, value in (
        ("malformed", rec.malformed_events),
        ("unknown", rec.unknown_events),
        ("parse_errors", rec.parse_errors),
        ("unpaired", rec.unpaired_calls),
        ("usage_conflicts", rec.usage_conflicts),
        ("negative", rec.negative_durations),
        ("used", rec.events_used),
        ("total", rec.events_total),
    ):
        counts[key] += value


def _skip(counts: Counter[str], skips: list[Skip], skip: Skip) -> None:
    assert skip.reason in SKIP_REASONS, "a closed reason"
    counts[f"reason:{skip.reason}"] += 1
    skips.append(skip)


def _read(ref: SessionRef, lines_max: int, bytes_max: int) -> SessionData | Skip:
    if ref.harness == "claude":
        return read_claude_session(ref, lines_max, bytes_max)
    assert ref.harness == "kiro", "two readers"
    return read_kiro_session(ref, lines_max, bytes_max)


def _find_refs(options: dict[str, str], sessions_max: int) -> list[SessionRef] | str:
    """Listed sessions of the chosen harnesses, Claude Code first, within one shared bound."""
    harness = options.get("--harness", "all")
    refs: list[SessionRef] = []
    if harness in ("claude", "all"):
        projects = Path(options.get("--projects", CLAUDE_PROJECTS_DEFAULT)).expanduser()
        found = find_claude_sessions(projects, sessions_max)
        if isinstance(found, str):
            return f"claude: {found}"
        refs.extend(found)
    if harness in ("kiro", "all"):
        home = Path(options.get("--home", DEFAULT_HOME)).expanduser()
        found = find_kiro_sessions(home / "sessions", sessions_max)
        # a machine without Kiro is normal now; a missing store is not an error under "all"
        if isinstance(found, str) and not (harness == "all" and not (home / "sessions").exists()):
            return f"kiro: {found}"
        refs.extend(found if isinstance(found, list) else [])
    if len(refs) > sessions_max:
        return f"over {sessions_max} sessions"
    return refs


def _build_one(
    ref: SessionRef,
    events_dir: Path,
    options: dict[str, str],
    counts: Counter[str],
    skips: list[Skip],
) -> None:
    """Skip, rebuild or reuse one listed session and add it to the tallies. OSError from the
    write propagates; ValueError (invalid or over-bound data) becomes `events not written`."""
    assert ref.harness in HARNESSES, "a known harness"
    if ref.skip_reason:
        _skip(counts, skips, Skip(ref.session_id, ref.harness, ref.skip_reason))
        return
    lines_max = int(options.get("--lines-max", LINES_PER_FILE_MAX))
    bytes_max = int(options.get("--bytes-max", FILE_BYTES_MAX))
    assert lines_max > 0 and bytes_max > 0, "parse_build_args validated the bounds"
    if needs_rebuild(events_dir, ref, "--force" in options):
        read = _read(ref, lines_max, bytes_max)
        if isinstance(read, Skip):
            _skip(counts, skips, read)
            return
        assert read.session.session_id == ref.session_id, "the data is for the listed session"
        try:
            write_session(events_dir, read)
        except ValueError:
            counts["not_written"] += 1
            return
        _count_record(counts, read.session)
        return
    rec = load_session_line(events_dir / ref.harness / f"{ref.session_id}.jsonl")
    if isinstance(rec, str):
        # file damaged between the rebuild check and the load; treat as a rebuild
        read = _read(ref, lines_max, bytes_max)
        if isinstance(read, Skip):
            _skip(counts, skips, read)
            return
        assert read.session.session_id == ref.session_id, "the data is for the listed session"
        try:
            write_session(events_dir, read)
        except ValueError:
            counts["not_written"] += 1
            return
        _count_record(counts, read.session)
        return
    _count_record(counts, rec)


def _coverage(counts: Counter[str], seen: int, without_raw: int) -> Coverage:
    skipped = tuple((reason, counts[f"reason:{reason}"]) for reason in SKIP_REASONS)
    accounted = counts["scanned"] + counts["not_written"] + sum(n for _, n in skipped)
    assert accounted == seen, "every listed session was tallied exactly once"
    return Coverage(
        seen=seen,
        scanned_from_raw=counts["scanned"],
        not_built_yet=0,
        skipped=skipped,
        events_not_written=counts["not_written"],
        events_without_raw_log=without_raw,
        filtered_out=0,
        malformed_events=counts["malformed"],
        unknown_events=counts["unknown"],
        parse_errors=counts["parse_errors"],
        unpaired_calls=counts["unpaired"],
        usage_conflicts=counts["usage_conflicts"],
        negative_durations=counts["negative"],
        truncated_findings_sessions=0,
        zero_step_sessions=counts["zero_steps"],
        events_used=counts["used"],
        events_total=counts["total"],
    )


def _oserror_name(err: OSError) -> str:
    name = (Path(str(err.filename)).name if err.filename else "") or "events"
    assert name, "a name to report"
    return name


def run_build(options: dict[str, str]) -> tuple[list[str], int]:
    """`build`: (output lines, exit code). Exit 1 on a fatal I/O error or data-quality error."""
    events_dir = Path(options.get("--events-dir", str(DEFAULT_EVENTS_DIR))).expanduser()
    state_dir = Path(options.get("--state-dir", str(events_dir.parent))).expanduser()
    backup_dir = Path(options.get("--backup-dir", BACKUP_DIR_DEFAULT)).expanduser()
    sessions_max = int(options.get("--sessions-max", SESSIONS_MAX))
    assert 0 < sessions_max <= SESSIONS_MAX, "parse_build_args validated the bound"
    refs = _find_refs(options, sessions_max)
    if isinstance(refs, str):
        return [f"build: error {refs}"], EXIT_BAD
    counts: Counter[str] = Counter()
    skips: list[Skip] = []
    try:
        for ref in refs:  # find_kiro_sessions bounded the listing
            _build_one(ref, events_dir, options, counts, skips)
    except OSError as err:
        return [f"build: error cannot write {_oserror_name(err)}: {err.strerror}"], EXIT_BAD
    without_raw = 0
    for harness in HARNESSES:
        stored = list_stored(events_dir, harness, sessions_max)
        if isinstance(stored, str):
            return [f"build: error {stored}"], EXIT_BAD
        without_raw += count_without_raw(stored, [r for r in refs if r.harness == harness])
    try:
        cov = _coverage(counts, len(refs), without_raw)
        write_skips(state_dir, skips)
        write_coverage(state_dir, cov)
    except ValueError as err:
        return [f"build: error {err}"], EXIT_BAD
    except OSError as err:
        return [f"build: error state {_oserror_name(err)}: {err.strerror}"], EXIT_BAD
    try:
        copied = backup_events(events_dir, backup_dir, sessions_max)
    except OSError as err:
        return [f"build: error backup {_oserror_name(err)}: {err.strerror}"], EXIT_BAD
    if isinstance(copied, str):
        return [f"build: error {copied}"], EXIT_BAD
    problems = check_quality(cov)
    lines = [f"# traj build {options.get('--stamp') or default_stamp()}", ""]
    lines.append(f"build: backup copied {copied} files")
    lines.extend(f"build: data-quality error {reason}" for reason in problems)
    lines.extend(["", *render_coverage(cov), "", status_line(cov)])
    return lines, EXIT_BAD if problems else EXIT_OK


# ---------------------------------------------------------------- report


def _load_all_sessions(
    events_dir: Path, sessions_max: int
) -> tuple[list[tuple[str, object]], str | None]:
    """Load every events file for every harness. Returns ([(path_str, data_or_reason)], error).
    `error` is non-None only on a fatal store listing failure."""
    results: list[tuple[str, object]] = []
    for harness in HARNESSES:
        stored = list_stored(events_dir, harness, sessions_max)
        if isinstance(stored, str):
            return results, stored
        for path in stored:  # bounded by sessions_max
            results.append((str(path), load_session(path)))
    return results, None


def _apply_filters(
    loaded: list[tuple[str, object]], options: dict[str, str]
) -> tuple[list[object], int]:
    """Filter loaded sessions by --since, --workspace, --harness.
    Returns (kept data list, filtered_out count). Errors (str results) are excluded silently
    (they become `not_built_yet` in coverage)."""
    since = options.get("--since")
    workspace = options.get("--workspace")
    harness_filter = options.get("--harness", "all")
    kept: list[object] = []
    filtered_out = 0
    for _, result in loaded:
        if isinstance(result, str):
            continue  # unreadable: not counted in filtered_out
        from traj.schema import SessionData as SD

        assert isinstance(result, SD), "load_session returns SessionData or str"
        data = result
        if harness_filter != "all" and data.session.harness != harness_filter:
            filtered_out += 1
            continue
        if since and data.session.created_day and data.session.created_day < since:
            filtered_out += 1
            continue
        if workspace and data.session.workspace_label != workspace:
            filtered_out += 1
            continue
        kept.append(data)
    return kept, filtered_out


def _empty_coverage() -> Coverage:
    """Placeholder coverage when no build has run yet."""
    return Coverage(
        seen=0,
        scanned_from_raw=0,
        not_built_yet=0,
        skipped=tuple((r, 0) for r in SKIP_REASONS),
        events_not_written=0,
        events_without_raw_log=0,
        filtered_out=0,
        malformed_events=0,
        unknown_events=0,
        parse_errors=0,
        unpaired_calls=0,
        usage_conflicts=0,
        negative_durations=0,
        truncated_findings_sessions=0,
        zero_step_sessions=0,
        events_used=0,
        events_total=0,
    )


def _active_filters(options: dict[str, str]) -> dict[str, str]:
    """Non-default filter values for the report header."""
    result: dict[str, str] = {}
    for key in ("--since", "--workspace", "--harness", "--detectors"):
        if key in options:
            result[key.lstrip("-")] = options[key]
    return result


def _write_out(lines: list[str], out_path: str, cov: Coverage) -> tuple[list[str], int] | None:
    """Write lines to `out_path`; return (status_line, 0) or (error, 1). None = no --out."""
    try:
        Path(out_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as err:
        return [f"report: error writing {out_path}: {err.strerror}"], EXIT_BAD
    return [status_line(cov)], EXIT_OK


def run_report(options: dict[str, str]) -> tuple[list[str], int]:
    """`report`: (output lines, exit code). Exit 0 even on data-quality errors."""
    stamp = options.get("--stamp") or default_stamp()
    events_dir = Path(options.get("--events-dir", str(DEFAULT_EVENTS_DIR))).expanduser()
    state_dir = events_dir.parent

    cov_result = load_coverage(state_dir)
    cov: Coverage = _empty_coverage() if isinstance(cov_result, str) else cov_result

    loaded, fatal = _load_all_sessions(events_dir, SESSIONS_MAX)
    if fatal is not None:
        return [f"report: error {fatal}"], EXIT_BAD

    from traj.schema import SessionData as SD

    not_built_yet = sum(1 for _, r in loaded if isinstance(r, str))
    all_datas = [r for _, r in loaded if isinstance(r, SD)]
    kept_objs, filtered_out = _apply_filters(list(loaded), options)
    datas = [d for d in kept_objs if isinstance(d, SD)]
    cov = replace(cov, not_built_yet=cov.not_built_yet + not_built_yet, filtered_out=filtered_out)

    out_path = options.get("--out")
    if "--session" in options:
        uuid = options["--session"]
        match = next((d for d in all_datas if d.session.session_id == uuid), None)
        if match is None:
            return [f"report: session {uuid} not found in events store"], EXIT_BAD
        lines = render_session(match, [], stamp)
    else:
        lines = render_fleet(datas, cov, stamp, _active_filters(options))

    if out_path:
        result = _write_out(lines, out_path, cov)
        assert result is not None, "_write_out returns a result when out_path is given"
        return result
    return lines, EXIT_OK


# ---------------------------------------------------------------- coverage


def run_coverage(options: dict[str, str]) -> tuple[list[str], int]:
    """`coverage`: print the last build's coverage table and status line."""
    stamp = options.get("--stamp") or default_stamp()
    events_dir = Path(options.get("--events-dir", str(DEFAULT_EVENTS_DIR))).expanduser()
    state_dir = events_dir.parent
    cov = load_coverage(state_dir)
    if isinstance(cov, str):
        return [f"coverage: error {cov}"], EXIT_BAD
    problems = check_quality(cov)
    lines = [f"# traj coverage  {stamp}", ""]
    if problems:
        for p in problems:
            lines += [f"> **DATA QUALITY: {p}**", ""]
    lines.extend(render_coverage(cov))
    lines += ["", status_line(cov)]
    return lines, EXIT_OK


# ---------------------------------------------------------------- corrections


def run_corrections(options: dict[str, str]) -> tuple[list[str], int]:
    """`corrections`: write the list to --out; stdout is the counts line only, never a quote."""
    out_path = Path(options["--out"]).expanduser()
    projects = Path(options.get("--projects", CLAUDE_PROJECTS_DEFAULT)).expanduser()
    until = datetime.now().astimezone()
    since_day = (
        date.fromisoformat(options["--since"])
        if "--since" in options
        else (until - timedelta(days=CORRECTIONS_DAYS_DEFAULT)).date()
    )
    since = datetime.combine(since_day, time()).astimezone()  # local midnight, as v1's --since
    if out_path.exists():
        return [f"error: {out_path} exists; nothing overwritten"], EXIT_BAD
    if since > until:
        return ["error: --since is in the future"], EXIT_BAD
    scanned = scan(projects, since, until)
    if isinstance(scanned, str):
        return [f"error: {scanned}"], EXIT_BAD
    entries, coverage = scanned
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(render(entries, coverage, since, until), encoding="utf-8")
    except OSError as err:
        return [f"error: writing {out_path}: {err.strerror}"], EXIT_BAD
    return [counts_line(entries)], EXIT_OK


# ---------------------------------------------------------------- main


def main(argv: list[str]) -> int:
    if not argv:
        print(BUILD_USAGE, file=sys.stderr)
        return EXIT_USAGE
    command = argv[0]
    rest = argv[1:]
    if command == "build":
        options = parse_build_args(rest)
        if isinstance(options, str):
            print(options, file=sys.stderr)
            return EXIT_USAGE
        lines, code = run_build(options)
    elif command == "report":
        options = parse_report_args(rest)
        if isinstance(options, str):
            print(options, file=sys.stderr)
            return EXIT_USAGE
        lines, code = run_report(options)
    elif command == "coverage":
        options = parse_coverage_args(rest)
        if isinstance(options, str):
            print(options, file=sys.stderr)
            return EXIT_USAGE
        lines, code = run_coverage(options)
    elif command == "corrections":
        options = parse_corrections_args(rest)
        if isinstance(options, str):
            print(options, file=sys.stderr)
            return EXIT_USAGE
        lines, code = run_corrections(options)
    else:
        print(BUILD_USAGE, file=sys.stderr)
        return EXIT_USAGE
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
