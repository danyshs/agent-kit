"""Small helpers shared by readers, store and report. Four are copies of evals/lib/miner.py
(args_hash, exit_code_from_tail, read_events, is_eval_traffic); tests/test_common.py pins them."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast

from traj.limits import CWDS_PER_SESSION_MAX, EXIT_TAIL_CHARS, NAME_CHARS_MAX, OTHER

# case-preserving on purpose: Claude tool names are Bash, Read, WebFetch
NAME_RE: Final = re.compile(rf"^[A-Za-z0-9_][A-Za-z0-9_.:-]{{0,{NAME_CHARS_MAX - 1}}}$")
SESSION_ID_RE: Final = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
EXIT_TAIL_RE: Final = re.compile(r"Exit Code: (\d{1,5})\s*$")
ISO_MS_RE: Final = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?Z")
HEX12_RE: Final = re.compile(r"[0-9a-f]{12}")
ISO_CHARS_MAX: Final = 40  # longer than any real timestamp; skip the regex on junk


def as_dict(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def as_list(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else []


def as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def clean_name(raw: str) -> str:
    """A name that is safe to print: matches NAME_RE, else (other)."""
    result = raw if NAME_RE.fullmatch(raw) else OTHER
    assert result == OTHER or len(result) <= NAME_CHARS_MAX, "a clean name is short"
    return result


def args_hash(args: dict[str, object]) -> str:
    text = json.dumps(args, sort_keys=True, default=str)
    assert text.startswith("{"), "a dict dumps as an object"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    assert HEX12_RE.fullmatch(digest) is not None, "12 hex"
    return digest


def short_hash(text: str) -> str:
    """12 hex of sha256 over the text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def exit_code_from_tail(content: str) -> int | None:
    """The trailing `Exit Code: N` of a shell result. Only that number is read."""
    assert EXIT_TAIL_CHARS > 0, "the tail bound is positive"
    match = EXIT_TAIL_RE.search(content[-EXIT_TAIL_CHARS:])
    code = int(match.group(1)) if match else None
    assert code is None or 0 <= code <= 99_999, "at most five digits"
    return code


def parse_iso_ms(text: str) -> int | None:
    """UTC epoch milliseconds from `YYYY-MM-DDTHH:MM:SS[.fff]Z`; None on any other input."""
    match = ISO_MS_RE.fullmatch(text) if len(text) <= ISO_CHARS_MAX else None
    if match is None:
        return None
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    fraction = (match.group(7) or "0").ljust(3, "0")[:3]
    try:
        moment = datetime(year, month, day, hour, minute, second, tzinfo=UTC)
    except ValueError:
        return None
    millis = int(moment.timestamp()) * 1000 + int(fraction)
    return millis


def read_events(
    path: Path, lines_max: int, bytes_max: int
) -> tuple[list[dict[str, object]], int] | str:
    """(events, parse-error count), or the reason the file is over a bound. Never partial."""
    assert lines_max > 0 and bytes_max > 0, "bounds are positive"
    try:
        size = path.stat().st_size
    except OSError as err:
        return f"cannot stat {path.name}: {err.strerror}"
    if size > bytes_max:
        return f"{path.name} is {size} bytes, over the {bytes_max} byte bound"
    events: list[dict[str, object]] = []
    parse_errors = 0
    try:
        with path.open("rb") as raw:
            data = raw.read(bytes_max + 1)  # a file that grew after the stat still stops here
        if len(data) > bytes_max:
            return f"{path.name} grew past the {bytes_max} byte bound while being read"
        with io.TextIOWrapper(io.BytesIO(data), encoding="utf-8", errors="replace") as handle:
            for count, line in enumerate(handle, start=1):
                if count > lines_max:
                    return f"{path.name} has over {lines_max} lines"
                if not line.strip():
                    continue
                try:
                    value: object = json.loads(line)
                except ValueError:
                    parse_errors += 1
                    continue
                if isinstance(value, dict):
                    events.append(cast(dict[str, object], value))
                else:
                    parse_errors += 1
    except OSError as err:
        return f"cannot read {path.name}: {err.strerror}"
    assert parse_errors >= 0 and len(events) <= lines_max, "counts are consistent"
    return events, parse_errors


def is_eval_traffic(cwds: Sequence[object], eval_roots: Sequence[Path]) -> bool:
    """True when any cwd resolves to or under an eval root (macOS /tmp is /private/tmp).
    Non-strings and empty strings are ignored; only the first CWDS_PER_SESSION_MAX are read."""
    assert all(root.is_absolute() for root in eval_roots), "roots are absolute"
    assert CWDS_PER_SESSION_MAX > 0, "the cwd bound is positive"
    for item in cwds[:CWDS_PER_SESSION_MAX]:
        cwd = as_str(item)
        if not cwd:
            continue
        real = Path(os.path.realpath(cwd))
        if any(real == root or real.is_relative_to(root) for root in eval_roots):
            return True
    return False
