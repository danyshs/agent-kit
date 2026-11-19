"""UserPromptSubmit hook: the attention level (Low, Medium, High) for this session.

Only the prompt the user just
typed is read, never the transcript. The level lives in ~/.claude/attention/<session_id>, one
"<UTC time> <LEVEL> <user|default|error>" line per change; the last line is current.
Always exits 0: a broken hook must not block a prompt, so it falls back to Medium and says so.
Standard library only; runs on python 3.9.
"""

from __future__ import annotations

import datetime
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple, cast

MEGABYTE = 1024 * 1024
STDIN_BYTES_MAX = 16 * MEGABYTE  # same bound as guard.py
LEVEL_FILE_LINES_MAX = 100  # a level changes a few times per session at most
TRIGGERS: Dict[str, str] = {
    "(ENABLE_LOW_ENERGY)": "LOW",
    "(ENABLE_MEDIUM_ENERGY)": "MEDIUM",
    "(ENABLE_HIGH_ENERGY)": "HIGH",
}
LEVELS = ("LOW", "MEDIUM", "HIGH", "?")
SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
FENCE_PATTERN = re.compile(r"^\s*(```|~~~)")
INLINE_CODE_PATTERN = re.compile(r"`[^`\n]*`")
TRIGGER_PATTERN = re.compile("|".join(re.escape(t) for t in TRIGGERS))
SOURCES = ("user", "default", "error")
NAMES = {"LOW": "Low", "MEDIUM": "Medium", "HIGH": "High"}

assert len(TRIGGERS) == 3 and len(set(TRIGGERS.values())) == 3
assert all(level in LEVELS for level in TRIGGERS.values())


def attention_dir() -> Path:
    return Path.home() / ".claude" / "attention"


def strip_code(text: str) -> str:
    """The text with fenced blocks and inline code removed, so a quoted trigger doesn't count."""
    kept: List[str] = []
    fenced = False
    for line in text.split("\n"):
        if FENCE_PATTERN.match(line):
            fenced = not fenced  # an unclosed fence runs to the end
            continue
        if not fenced:
            kept.append(INLINE_CODE_PATTERN.sub("", line))
    return "\n".join(kept)


def trigger_in(prompt: str) -> Optional[str]:
    """The level of the last trigger outside code, or None."""
    found = TRIGGER_PATTERN.findall(strip_code(prompt))
    if not found:
        return None
    return TRIGGERS[found[-1]]


def read_lines(level_file: Path) -> List[str]:
    if not level_file.exists():
        return []
    text = level_file.read_text(encoding="utf-8")
    return [line for line in text.split("\n") if line.strip()]


def parse_line(line: str) -> Tuple[str, str]:
    parts = line.split()
    if len(parts) != 3 or parts[1] not in LEVELS or parts[2] not in SOURCES:
        raise ValueError(f"bad level file line: {line!r}")
    return parts[1], parts[2]


def last_real(lines: List[str]) -> Optional[Tuple[str, str]]:
    """The newest (level, source) that isn't an error mark, or None for a new session."""
    for line in reversed(lines):
        level, source = parse_line(line)
        if level != "?":
            return level, source
    return None


def append_level(level_file: Path, lines: List[str], level: str, source: str) -> None:
    assert level in LEVELS and source in SOURCES, (level, source)
    assert (level == "?") == (source == "error"), (level, source)
    if len(lines) >= LEVEL_FILE_LINES_MAX:
        raise ValueError(f"level file has {len(lines)} lines, cap {LEVEL_FILE_LINES_MAX}")
    level_file.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with level_file.open("a", encoding="utf-8") as handle:
        handle.write(f"{now} {level} {source}\n")


def decide(hook_input: Dict[str, object]) -> str:
    """Updates the level file and returns the line for the agent's context."""
    session = hook_input.get("session_id")
    if not isinstance(session, str) or not SESSION_ID_PATTERN.fullmatch(session):
        raise ValueError(f"bad session_id: {session!r}")
    prompt = hook_input.get("prompt")
    if not isinstance(prompt, str):
        raise ValueError("hook input has no prompt")
    level_file = attention_dir() / session
    lines = read_lines(level_file)
    newest = parse_line(lines[-1])[0] if lines else None
    typed = trigger_in(prompt)
    if typed is not None:
        level, source = typed, "user"
    else:
        real = last_real(lines)
        level, source = real if real is not None else ("MEDIUM", "default")
    if newest != level:  # also rewrites the level after an error mark
        append_level(level_file, lines, level, source)
    how = "set by the user" if source == "user" else "default, no trigger typed"
    return f"Attention level: {NAMES[level]} ({how}, session {session})"


def mark_error(raw: bytes) -> None:
    """Best effort: mark the level file so the next prompt reports the error."""
    try:
        decoded: object = json.loads(raw.decode("utf-8"))
        if not isinstance(decoded, dict):
            return
        session = cast(Dict[str, object], decoded).get("session_id")
        if isinstance(session, str) and SESSION_ID_PATTERN.fullmatch(session):
            level_file = attention_dir() / session
            append_level(level_file, read_lines(level_file), "?", "error")
    except Exception as error:  # noqa: BLE001 - already failing; stderr is all that's left
        print(f"attention: could not mark the error: {error}", file=sys.stderr)


def main() -> int:
    raw = sys.stdin.buffer.read(STDIN_BYTES_MAX + 1)
    try:
        if len(raw) > STDIN_BYTES_MAX:
            raise ValueError("hook input over 16 MB")
        decoded: object = json.loads(raw.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise ValueError("hook input is not a JSON object")
        print(decide(cast(Dict[str, object], decoded)))
    except Exception as error:  # noqa: BLE001 - any failure falls back to Medium, loudly
        print(f"attention: {error}", file=sys.stderr)
        print("Attention level: Medium (hook error)")
        mark_error(raw)
    return 0


if __name__ == "__main__":
    sys.exit(main())
