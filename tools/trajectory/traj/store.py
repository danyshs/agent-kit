"""Events files on disk: atomic write, validated load, the rebuild decision, listing.

Layout: <out_dir>/<harness>/<session_id>.jsonl. Backup, skips and coverage files come in stage 2.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from traj.common import SESSION_ID_RE, as_dict, as_str, clean_name
from traj.limits import (
    EVENTS_BYTES_MAX,
    EVENTS_LINES_MAX,
    HARNESSES,
    READER_VERSION,
    RULE_VERSION,
    SCHEMA_VERSION,
    SESSION_LINE_BYTES_MAX,
    SESSIONS_MAX,
    SKIP_REASONS,
    STATE_FILE_BYTES_MAX,
    STORE_ENTRIES_MAX,
)
from traj.report import COUNT_FIELDS, Coverage, coverage_problem
from traj.schema import (
    SessionData,
    SessionRecord,
    SessionRef,
    Skip,
    check_session,
    decode_session,
    decode_session_line,
    encode_session,
)

SUFFIX = ".jsonl"
TEMP_SUFFIX = ".tmp"  # never matches list_stored
SKIPS_NAME = "skips.jsonl"
COVERAGE_NAME = "coverage.json"


def events_path(out_dir: Path, harness: str, session_id: str) -> Path:
    """Where a session's events file lives. The id is checked, so no path can escape out_dir."""
    if harness not in HARNESSES:
        raise ValueError(f"unknown harness {harness!r}")
    if not SESSION_ID_RE.fullmatch(session_id):
        raise ValueError(f"session_id {session_id!r} is not a uuid")
    return out_dir / harness / f"{session_id}{SUFFIX}"


def write_session(out_dir: Path, data: SessionData) -> None:
    """Atomic: temp file in the same directory, then replace. ValueError before any file is
    touched when the data is invalid or encodes over EVENTS_BYTES_MAX (a file load would refuse).
    OSError propagates after cleanup."""
    problem = check_session(data)
    if problem is not None:
        raise ValueError(f"invalid session: {problem}")
    target = events_path(out_dir, data.session.harness, data.session.session_id)
    text = "\n".join(encode_session(data)) + "\n"
    size = len(text.encode("utf-8"))
    if size > EVENTS_BYTES_MAX:
        raise ValueError(f"events file is {size} bytes, over the {EVENTS_BYTES_MAX} byte bound")
    _atomic_write(target, text)
    assert target.is_file(), "the events file exists after a write"


def _atomic_write(target: Path, text: str) -> None:
    """Temp file in the same directory, then replace; the temp is removed on any error."""
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=target.parent, prefix=".", suffix=TEMP_SUFFIX)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(text)
        os.replace(temp_name, target)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def load_session(path: Path) -> SessionData | str:
    """The validated records in one events file, or why it is not valid."""
    assert EVENTS_BYTES_MAX > 0 and EVENTS_LINES_MAX > 0, "bounds are positive"
    try:
        with path.open("rb") as handle:
            data = handle.read(EVENTS_BYTES_MAX + 1)  # a file that grows mid-read still stops here
    except OSError as err:
        return f"cannot read {path.name}: {err.strerror}"
    if len(data) > EVENTS_BYTES_MAX:
        return f"{path.name} is over the {EVENTS_BYTES_MAX} byte bound"
    if data.count(b"\n") > EVENTS_LINES_MAX:  # before decoding: a big junk file costs no parse
        return f"{path.name} has over {EVENTS_LINES_MAX} lines"
    try:
        text = data.decode("utf-8")
    except ValueError:
        return f"{path.name} is not UTF-8"
    return decode_session(text.splitlines())


def load_session_line(path: Path) -> SessionRecord | str:
    """Only the session line of an events file, validated. Reads at most SESSION_LINE_BYTES_MAX
    bytes."""
    assert SESSION_LINE_BYTES_MAX > 0, "the line bound is positive"
    try:
        with path.open("rb") as handle:
            head = handle.readline(SESSION_LINE_BYTES_MAX + 1)
        line = head.decode("utf-8")
    except OSError as err:
        return f"cannot read {path.name}: {err.strerror}"
    except ValueError:
        return f"{path.name} is not UTF-8"
    return decode_session_line(line)


def needs_rebuild(out_dir: Path, ref: SessionRef, force: bool) -> bool:
    """True when force, or the events file is missing or its session line is invalid, or the raw
    files or the code versions differ from what was stored. Only the session line is read: a
    damaged body is found when `report` loads the file."""
    assert ref.skip_reason == "", "only readable sessions are rebuilt"
    if force:
        return True
    path = events_path(out_dir, ref.harness, ref.session_id)
    if not path.is_file():
        return True
    rec = load_session_line(path)
    if isinstance(rec, str):
        return True
    return (
        rec.source_bytes != ref.source_bytes
        or rec.source_mtime_ms != ref.source_mtime_ms
        or rec.schema_version != SCHEMA_VERSION
        or rec.reader_version != READER_VERSION
        or rec.rule_version != RULE_VERSION
    )


def list_stored(out_dir: Path, harness: str, sessions_max: int = SESSIONS_MAX) -> list[Path] | str:
    """Events files of one harness, sorted, or the reason the directory is over a bound
    (`sessions_max` files, twice that many entries). Names that are not a uuid are not events
    files."""
    assert harness in HARNESSES, "known harness"
    assert 0 < sessions_max <= SESSIONS_MAX, "the effective bound never exceeds the hard one"
    directory = out_dir / harness
    if not directory.is_dir():
        return []
    entries_max = 2 * sessions_max
    found: list[Path] = []
    for seen, entry in enumerate(directory.iterdir(), start=1):
        if seen > entries_max:
            return f"{harness} store has more than {entries_max} entries"
        if entry.suffix == SUFFIX and entry.is_file() and SESSION_ID_RE.fullmatch(entry.stem):
            found.append(entry)
        if len(found) > sessions_max:
            return f"{harness} store has more than {sessions_max} events files"
    return sorted(found)


def count_without_raw(stored: Sequence[Path], refs: Sequence[SessionRef]) -> int:
    """Events files whose session is no longer in the raw listing (skipped sessions count as
    listed: their raw log still exists)."""
    listed = {ref.session_id for ref in refs}
    count = sum(1 for path in stored if path.stem not in listed)
    assert 0 <= count <= len(stored), "a subset of the stored files"
    return count


# ---------------------------------------------------------------- build state: skips, coverage


def _read_state(path: Path) -> str | None:
    """Text of a small state file, or None when it is missing, over its bound or unreadable."""
    try:
        with path.open("rb") as handle:
            data = handle.read(STATE_FILE_BYTES_MAX + 1)
        return data.decode("utf-8") if len(data) <= STATE_FILE_BYTES_MAX else None
    except (OSError, ValueError):
        return None


def write_skips(state_dir: Path, skips: Sequence[Skip]) -> None:
    """out/skips.jsonl: one `{harness, reason, session_id}` line per skipped session, sorted."""
    assert all(s.reason in SKIP_REASONS and s.harness in HARNESSES for s in skips), "closed enums"
    rows = sorted((s.harness, s.reason, s.session_id) for s in skips)
    lines = [
        json.dumps(
            {"harness": h, "reason": r, "session_id": i}, sort_keys=True, separators=(",", ":")
        )
        for h, r, i in rows
    ]
    _atomic_write(state_dir / SKIPS_NAME, "".join(f"{line}\n" for line in lines))


def load_skips(state_dir: Path) -> list[Skip] | str:
    text = _read_state(state_dir / SKIPS_NAME)
    if text is None:
        return f"cannot read {SKIPS_NAME}"
    found: list[Skip] = []
    for line in text.splitlines()[:STORE_ENTRIES_MAX]:
        try:
            raw = as_dict(json.loads(line))
        except ValueError:
            return f"{SKIPS_NAME} has a line that is not JSON"
        harness, reason, session_id = (
            as_str(raw.get(k)) for k in ("harness", "reason", "session_id")
        )
        if set(raw) != {"harness", "reason", "session_id"} or harness not in HARNESSES:
            return f"{SKIPS_NAME} has a line with the wrong keys"
        if reason not in SKIP_REASONS or clean_name(session_id) != session_id:
            return f"{SKIPS_NAME} has a line with a bad reason or id"
        found.append(Skip(session_id, harness, reason))
    return found


def write_coverage(state_dir: Path, cov: Coverage) -> None:
    problem = coverage_problem(cov)
    if problem is not None:
        raise ValueError(f"invalid coverage: {problem}")
    body: dict[str, object] = {name: getattr(cov, name) for name in COUNT_FIELDS}
    body["skipped"] = dict(cov.skipped)
    text = json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n"
    _atomic_write(state_dir / COVERAGE_NAME, text)


def load_coverage(state_dir: Path) -> Coverage | str:
    text = _read_state(state_dir / COVERAGE_NAME)
    if text is None:
        return f"cannot read {COVERAGE_NAME}"
    try:
        raw = as_dict(json.loads(text))
    except ValueError:
        return f"{COVERAGE_NAME} is not JSON"
    skipped = as_dict(raw.get("skipped"))
    if set(raw) != {*COUNT_FIELDS, "skipped"} or set(skipped) != set(SKIP_REASONS):
        return f"{COVERAGE_NAME} has the wrong keys"
    counts = {name: raw[name] for name in COUNT_FIELDS}
    try:
        cov = Coverage(
            skipped=tuple((reason, cast(int, skipped[reason])) for reason in SKIP_REASONS),
            **cast(dict[str, int], counts),
        )
    except TypeError:
        return f"{COVERAGE_NAME} does not fit the coverage record"
    problem = coverage_problem(cov)
    return cov if problem is None else f"{COVERAGE_NAME}: {problem}"


# ---------------------------------------------------------------- backup


def backup_events(out_dir: Path, backup_dir: Path, sessions_max: int = SESSIONS_MAX) -> int | str:
    """Copy each events file whose backup is missing or differs in size or mtime. Never deletes.
    Returns the number copied, or the reason a store directory is over a bound. OSError
    propagates (the caller names the file)."""
    assert out_dir.resolve() != backup_dir.resolve(), "the backup is a different directory"
    copied = 0
    for harness in HARNESSES:
        stored = list_stored(out_dir, harness, sessions_max)
        if isinstance(stored, str):
            return stored
        for source in stored:
            target = backup_dir / harness / source.name
            have, want = (target.stat() if target.is_file() else None), source.stat()
            if have is not None and (have.st_size, have.st_mtime_ns) == (
                want.st_size,
                want.st_mtime_ns,
            ):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            handle, temp_name = tempfile.mkstemp(dir=target.parent, prefix=".", suffix=TEMP_SUFFIX)
            os.close(handle)
            try:
                shutil.copy2(source, temp_name)
                os.replace(temp_name, target)
            except BaseException:
                Path(temp_name).unlink(missing_ok=True)
                raise
            copied += 1
    assert copied >= 0, "a count"
    return copied
