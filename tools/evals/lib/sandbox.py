"""Throwaway sandbox for running model-written code (design 3.6, step 5).

The model never executes anything (release 1 trusts read_file only); this module runs code the
model *attempted* to write, after the fact, as the user. Isolation is macOS sandbox-exec plus a
scrubbed env, rlimits, a wall timeout and a process-group kill. It fails closed: a missing
sandbox, or a canary that does not see network and write attempts denied, means nothing runs.
What it does not cover is listed in design 3.6; do not claim more than that list.
"""

from __future__ import annotations

import os
import resource
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

SANDBOX_EXEC: Final = "/usr/bin/sandbox-exec"
# secrets the sandboxed code may not read; every other read under $HOME is NOT blocked (3.6)
DENY_READ_UNDER_HOME: Final = (".ssh", ".aws", ".gnupg", ".kiro", ".claude", ".netrc", ".config/gh")
ENV_PATH: Final = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
CPU_S_MAX: Final = 30  # RLIMIT_CPU; the wall timeout is the other backstop
FILE_BYTES_MAX: Final = 10 * 1024 * 1024  # RLIMIT_FSIZE: largest file a process may write
OPEN_FILES_MAX: Final = 256
OUTPUT_BYTES_MAX: Final = 65_536  # kept stdout and stderr, each
KILL_WAIT_S: Final = 5.0
CANARY_TIMEOUT_S: Final = 20.0
CANARY_PORT: Final = 9  # discard port; nothing listens, so only the sandbox can make it fail

STATUS_OK: Final = "ok"
STATUS_TIMEOUT: Final = "timeout"
CANARY_OK: Final = "NET_DENIED WRITE_DENIED"
CANARY_SOURCE: Final = (
    "import os, socket, sys\n"
    "out = []\n"
    "try:\n"
    "    socket.create_connection(('127.0.0.1', __PORT__), timeout=2)\n"
    "except PermissionError:\n"
    "    out.append('NET_DENIED')\n"
    "except OSError:\n"
    "    out.append('NET_OTHER')\n"
    "try:\n"
    "    open(os.path.join(sys.argv[1], 'canary.txt'), 'w').close()\n"
    "except PermissionError:\n"
    "    out.append('WRITE_DENIED')\n"
    "print(' '.join(out))\n"
).replace("__PORT__", str(CANARY_PORT))


@dataclass(frozen=True)
class SandboxResult:
    status: str  # ok | timeout
    returncode: int | None
    stdout_text: str
    stderr_text: str


def _quote(path: Path) -> str:
    """A path as an SBPL string literal; paths with a quote or backslash are refused."""
    text = str(path)
    assert text.startswith("/"), "profile paths are absolute"
    assert '"' not in text and "\\" not in text, "no characters that need escaping in a profile"
    return f'"{text}"'


def build_profile(scratch: Path, home: Path) -> str:
    """SBPL: allow by default, no network, writes only under scratch, secret dirs unreadable.
    The last matching rule wins, so the allows sit after the denies."""
    assert scratch.is_absolute() and home.is_absolute(), "paths are absolute"
    lines = ["(version 1)", "(allow default)", "(deny network*)", "(deny file-write*)"]
    lines.append(f'(allow file-write* (subpath {_quote(scratch)}) (literal "/dev/null"))')
    for name in DENY_READ_UNDER_HOME:
        lines.append(f"(deny file-read* (subpath {_quote(home / name)}))")
    assert any(line.startswith("(deny network") for line in lines), "the network is denied"
    return "\n".join(lines) + "\n"


def _limit_resources() -> None:
    """Runs in the child before exec. Memory is not capped: macOS ignores RLIMIT_AS/DATA."""
    assert CPU_S_MAX > 0 and FILE_BYTES_MAX > 0, "limits are positive"
    assert OPEN_FILES_MAX >= 64, "enough descriptors for python and pytest"
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_S_MAX, CPU_S_MAX))
    resource.setrlimit(resource.RLIMIT_FSIZE, (FILE_BYTES_MAX, FILE_BYTES_MAX))
    resource.setrlimit(resource.RLIMIT_NOFILE, (OPEN_FILES_MAX, OPEN_FILES_MAX))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def scrubbed_env(scratch: Path) -> dict[str, str]:
    """No token, no inherited variable: only what an interpreter needs."""
    assert scratch.is_absolute(), "scratch is absolute"
    tmp = scratch / "tmp"
    tmp.mkdir(exist_ok=True)
    env = {
        "PATH": ENV_PATH,
        "HOME": str(scratch),
        "TMPDIR": str(tmp),
        "LANG": "en_US.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
    assert "KIRO_HOME" not in env and not any("TOKEN" in key for key in env), "no secrets"
    return env


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    assert proc.pid > 0, "a started process"
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # start_new_session made pid the group id
    except (ProcessLookupError, PermissionError):
        pass  # the group is already gone


def run_sandboxed(argv: list[str], scratch: Path, timeout_s: float) -> SandboxResult:
    """Run argv with cwd = scratch under the profile. Never raises on the child's behavior;
    a missing sandbox-exec raises FileNotFoundError, which callers prevent with check_sandbox."""
    assert argv and timeout_s > 0, "a command and a positive timeout"
    assert scratch.is_absolute() and scratch.is_dir(), "scratch is an existing absolute dir"
    real = scratch.resolve()
    profile = build_profile(real, Path.home().resolve())
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(
            [SANDBOX_EXEC, "-p", profile, *argv],
            cwd=real,
            env=scrubbed_env(real),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            start_new_session=True,
            preexec_fn=_limit_resources,  # single-threaded here, so the fork hazard does not apply
        )
        status = STATUS_OK
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            status = STATUS_TIMEOUT
        finally:
            _kill_group(proc)  # children that outlive the main process die too
        proc.wait(timeout=KILL_WAIT_S)
        out.seek(0)
        err.seek(0)
        result = SandboxResult(
            status,
            None if status == STATUS_TIMEOUT else proc.returncode,
            out.read(OUTPUT_BYTES_MAX).decode("utf-8", "replace"),
            err.read(OUTPUT_BYTES_MAX).decode("utf-8", "replace"),
        )
    assert len(result.stdout_text) <= OUTPUT_BYTES_MAX, "kept output is bounded"
    return result


def check_sandbox() -> str | None:
    """None when the sandbox is proven to deny a network connect and a write outside scratch;
    else why not. Runs a known-bad input through the real profile every time (fails closed)."""
    assert CANARY_SOURCE and CANARY_TIMEOUT_S > 0, "the canary program and its timeout are defined"
    assert sys.executable, "an interpreter to run the canary with"
    if sys.platform != "darwin" or not os.path.exists(SANDBOX_EXEC):
        return "sandbox-exec is not available: refusing to run model-written code"
    with tempfile.TemporaryDirectory(prefix="evsbx-canary-") as parent:
        scratch = Path(parent).resolve() / "inside"
        outside = Path(parent).resolve() / "outside"
        scratch.mkdir()
        outside.mkdir()
        try:
            result = run_sandboxed(
                [sys.executable, "-c", CANARY_SOURCE, str(outside)], scratch, CANARY_TIMEOUT_S
            )
        except OSError as err:
            return f"sandbox canary could not start: {err}"
        leaked = (outside / "canary.txt").exists()
    if result.status != STATUS_OK or result.stdout_text.strip() != CANARY_OK or leaked:
        return f"sandbox canary did not see the attempts denied: {result.stdout_text.strip()!r}"
    return None
