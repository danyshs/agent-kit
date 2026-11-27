"""Throwaway sandbox for running model-written code (design 3.6, step 5).

The model never executes anything (release 1 trusts read_file only); this module runs code the
model *attempted* to write, after the fact, as the user. Isolation is sandbox-exec on macOS or
bwrap on Linux, plus a scrubbed env, rlimits, a wall timeout and a process-group kill. It fails
closed: a missing sandbox, or a canary that does not see network and write attempts denied, means
nothing runs. What it does not cover is listed in design 3.6; do not claim more than that list.

bwrap needs unprivileged user namespaces. Ubuntu 23.10 and later block them through AppArmor unless
`kernel.apparmor_restrict_unprivileged_userns=0`; the canary then fails and says why.
"""

from __future__ import annotations

import os
import resource
import signal
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

SANDBOX_EXEC: Final = "/usr/bin/sandbox-exec"
BWRAP: Final = "/usr/bin/bwrap"  # a fixed path, not a PATH lookup, like sandbox-exec
BACKEND_SANDBOX_EXEC: Final = "sandbox-exec"
BACKEND_BWRAP: Final = "bwrap"
NO_SANDBOX: Final = (
    "no sandbox on this machine (sandbox-exec on macOS, bwrap on Linux): "
    "refusing to run model-written code"
)
# secrets the sandboxed code may not read; every other read under $HOME is NOT blocked (3.6)
DENY_READ_UNDER_HOME: Final = (".ssh", ".aws", ".gnupg", ".kiro", ".claude", ".netrc", ".config/gh")
ENV_PATH: Final = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
CPU_S_MAX: Final = 30  # RLIMIT_CPU; the wall timeout is the other backstop
FILE_BYTES_MAX: Final = 10 * 1024 * 1024  # RLIMIT_FSIZE: largest file a process may write
OPEN_FILES_MAX: Final = 256
MEMORY_BYTES_MAX: Final = 2 * 1024 * 1024 * 1024  # RLIMIT_AS, Linux only: macOS ignores it
OUTPUT_BYTES_MAX: Final = 65_536  # kept stdout and stderr, each
KILL_WAIT_S: Final = 5.0
CANARY_TIMEOUT_S: Final = 20.0
STDERR_TAIL_CHARS: Final = 300  # enough for bwrap's one-line reason it couldn't start

STATUS_OK: Final = "ok"
STATUS_TIMEOUT: Final = "timeout"
CANARY_OK: Final = "NET_DENIED WRITE_DENIED"
# argv[2] is a port with a live listener outside, so only the sandbox can make the connect fail:
# sandbox-exec answers EPERM, bwrap's empty network namespace ECONNREFUSED. EROFS is bwrap's
# read-only root. A missing dir is not a denial, so it doesn't count.
CANARY_SOURCE: Final = (
    "import errno, os, socket, sys\n"
    "out = []\n"
    "try:\n"
    "    socket.create_connection(('127.0.0.1', int(sys.argv[2])), timeout=2).close()\n"
    "except OSError:\n"
    "    out.append('NET_DENIED')\n"
    "try:\n"
    "    open(os.path.join(sys.argv[1], 'canary.txt'), 'w').close()\n"
    "except OSError as err:\n"
    "    if err.errno in (errno.EACCES, errno.EPERM, errno.EROFS):\n"
    "        out.append('WRITE_DENIED')\n"
    "print(' '.join(out))\n"
)


@dataclass(frozen=True)
class SandboxResult:
    status: str  # ok | timeout
    returncode: int | None
    stdout_text: str
    stderr_text: str


def _require(ok: bool, why: str) -> None:
    """An assert that survives python -O: these checks hold the sandbox shut."""
    if not ok:
        raise ValueError(why)


def _quote(path: Path) -> str:
    """A path as an SBPL string literal; paths with a quote or backslash are refused."""
    text = str(path)
    _require(text.startswith("/"), "profile paths are absolute")
    _require('"' not in text and "\\" not in text, "no characters that need escaping in a profile")
    return f'"{text}"'


def build_profile(scratch: Path, home: Path) -> str:
    """SBPL: allow by default, no network, writes only under scratch, secret dirs unreadable.
    The last matching rule wins, so the allows sit after the denies."""
    _require(scratch.is_absolute() and home.is_absolute(), "paths are absolute")
    lines = ["(version 1)", "(allow default)", "(deny network*)", "(deny file-write*)"]
    lines.append(f'(allow file-write* (subpath {_quote(scratch)}) (literal "/dev/null"))')
    for name in DENY_READ_UNDER_HOME:
        # a symlinked secret is readable at its target, so deny both paths
        for path in dict.fromkeys((home / name, (home / name).resolve())):
            lines.append(f"(deny file-read* (subpath {_quote(path)}))")
    _require(any(line.startswith("(deny network") for line in lines), "the network is denied")
    return "\n".join(lines) + "\n"


def build_bwrap_argv(scratch: Path, home: Path) -> list[str]:
    """bwrap: the whole filesystem read-only, a fresh /dev and /proc, an empty network namespace,
    scratch writable, and each secret under home that exists covered: a dir by an empty tmpfs, a
    file by /dev/null. Covering the resolved path also covers a symlink to it."""
    _require(scratch.is_absolute() and home.is_absolute(), "paths are absolute")
    argv = [BWRAP, "--die-with-parent", "--unshare-net", "--unshare-pid", "--unshare-ipc"]
    argv += ["--unshare-uts", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    for name in DENY_READ_UNDER_HOME:
        secret = (home / name).resolve()
        if secret.is_dir():
            argv += ["--tmpfs", str(secret)]
        elif secret.exists():
            argv += ["--ro-bind", "/dev/null", str(secret)]
    argv += ["--bind", str(scratch), str(scratch), "--chdir", str(scratch)]
    _require("--unshare-net" in argv, "the network is denied")
    # later mounts sit on top, so scratch must come after the read-only root
    _require(argv.index("--ro-bind") < argv.index("--bind"), "scratch is bound after the root")
    return argv


def backend() -> str | None:
    """The sandbox this machine has, or None. Each platform has exactly one; no fallback."""
    platform = sys.platform  # a plain str, so the checker types both branches on either host
    if platform == "darwin":
        return BACKEND_SANDBOX_EXEC if os.path.exists(SANDBOX_EXEC) else None
    if platform.startswith("linux"):
        return BACKEND_BWRAP if os.path.exists(BWRAP) else None
    return None


def sandbox_argv(argv: list[str], scratch: Path, home: Path) -> list[str]:
    """argv wrapped in this machine's sandbox. Raises FileNotFoundError when there is none."""
    assert argv, "a command to wrap"
    kind = backend()
    if kind == BACKEND_SANDBOX_EXEC:
        return [SANDBOX_EXEC, "-p", build_profile(scratch, home), *argv]
    if kind == BACKEND_BWRAP:
        return [*build_bwrap_argv(scratch, home), "--", *argv]
    raise FileNotFoundError(NO_SANDBOX)


def _limit_resources() -> None:
    """Runs in the child before exec, and the sandbox passes the limits on."""
    assert CPU_S_MAX > 0 and FILE_BYTES_MAX > 0, "limits are positive"
    assert OPEN_FILES_MAX >= 64, "enough descriptors for python and pytest"
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_S_MAX, CPU_S_MAX))
    resource.setrlimit(resource.RLIMIT_FSIZE, (FILE_BYTES_MAX, FILE_BYTES_MAX))
    resource.setrlimit(resource.RLIMIT_NOFILE, (OPEN_FILES_MAX, OPEN_FILES_MAX))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    platform = sys.platform  # a plain str, as in backend()
    if platform.startswith("linux"):
        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES_MAX, MEMORY_BYTES_MAX))


def scrubbed_env(scratch: Path) -> dict[str, str]:
    """No token, no inherited variable: only what an interpreter needs."""
    _require(scratch.is_absolute(), "scratch is absolute")
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
    _require("KIRO_HOME" not in env and not any("TOKEN" in key for key in env), "no secrets")
    return env


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    assert proc.pid > 0, "a started process"
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # start_new_session made pid the group id
    except (ProcessLookupError, PermissionError):
        pass  # the group is already gone


def run_sandboxed(argv: list[str], scratch: Path, timeout_s: float) -> SandboxResult:
    """Run argv with cwd = scratch inside the sandbox. Never raises on the child's behavior;
    a missing sandbox raises FileNotFoundError, which callers prevent with check_sandbox."""
    assert argv and timeout_s > 0, "a command and a positive timeout"
    _require(scratch.is_absolute() and scratch.is_dir(), "scratch is an existing absolute dir")
    real = scratch.resolve()
    command = sandbox_argv(argv, real, Path.home().resolve())
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(
            command,
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
    else why not. Runs a known-bad input through the real sandbox every time (fails closed)."""
    assert CANARY_SOURCE and CANARY_TIMEOUT_S > 0, "the canary program and its timeout are defined"
    assert sys.executable, "an interpreter to run the canary with"
    if backend() is None:
        return NO_SANDBOX
    with (
        tempfile.TemporaryDirectory(prefix="evsbx-canary-") as parent,
        socket.socket() as listener,
    ):
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = str(listener.getsockname()[1])
        scratch = Path(parent).resolve() / "inside"
        outside = Path(parent).resolve() / "outside"
        scratch.mkdir()
        outside.mkdir()
        canary = [sys.executable, "-c", CANARY_SOURCE, str(outside), port]
        try:
            result = run_sandboxed(canary, scratch, CANARY_TIMEOUT_S)
        except OSError as err:
            return f"sandbox canary could not start: {err}"
        leaked = (outside / "canary.txt").exists()
    if result.status != STATUS_OK or result.stdout_text.strip() != CANARY_OK or leaked:
        seen, why = result.stdout_text.strip(), result.stderr_text[-STDERR_TAIL_CHARS:]
        return f"sandbox canary did not see the attempts denied: {seen!r}, stderr {why!r}"
    return None
