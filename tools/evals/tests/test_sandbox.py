"""Goal: model-written code runs only inside a sandbox that is proven to deny network and writes
outside the scratch dir, and the sandbox guard fails closed (design 3.6, debugging-discipline #7).
Method: run small Python programs through the real sandbox-exec profile and assert on what they
could and could not do; break the guard on purpose (no profile denies, no sandbox-exec) and see it
trip. Every known-bad input has a known-good twin so a test cannot pass by the sandbox being down.
macOS only: the sandbox is sandbox-exec."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from lib import sandbox
from lib.sandbox import (
    DENY_READ_UNDER_HOME,
    OUTPUT_BYTES_MAX,
    STATUS_OK,
    STATUS_TIMEOUT,
    SandboxResult,
    build_profile,
    check_sandbox,
    run_sandboxed,
    scrubbed_env,
)

DARWIN = sys.platform == "darwin"


def py(code: str, scratch: Path, *args: str, timeout_s: float = 30.0) -> SandboxResult:
    return run_sandboxed([sys.executable, "-c", code, *args], scratch, timeout_s)


@unittest.skipUnless(DARWIN, "sandbox-exec is macOS only")
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
        self.assertIn("PermissionError", sandboxed.stderr_text)

    def test_write_outside_scratch_is_denied_and_inside_is_allowed(self) -> None:
        code = "import sys\nopen(sys.argv[1] + '/f.txt', 'w').write('x')\nprint('WROTE')\n"
        inside = py(code, self.scratch, str(self.scratch))
        outside = py(code, self.scratch, str(self.outside))
        self.assertIn("WROTE", inside.stdout_text)
        self.assertTrue((self.scratch / "f.txt").is_file())
        self.assertNotIn("WROTE", outside.stdout_text)
        self.assertFalse((self.outside / "f.txt").exists())

    def test_secret_dirs_are_unreadable_and_other_home_files_are_not_blocked(self) -> None:
        home = Path(self._tmp.name).resolve() / "fakehome"
        for name in (".ssh", "docs"):
            (home / name).mkdir(parents=True)
            (home / name / "f").write_text("secret", encoding="utf-8")
        profile = build_profile(self.scratch, home)

        def cat(rel: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [sandbox.SANDBOX_EXEC, "-p", profile, "/bin/cat", str(home / rel)],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(cat("docs/f").stdout, "secret", "reads outside the deny list work (3.6)")
        self.assertEqual(cat(".ssh/f").stdout, "")
        self.assertNotEqual(cat(".ssh/f").returncode, 0)
        self.assertEqual(len(DENY_READ_UNDER_HOME), profile.count("(deny file-read*"))

    def test_timeout_kills_the_process_group_and_reports_it(self) -> None:
        code = (
            "import subprocess, time\n"
            "child = subprocess.Popen(['sleep', '60'])\n"
            "open('child.pid', 'w').write(str(child.pid))\n"
            "time.sleep(60)\n"
        )
        started = time.monotonic()
        result = py(code, self.scratch, timeout_s=2.0)
        self.assertEqual(result.status, STATUS_TIMEOUT)
        self.assertIsNone(result.returncode)
        self.assertLess(time.monotonic() - started, 15)
        child = int((self.scratch / "child.pid").read_text(encoding="utf-8"))
        for _ in range(30):  # the kill is async; give it up to 3 s
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            os.kill(child, 9)  # do not leave it behind if the assertion below fails
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


@unittest.skipUnless(DARWIN, "sandbox-exec is macOS only")
class SandboxFailsClosedTest(unittest.TestCase):
    """The guard must be seen to trip on a known-bad sandbox, not only to pass on a good one."""

    def test_a_profile_that_denies_nothing_trips_the_canary(self) -> None:
        with mock.patch.object(
            sandbox, "build_profile", return_value="(version 1)(allow default)\n"
        ):
            problem = check_sandbox()
        assert problem is not None
        self.assertIn("did not see the attempts denied", problem)

    def test_a_profile_that_denies_only_the_network_trips_the_canary(self) -> None:
        profile = "(version 1)(allow default)(deny network*)\n"
        with mock.patch.object(sandbox, "build_profile", return_value=profile):
            problem = check_sandbox()
        assert problem is not None
        self.assertIn("did not see the attempts denied", problem)

    def test_a_missing_sandbox_exec_refuses(self) -> None:
        with mock.patch.object(sandbox, "SANDBOX_EXEC", "/nonexistent/sandbox-exec"):
            problem = check_sandbox()
        assert problem is not None
        self.assertIn("refusing to run", problem)

    def test_a_non_macos_platform_refuses(self) -> None:
        with mock.patch.object(sys, "platform", "linux"):
            problem = check_sandbox()
        assert problem is not None
        self.assertIn("refusing to run", problem)


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

    def test_profile_refuses_paths_that_need_escaping(self) -> None:
        with self.assertRaises(ValueError):
            build_profile(Path('/tmp/a"b'), Path("/home/x"))
        with self.assertRaises(ValueError):
            build_profile(Path("relative"), Path("/home/x"))
