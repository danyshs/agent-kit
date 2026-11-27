"""Goal: model-written code runs only inside a sandbox that is proven to deny network and writes
outside the scratch dir, and the sandbox guard fails closed (design 3.6, debugging-discipline #7).
Method: run small Python programs through this machine's real sandbox (sandbox-exec on macOS, bwrap
on Linux) and assert on what they could and could not do; break the guard on purpose (a sandbox
that denies nothing, no sandbox at all) and see it trip. Every known-bad input has a known-good twin
so a test cannot pass by the sandbox being down. The profile, the bwrap argv and the backend choice
are checked on every platform."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import AbstractContextManager
from pathlib import Path
from unittest import mock

from lib import sandbox
from lib.sandbox import (
    DENY_READ_UNDER_HOME,
    OUTPUT_BYTES_MAX,
    STATUS_OK,
    STATUS_TIMEOUT,
    SandboxResult,
    build_bwrap_argv,
    build_profile,
    check_sandbox,
    run_sandboxed,
    scrubbed_env,
)

HAVE_SANDBOX = sandbox.backend() is not None
NO_SANDBOX_HERE = "no sandbox on this machine"
# the grandchild in the timeout test; it beats until it is killed or its dir is gone
BEAT = "import time\nwhile True:\n    open('beat', 'a').write('.')\n    time.sleep(0.05)\n"


def py(code: str, scratch: Path, *args: str, timeout_s: float = 30.0) -> SandboxResult:
    return run_sandboxed([sys.executable, "-c", code, *args], scratch, timeout_s)


def has_run(argv: list[str], run: list[str]) -> bool:
    """run appears in argv as consecutive items."""
    return any(argv[i : i + len(run)] == run for i in range(len(argv)))


def permissive(deny_network: bool) -> AbstractContextManager[object]:
    """This machine's sandbox, broken: writes go through, and the network unless deny_network."""
    if sandbox.backend() == sandbox.BACKEND_BWRAP:
        argv = [sandbox.BWRAP, *(["--unshare-net"] if deny_network else []), "--bind", "/", "/"]
        return mock.patch.object(sandbox, "build_bwrap_argv", return_value=[*argv, "--dev", "/dev"])
    profile = "(version 1)(allow default)" + ("(deny network*)" if deny_network else "") + "\n"
    return mock.patch.object(sandbox, "build_profile", return_value=profile)


@unittest.skipUnless(HAVE_SANDBOX, NO_SANDBOX_HERE)
class SandboxTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name).resolve()
        self.scratch = base / "scratch"
        self.outside = base / "outside"
        self.scratch.mkdir()
        self.outside.mkdir()

    def test_the_canary_passes_on_this_machine(self) -> None:
        self.assertIsNone(check_sandbox())

    def test_network_connect_is_denied_and_the_unsandboxed_twin_connects(self) -> None:
        code = (
            "import socket, sys\n"
            "socket.create_connection(('127.0.0.1', int(sys.argv[1])), timeout=3)\n"
            "print('CONNECTED')\n"
        )
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = str(server.getsockname()[1])
            sandboxed = py(code, self.scratch, port)
            twin = subprocess.run(
                [sys.executable, "-c", code, port], capture_output=True, text=True, check=False
            )
        self.assertEqual(twin.stdout.strip(), "CONNECTED", "the twin proves the listener works")
        self.assertNotIn("CONNECTED", sandboxed.stdout_text)
        self.assertNotEqual(sandboxed.returncode, 0)
        self.assertIn("Error", sandboxed.stderr_text, "the connect raised")

    def test_write_outside_scratch_is_denied_and_inside_is_allowed(self) -> None:
        code = "import sys\nopen(sys.argv[1] + '/f.txt', 'w').write('x')\nprint('WROTE')\n"
        inside = py(code, self.scratch, str(self.scratch))
        outside = py(code, self.scratch, str(self.outside))
        self.assertIn("WROTE", inside.stdout_text)
        self.assertTrue((self.scratch / "f.txt").is_file())
        self.assertNotIn("WROTE", outside.stdout_text)
        self.assertFalse((self.outside / "f.txt").exists())

    def test_secrets_are_unreadable_and_other_home_files_are_not_blocked(self) -> None:
        base = Path(self._tmp.name).resolve()
        home = base / "fakehome"
        for name in (".ssh", "docs"):
            (home / name).mkdir(parents=True)
            (home / name / "f").write_text("secret", encoding="utf-8")
        (base / "realclaude").mkdir()
        (base / "realclaude" / "f").write_text("secret", encoding="utf-8")
        (home / ".claude").symlink_to(base / "realclaude")
        (home / ".netrc").write_text("secret", encoding="utf-8")

        def cat(path: Path) -> subprocess.CompletedProcess[str]:
            argv = sandbox.sandbox_argv(["/bin/cat", str(path)], self.scratch, home)
            return subprocess.run(argv, capture_output=True, text=True, check=False)

        self.assertEqual(cat(home / "docs/f").stdout, "secret", "reads off the deny list work")
        for secret in (home / ".ssh/f", home / ".claude/f", base / "realclaude/f", home / ".netrc"):
            self.assertEqual(cat(secret).stdout, "", secret)

    def test_timeout_kills_the_process_group_and_reports_it(self) -> None:
        # a pid from inside a pid namespace means nothing outside, so watch the beat file instead
        code = f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', {BEAT!r}])\n"
        code += "time.sleep(60)\n"
        started = time.monotonic()
        result = py(code, self.scratch, timeout_s=2.0)
        self.assertEqual(result.status, STATUS_TIMEOUT)
        self.assertIsNone(result.returncode)
        self.assertLess(time.monotonic() - started, 15)
        beat = self.scratch / "beat"
        self.assertTrue(beat.is_file(), "the grandchild ran")
        for _ in range(30):  # the kill is async; give it up to 3 s
            size = beat.stat().st_size
            time.sleep(0.3)
            if beat.stat().st_size == size:
                break
        else:
            self.fail("the grandchild outlived the timeout")

    def test_normal_exit_reports_ok_and_the_return_code(self) -> None:
        result = py("import sys\nprint('hi')\nsys.exit(3)\n", self.scratch)
        self.assertEqual((result.status, result.returncode), (STATUS_OK, 3))
        self.assertEqual(result.stdout_text, "hi\n")

    def test_output_is_capped(self) -> None:
        result = py("print('x' * 500_000)\n", self.scratch)
        self.assertLessEqual(len(result.stdout_text), OUTPUT_BYTES_MAX)
        self.assertGreater(len(result.stdout_text), 1000, "the cap truncates, it does not drop")

    def test_file_size_limit_stops_a_runaway_writer(self) -> None:
        code = (
            "f = open('big.bin', 'wb')\n"
            "try:\n"
            "    for _ in range(100):\n"
            "        f.write(b'x' * 1_000_000)\n"
            "        f.flush()\n"
            "except OSError:\n"
            "    print('LIMITED')\n"
        )
        result = py(code, self.scratch)
        self.assertLessEqual((self.scratch / "big.bin").stat().st_size, sandbox.FILE_BYTES_MAX)
        self.assertTrue("LIMITED" in result.stdout_text or result.returncode != 0)

    def test_environment_is_scrubbed(self) -> None:
        with mock.patch.dict(os.environ, {"SECRET_API_TOKEN": "hunter2", "KIRO_HOME": "/x"}):
            result = py(
                "import os\nprint(sorted(os.environ))\nprint(os.environ['HOME'])\n", self.scratch
            )
        self.assertNotIn("SECRET_API_TOKEN", result.stdout_text)
        self.assertNotIn("KIRO_HOME", result.stdout_text)
        self.assertIn(str(self.scratch), result.stdout_text, "HOME is the scratch dir")
        env = scrubbed_env(self.scratch)
        self.assertEqual(env["HOME"], str(self.scratch))
        self.assertNotIn("KIRO_HOME", env)


@unittest.skipUnless(HAVE_SANDBOX, NO_SANDBOX_HERE)
class SandboxFailsClosedTest(unittest.TestCase):
    """The guard must be seen to trip on a known-bad sandbox, not only to pass on a good one."""

    def test_a_sandbox_that_denies_nothing_trips_the_canary(self) -> None:
        with permissive(deny_network=False):
            problem = check_sandbox()
        assert problem is not None
        self.assertIn("did not see the attempts denied", problem)

    def test_a_sandbox_that_denies_only_the_network_trips_the_canary(self) -> None:
        with permissive(deny_network=True):
            problem = check_sandbox()
        assert problem is not None
        self.assertIn("did not see the attempts denied", problem)


class BackendTest(unittest.TestCase):
    """Each platform gets its own sandbox or none; there is no fallback to running bare."""

    def pick(self, platform: str, present: set[str]) -> str | None:
        def exists(path: str) -> bool:
            return path in present

        with (
            mock.patch.object(sys, "platform", platform),
            mock.patch.object(sandbox.os.path, "exists", side_effect=exists),
        ):
            return sandbox.backend()

    def test_each_platform_uses_only_its_own_sandbox(self) -> None:
        both = {sandbox.SANDBOX_EXEC, sandbox.BWRAP}
        self.assertEqual(self.pick("darwin", both), sandbox.BACKEND_SANDBOX_EXEC)
        self.assertEqual(self.pick("linux", both), sandbox.BACKEND_BWRAP)
        self.assertIsNone(self.pick("darwin", {sandbox.BWRAP}))
        self.assertIsNone(self.pick("linux", {sandbox.SANDBOX_EXEC}))
        self.assertIsNone(self.pick("win32", both))

    def test_no_sandbox_refuses_and_wrapping_raises(self) -> None:
        with mock.patch.object(sandbox, "backend", return_value=None):
            self.assertEqual(check_sandbox(), sandbox.NO_SANDBOX)
            with self.assertRaises(FileNotFoundError):
                sandbox.sandbox_argv(["/bin/true"], Path("/tmp/s"), Path("/home/x"))
        self.assertIn("refusing to run", sandbox.NO_SANDBOX)

    def test_memory_is_capped_on_linux_only(self) -> None:
        for platform, capped in (("linux", True), ("darwin", False)):
            with (
                mock.patch.object(sys, "platform", platform),
                mock.patch.object(sandbox.resource, "setrlimit") as setrlimit,
            ):
                sandbox._limit_resources()  # pyright: ignore[reportPrivateUsage] -- the unit under test
            limits = {call.args[0] for call in setrlimit.call_args_list}
            self.assertEqual(sandbox.resource.RLIMIT_AS in limits, capped, platform)
            self.assertIn(sandbox.resource.RLIMIT_CPU, limits)


class ProfileTest(unittest.TestCase):
    def test_profile_denies_network_and_writes_then_allows_scratch(self) -> None:
        profile = build_profile(Path("/tmp/s"), Path("/home/x"))
        lines = profile.splitlines()
        self.assertEqual(
            lines[:4], ["(version 1)", "(allow default)", "(deny network*)", "(deny file-write*)"]
        )
        self.assertIn('(allow file-write* (subpath "/tmp/s") (literal "/dev/null"))', lines)
        self.assertIn('(deny file-read* (subpath "/home/x/.ssh"))', lines)
        deny_at = lines.index("(deny file-write*)")
        allow_at = next(i for i, line in enumerate(lines) if line.startswith("(allow file-write*"))
        self.assertLess(deny_at, allow_at, "the last matching rule wins, so the allow comes last")

    def test_profile_denies_each_secret_once_and_a_symlinked_one_at_its_target(self) -> None:
        self.assertEqual(
            # /home is itself a link on macOS, so the plain case uses a path nothing resolves
            build_profile(Path("/tmp/s"), Path("/nonexistent/x")).count("(deny file-read*"),
            len(DENY_READ_UNDER_HOME),
        )
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            (home / "real").mkdir()
            (home / ".claude").symlink_to(home / "real")
            profile = build_profile(Path("/tmp/s"), home)
        self.assertIn(f'(deny file-read* (subpath "{home / ".claude"}"))', profile)
        self.assertIn(f'(deny file-read* (subpath "{home / "real"}"))', profile)

    def test_profile_refuses_paths_that_need_escaping(self) -> None:
        with self.assertRaises(ValueError):
            build_profile(Path('/tmp/a"b'), Path("/home/x"))
        with self.assertRaises(ValueError):
            build_profile(Path("relative"), Path("/home/x"))


class BwrapArgvTest(unittest.TestCase):
    def test_root_read_only_no_network_scratch_writable_last(self) -> None:
        argv = build_bwrap_argv(Path("/tmp/s"), Path("/nonexistent/home"))
        self.assertEqual(argv[0], sandbox.BWRAP)
        self.assertIn("--unshare-net", argv)
        self.assertTrue(has_run(argv, ["--ro-bind", "/", "/"]))
        self.assertTrue(has_run(argv, ["--dev", "/dev"]))
        self.assertEqual(argv[-5:], ["--bind", "/tmp/s", "/tmp/s", "--chdir", "/tmp/s"])
        self.assertLess(argv.index("--ro-bind"), argv.index("--bind"), "scratch sits on top")
        self.assertFalse(any("/nonexistent/home" in part for part in argv), "no missing secrets")

    def test_existing_secrets_are_covered_and_a_symlink_at_its_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            (home / ".ssh").mkdir()
            (home / "real").mkdir()
            (home / ".claude").symlink_to(home / "real")
            (home / ".netrc").write_text("x", encoding="utf-8")
            argv = build_bwrap_argv(Path("/tmp/s"), home)
        self.assertTrue(has_run(argv, ["--tmpfs", str(home / ".ssh")]))
        self.assertTrue(has_run(argv, ["--tmpfs", str(home / "real")]))
        self.assertTrue(has_run(argv, ["--ro-bind", "/dev/null", str(home / ".netrc")]))
        self.assertFalse(any(str(home / ".aws") in part for part in argv))
        self.assertLess(argv.index("--tmpfs"), argv.index("--bind"))

    def test_relative_paths_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            build_bwrap_argv(Path("relative"), Path("/home/x"))
