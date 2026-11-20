"""Append-only writer for the LowLog: one JSON line per decision the agent made alone at Low.

Agents call it and never read the
log; the user reads it when they choose and deletes it to clear it. No read command on purpose.
Exit 0 written, 1 git or file system failure (one stderr line, no traceback), 2 bad arguments,
3 log full. Standard library only; runs on python 3.9.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

MEGABYTE = 1024 * 1024
LOG_BYTES_MAX = MEGABYTE  # ~2,000 entries; past it the user deletes the file
FIELD_CHARS_MAX = 2000
GIT_TIMEOUT_S = 5
TEXT_FIELDS = ("session", "ask-point", "options", "choice", "reason")
YES_NO_FIELDS = ("spec-edited", "product-facing")
USAGE = ("usage: lowlog.py append --repo PATH --session ID --ask-point TEXT --options TEXT "
         "--choice TEXT --reason TEXT --spec-edited yes|no --product-facing yes|no")


class BadArgs(Exception):
    pass


def parse(argv: list[str]) -> dict[str, str]:
    if not argv or argv[0] != "append":
        raise BadArgs("first argument must be 'append'")
    rest = argv[1:]
    if len(rest) % 2:
        raise BadArgs("every flag needs a value")
    values: dict[str, str] = {}
    allowed = ("repo",) + TEXT_FIELDS + YES_NO_FIELDS
    for index in range(0, len(rest), 2):  # pairs; odd length refused above
        flag, value = rest[index], rest[index + 1]
        name = flag[2:] if flag.startswith("--") else ""
        if name not in allowed:
            raise BadArgs(f"unknown flag {flag}")
        if name in values:
            raise BadArgs(f"{flag} given twice")
        if not value.strip():
            raise BadArgs(f"{flag} is empty")
        if len(value) > FIELD_CHARS_MAX:
            raise BadArgs(f"{flag} is over {FIELD_CHARS_MAX} characters")
        values[name] = value
    missing = [name for name in allowed if name not in values]
    if missing:
        raise BadArgs("missing --" + ", --".join(missing))
    for name in YES_NO_FIELDS:
        if values[name] not in ("yes", "no"):
            raise BadArgs(f"--{name} must be yes or no")
    return values


def top_level(repo: str) -> Path:
    path = Path(repo).resolve()
    done = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True, timeout=GIT_TIMEOUT_S, check=False)
    if done.returncode != 0 or Path(done.stdout.strip()).resolve() != path:
        raise BadArgs(f"--repo {repo} is not a git top level")
    return path


def log_path(repo: Path) -> Path:
    digest = hashlib.sha256(str(repo).encode("utf-8")).hexdigest()[:8]
    return Path.home() / ".claude" / "lowlog" / f"{repo.name}-{digest}.jsonl"


def entry(values: dict[str, str], repo: Path) -> str:
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    record: dict[str, object] = {"ts": now, "repo": str(repo)}
    for name in TEXT_FIELDS:
        record[name.replace("-", "_")] = values[name]
    for name in YES_NO_FIELDS:
        record[name.replace("-", "_")] = values[name] == "yes"
    line = json.dumps(record, ensure_ascii=False) + "\n"
    assert line.count("\n") == 1, "one entry is one line"
    return line


def append(path: Path, line: str) -> str | None:
    """None when written; the refusal text when the log is full."""
    if path.exists() and path.stat().st_size > LOG_BYTES_MAX:
        return f"LowLog over 1 MiB; delete it to clear: {path}"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = line.encode("utf-8")
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        written = os.write(fd, data)  # one write with O_APPEND: parallel sessions don't interleave
    finally:
        os.close(fd)
    assert written == len(data), (written, len(data))
    return None


def main(argv: list[str]) -> int:
    try:
        values = parse(argv)
        repo = top_level(values["repo"])
    except BadArgs as error:
        print(f"lowlog: {error}\n{USAGE}", file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired:
        print(f"lowlog: git rev-parse timed out after {GIT_TIMEOUT_S}s", file=sys.stderr)
        return 1
    except OSError as error:  # git not installed, or the repo path can't be read
        print(f"lowlog: could not run git: {error}", file=sys.stderr)
        return 1
    try:
        full = append(log_path(repo), entry(values, repo))
    except OSError as error:
        print(f"lowlog: could not write the log: {error}", file=sys.stderr)
        return 1
    if full is not None:
        print(f"lowlog: {full}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
