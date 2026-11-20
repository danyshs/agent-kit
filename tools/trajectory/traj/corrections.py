"""v2 step 1: the weekly correction list. Spec: docs/designs/trajectory-analysis/v2/design_draft.md

Reads top-level Claude Code transcripts directly and quotes the user. Quotes go only into the one
file the `corrections` command writes, never into v1's events, backup or reports.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

from traj.common import as_dict, as_list, as_str
from traj.limits import (
    CORRECTION_ENTRIES_MAX,
    CORRECTION_FILES_MAX,
    CORRECTION_QUOTE_CHARS_MAX,
    CORRECTION_TEXT_CHARS_MAX,
    FILE_BYTES_MAX,
    LINES_PER_FILE_MAX,
)

Kind = Literal["stop", "tool_use", "correction"]
KINDS: Final[tuple[Kind, ...]] = ("stop", "tool_use", "correction")
MARKERS: Final[dict[str, Kind]] = {
    "[Request interrupted by user]": "stop",
    "[Request interrupted by user for tool use]": "tool_use",
}
REJECTION: Final = "doesn't want to proceed"
FEEDBACK: Final = "the user said:"
NO_FOLLOW_UP: Final = "(no follow-up)"
# "no," needs its own form: \b after the comma would demand a word character there
CORRECTION: Final = re.compile(
    r"\bno[,.!]|\b(wrong|garbage|don't|do not|stop|why did|you didn't|not what|that's not"
    r"|undo|revert)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Entry:
    when: datetime  # UTC, from the record
    project: str  # project folder name as-is
    path: Path
    line: int  # 1-based physical line of the quoted message
    kind: Kind
    quote: str  # raw; render cleans and cuts it


@dataclass
class Coverage:
    files: int = 0
    cut: int = 0
    duplicates: int = 0
    oldest: datetime | None = None
    truncated: int = 0


@dataclass
class _Pending:
    when: datetime
    line: int
    kind: Kind
    duplicate: bool


@dataclass
class _FileState:
    entries: list[Entry] = field(default_factory=lambda: list[Entry]())
    pending: list[_Pending] = field(default_factory=lambda: list[_Pending]())


def _block_texts(blocks: list[object]) -> str:
    texts = [as_str(as_dict(b).get("text")) for b in blocks if as_dict(b).get("type") == "text"]
    return " ".join(t for t in texts if t)


def typed_text(record: dict[str, object]) -> str:
    """The text the user typed, or "" for meta, compaction summaries, tool results, non-humans."""
    if record.get("type") != "user" or record.get("isMeta") or record.get("isCompactSummary"):
        return ""
    origin = as_dict(record.get("origin"))
    if origin and origin.get("kind") != "human":
        return ""  # task notifications, peer agents; older lines have no origin at all
    content = as_dict(record.get("message")).get("content")
    if isinstance(content, str):
        return content
    return _block_texts(as_list(content))


def rejection_feedback(record: dict[str, object]) -> str:
    """What the user typed when rejecting a tool call, or "" (it sits in a tool_result block)."""
    if record.get("type") != "user":
        return ""
    for block in as_list(as_dict(record.get("message")).get("content")):
        result = as_dict(block)
        if result.get("type") != "tool_result":
            continue
        inner = result.get("content")
        text = inner if isinstance(inner, str) else _block_texts(as_list(inner))
        if REJECTION not in text:
            continue
        at = text.lower().find(FEEDBACK)
        feedback = text[at + len(FEEDBACK) :].strip() if at >= 0 else ""
        if feedback:
            return feedback
    return ""


def classify(text: str) -> Kind | None:
    """Exact interrupt markers first; then the correction-like candidate rule."""
    assert text, "only typed text is classified"
    marker = MARKERS.get(text.strip())
    if marker is not None:
        return marker
    if len(text) > CORRECTION_TEXT_CHARS_MAX or text.startswith("<"):
        return None
    return "correction" if CORRECTION.search(text) else None


def _stamp(record: dict[str, object]) -> datetime | None:
    stamp = record.get("timestamp")
    if not isinstance(stamp, str):
        return None
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo is not None else None


def _flush(state: _FileState, project: str, path: Path, quote: str, line: int | None) -> None:
    """Pending interrupts take their follow-up (`quote` at `line`) and become entries, unless
    duplicates. With no follow-up (`line` None) the entry points at the interrupt itself."""
    assert (line is None) == (quote == NO_FOLLOW_UP), (line, quote)
    for p in state.pending:
        assert line is None or p.line < line, (p.line, line)
        if not p.duplicate:
            at = p.line if line is None else line
            state.entries.append(Entry(p.when, project, path, at, p.kind, quote))
    state.pending.clear()


def read_file(path: Path, seen: set[str], coverage: Coverage) -> list[Entry]:
    """One transcript: entries in file order. Pairing runs here, before dedupe drops copies."""
    project = path.parent.name
    if path.stat().st_size > FILE_BYTES_MAX:
        coverage.cut += 1
        return []
    state = _FileState()
    with path.open(encoding="utf-8", errors="replace") as handle:
        for number, raw in enumerate(handle, start=1):
            if number > LINES_PER_FILE_MAX:
                coverage.cut += 1
                break
            _read_line(raw, number, project, path, seen, coverage, state)
    _flush(state, project, path, NO_FOLLOW_UP, None)
    assert not state.pending
    return state.entries


def _read_line(
    raw: str,
    number: int,
    project: str,
    path: Path,
    seen: set[str],
    coverage: Coverage,
    state: _FileState,
) -> None:
    try:
        record = as_dict(json.loads(raw))
    except json.JSONDecodeError:
        return  # a half-written last line
    when = _stamp(record)
    if when is None:
        return  # only metadata records lack a timestamp
    uuid = record.get("uuid")
    duplicate = isinstance(uuid, str) and uuid in seen
    if isinstance(uuid, str) and record.get("type") == "user":
        coverage.duplicates += duplicate
        seen.add(uuid)
    feedback = rejection_feedback(record)
    if feedback and not duplicate:
        state.entries.append(Entry(when, project, path, number, "tool_use", feedback))
    text = typed_text(record)
    if not text.strip():
        return
    if coverage.oldest is None or when < coverage.oldest:
        coverage.oldest = when
    kind = classify(text)
    if text.strip() in MARKERS:
        assert kind in ("stop", "tool_use"), kind
        state.pending.append(_Pending(when, number, kind, duplicate))
    elif state.pending:
        _flush(state, project, path, text, number)  # a follow-up is not listed again
    elif kind == "correction" and not duplicate:
        state.entries.append(Entry(when, project, path, number, kind, text))


def scan(projects: Path, since: datetime, until: datetime) -> tuple[list[Entry], Coverage] | str:
    """All entries in [since, until], newest first, cut at CORRECTION_ENTRIES_MAX; or an error."""
    assert since.tzinfo is not None and until.tzinfo is not None, "aware datetimes only"
    if not projects.is_dir():
        return f"projects folder not found: {projects}"
    files = sorted(projects.glob("*/*.jsonl"))  # subagent files sit deeper and are not read
    if len(files) > CORRECTION_FILES_MAX:
        return f"{len(files)} transcripts, over the cap of {CORRECTION_FILES_MAX}"
    coverage = Coverage(files=len(files))
    seen: set[str] = set()
    entries: list[Entry] = []
    for path in files:
        entries.extend(e for e in read_file(path, seen, coverage) if since <= e.when <= until)
    entries.sort(key=lambda e: (-e.when.timestamp(), str(e.path), e.line))
    coverage.truncated = max(0, len(entries) - CORRECTION_ENTRIES_MAX)
    kept = entries[:CORRECTION_ENTRIES_MAX]
    assert len(kept) + coverage.truncated == len(entries)
    return kept, coverage


def clean_quote(text: str) -> str:
    """One line, no backticks, at most CORRECTION_QUOTE_CHARS_MAX characters."""
    folded = " ".join(text.split()).replace("`", "'")
    cut = folded[:CORRECTION_QUOTE_CHARS_MAX]
    assert len(cut) <= CORRECTION_QUOTE_CHARS_MAX and "\n" not in cut
    return cut


def _local(when: datetime) -> str:
    return when.astimezone().strftime("%Y-%m-%d %H:%M %z")


def render(entries: list[Entry], coverage: Coverage, since: datetime, until: datetime) -> str:
    """The Markdown list; the coverage line is always last."""
    assert all(since <= e.when <= until for e in entries)
    local_since, local_until = since.astimezone(), until.astimezone()
    lines = [
        f"# Corrections, {local_since:%Y-%m-%d %H:%M} to {local_until:%Y-%m-%d %H:%M} "
        f"({local_until:%z})",
    ]
    for kind in KINDS:
        section = [e for e in entries if e.kind == kind]
        lines += ["", f"## {kind} ({len(section)})"]
        if not section:
            lines.append("(none)")
        for e in section:
            lines.append(f"- {_local(e.when)} · {e.project} · `{e.path}:{e.line}`")
            lines.append(f"  > {clean_quote(e.quote)}")
    lines.append("")
    if coverage.truncated:
        lines.append(f"(truncated: {coverage.truncated} older entries)")
    oldest = f"{coverage.oldest.astimezone():%Y-%m-%d}" if coverage.oldest else "none"
    lines.append(
        f"coverage: {coverage.files} files read, {coverage.cut} cut, "
        f"{coverage.duplicates} duplicate messages dropped; oldest message {oldest}"
    )
    return "\n".join(lines) + "\n"


def counts_line(entries: list[Entry]) -> str:
    """Counts only: weekly puts this in an AppleScript string and the launchd log."""
    counts = {kind: sum(1 for e in entries if e.kind == kind) for kind in KINDS}
    line = "traj corrections: " + ", ".join(f"{counts[k]} {k}" for k in KINDS)
    assert sum(counts.values()) == len(entries)
    return line
