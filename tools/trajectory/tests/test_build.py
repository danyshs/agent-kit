"""build end to end on fixtures, plus the stage 2 store pieces: skips, coverage, backup."""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

from tests.helpers import SESSION_ID, make_coverage, make_data
from traj import cli, store
from traj.limits import SKIP_REASONS
from traj.schema import Skip

FIXTURES = Path(__file__).parent / "fixtures" / "kiro"
BASIC = "11111111-1111-4111-8111-111111111111"


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class Env:
    """A scratch home (sessions copied from fixture cases), events dir and backup dir."""

    def __init__(self, tmp: str, *cases: str) -> None:
        self.root = Path(tmp)
        self.home = self.root / "home"
        for case in cases:
            shutil.copytree(FIXTURES / case, self.home / "sessions", dirs_exist_ok=True)
        (self.home / "sessions").mkdir(parents=True, exist_ok=True)
        # never the live ~/.claude/projects: an empty scratch store unless a test fills it
        self.projects = self.root / "projects"
        self.projects.mkdir()
        self.events = self.root / "out" / "events"
        self.backup = self.root / "backup"

    def build(self, *extra: str) -> tuple[int, str, str]:
        return run(
            "build",
            "--home",
            str(self.home),
            "--projects",
            str(self.projects),
            "--events-dir",
            str(self.events),
            "--backup-dir",
            str(self.backup),
            "--stamp",
            "t",
            *extra,
        )

    def file(self, session_id: str = BASIC) -> Path:
        return self.events / "kiro" / f"{session_id}.jsonl"


class BuildTest(unittest.TestCase):
    def test_build_writes_events_coverage_skips_and_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, "basic", "no_session_json")
            code, out, err = env.build()
            self.assertEqual((code, err), (0, ""))
            self.assertTrue(env.file().is_file())
            self.assertEqual(len(list((env.events / "kiro").iterdir())), 1)
            self.assertEqual(len(list((env.backup / "kiro").iterdir())), 1)
            self.assertIn("| seen | 2 |", out)
            self.assertIn("| no session.json | 1 |", out)
            self.assertEqual(
                out.splitlines()[-1], "traj: sessions=2 built=1 skipped=1 malformed=0 unknown=0"
            )
            skips = store.load_skips(env.events.parent)
            self.assertEqual(
                skips, [Skip("77777777-7777-4777-8777-777777777777", "kiro", "no session.json")]
            )
            cov = store.load_coverage(env.events.parent)
            assert not isinstance(cov, str)
            self.assertEqual(cov.seen, 2)

    def test_the_session_bound_holds_through_listing_store_and_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, "basic", "user_neither")
            code, out, _ = env.build("--sessions-max", "2")  # exactly at the bound
            self.assertEqual(code, 0)
            self.assertIn("backup copied 2 files", out)
            code, out, _ = env.build("--sessions-max", "1")  # 2 sessions listed
            self.assertEqual((code, out.strip()), (1, "build: error kiro: over 1 sessions"))
            shutil.rmtree(env.home / "sessions")
            (env.home / "sessions").mkdir()  # no raw logs, 2 events files, bound 1
            code, out, _ = env.build("--sessions-max", "1")
            self.assertEqual(code, 1)
            self.assertIn("store has more than 1 events files", out)

    def test_state_files_follow_state_dir_not_events_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, "basic")
            state = Path(tmp) / "state"
            code, _, _ = env.build("--state-dir", str(state))
            self.assertEqual(code, 0)
            self.assertTrue((state / "coverage.json").is_file())
            self.assertTrue((state / "skips.jsonl").is_file())
            self.assertFalse((env.events.parent / "coverage.json").exists())

    def test_unsound_coverage_is_an_error_line_not_a_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, "basic")
            with mock.patch("traj.cli.write_coverage", side_effect=ValueError("invalid coverage")):
                code, out, _ = env.build()
            self.assertEqual((code, out.strip()), (1, "build: error invalid coverage"))

    def test_a_raw_log_that_grows_after_the_listing_is_rebuilt_next_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, "basic")
            env.build()
            first = json.loads(env.file().read_text().splitlines()[0])["source_bytes"]
            messages = next(env.home.glob("sessions/*/sess_*/messages.jsonl"))
            with messages.open("a") as handle:
                handle.write(json.dumps({"payload": {"type": "session_start"}}) + "\n")
            env.build()
            second = json.loads(env.file().read_text().splitlines()[0])["source_bytes"]
            self.assertGreater(second, first)

    def test_the_canary_is_in_no_output_or_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, "basic", "subexec", "malformed", "user_neither", "no_session_json")
            _, out, err = env.build()
            texts = [out, err]
            for path in env.root.rglob("*"):
                if path.is_file() and env.home not in path.parents:
                    texts.append(path.read_text())
            self.assertGreater(len(texts), 8)
            for text in texts:
                self.assertNotIn("CANARY", text)
                self.assertNotIn("DEADBEEF", text)

    def test_second_build_reuses_and_gives_identical_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, "basic")
            env.build()
            first = env.file().read_bytes()
            stamp = env.file().stat().st_mtime_ns
            _, out, _ = env.build()
            self.assertEqual(env.file().stat().st_mtime_ns, stamp)  # not rewritten
            self.assertIn("backup copied 0 files", out)
            _, _, _ = env.build("--force")
            self.assertEqual(env.file().read_bytes(), first)  # deterministic

    def test_raw_change_triggers_a_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, "basic")
            env.build()
            messages = next(env.home.glob("sessions/*/sess_*/messages.jsonl"))
            with messages.open("a") as handle:
                handle.write(json.dumps({"payload": {"type": "session_start"}}) + "\n")
            stamp = env.file().stat().st_mtime_ns
            env.build()
            self.assertNotEqual(env.file().stat().st_mtime_ns, stamp)

    def test_unknown_format_exits_1_with_a_banner_and_still_writes_others(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, "basic", "bad_version")
            code, out, _ = env.build()
            self.assertEqual(code, 1)
            self.assertIn("build: data-quality error 1 sessions skipped as unknown format", out)
            self.assertTrue(env.file().is_file())
            self.assertEqual(out.splitlines()[-1].split()[0], "traj:")

    def test_malformed_over_one_percent_exits_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, "malformed")
            code, out, _ = env.build()
            self.assertEqual(code, 1)
            self.assertIn("malformed events 3 of", out)
            self.assertIn("unknown events 1 of 7", out)

    def test_over_bound_sessions_are_skipped_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, "basic")
            code, out, _ = env.build("--lines-max", "3")
            self.assertEqual(code, 0)
            self.assertIn("| over bound | 1 |", out)
            self.assertFalse(env.file().exists())

    def test_write_failure_for_one_session_is_a_coverage_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, "basic")
            with mock.patch("traj.cli.write_session", side_effect=ValueError("too big")):
                code, out, _ = env.build()
            self.assertEqual(code, 0)
            self.assertIn("| events not written | 1 |", out)
            self.assertIn("traj: sessions=1 built=0 skipped=1", out)

    def test_an_events_file_with_no_raw_log_is_never_rewritten_or_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, "basic")
            env.build()
            shutil.rmtree(env.home / "sessions")
            (env.home / "sessions").mkdir()
            before = env.file().read_bytes()
            stamp = env.file().stat().st_mtime_ns
            for extra in ((), ("--force",)):
                _, out, _ = env.build(*extra)
                self.assertIn("| events files with no raw log | 1 |", out)
                self.assertEqual(env.file().read_bytes(), before)
                self.assertEqual(env.file().stat().st_mtime_ns, stamp)

    def test_backup_restores_the_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, "basic")
            env.build()
            saved = (env.backup / "kiro" / env.file().name).read_bytes()
            shutil.rmtree(env.events)
            env.build()
            self.assertEqual((env.backup / "kiro" / env.file().name).read_bytes(), saved)
            self.assertEqual(env.file().read_bytes(), saved)
            shutil.rmtree(env.events)
            shutil.copytree(env.backup, env.events, dirs_exist_ok=True)
            self.assertEqual(env.file().read_bytes(), saved)

    def test_missing_store_is_a_fatal_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp)
            shutil.rmtree(env.home / "sessions")
            code, out, _ = env.build("--harness", "kiro")
            self.assertEqual(code, 1)
            self.assertEqual(out.strip(), "build: error kiro: sessions is not a directory")

    def test_missing_kiro_store_is_fine_under_all(self) -> None:
        # Claude Code is the primary source; a machine without Kiro still builds
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp)
            shutil.rmtree(env.home / "sessions")
            code, out, _ = env.build()
            self.assertEqual(code, 0)
            self.assertIn("| seen | 0 |", out)

    def test_missing_claude_store_is_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, "basic")
            env.projects.rmdir()
            code, out, _ = env.build()
            self.assertEqual(code, 1)
            self.assertEqual(out.strip(), "build: error claude: projects is not a directory")

    def test_builds_claude_and_kiro_together(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, "basic")
            sid = "12345678-1234-4234-8234-123456789abc"
            line = {
                "type": "user",
                "uuid": "u1",
                "timestamp": "2026-11-20T10:00:01.000Z",
                "cwd": "/home/x/p",
                "message": {"content": "hi"},
            }
            project = env.projects / "-Users-x-p"
            project.mkdir()
            (project / f"{sid}.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
            code, out, _ = env.build()
            self.assertEqual(code, 0, out)
            self.assertTrue((env.events / "claude" / f"{sid}.jsonl").is_file())
            self.assertTrue(env.file().is_file())
            self.assertIn("| seen | 2 |", out)
            code, out, _ = env.build("--harness", "claude", "--force")
            self.assertIn("| seen | 1 |", out)

    def test_unwritable_events_dir_is_a_fatal_error_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Env(tmp, "basic")
            env.events.parent.mkdir(parents=True)
            env.events.write_text("a file, not a directory")
            code, out, _ = env.build()
            self.assertEqual(code, 1)
            self.assertTrue(out.startswith("build: error cannot write"))


class ArgsTest(unittest.TestCase):
    def test_usage_errors_exit_2_on_stderr(self) -> None:
        bad = [
            [],
            ["report"],
            ["build", "--nope", "1"],
            ["build", "--stamp"],
            ["build", "--stamp", " "],
            ["build", "--stamp", "x" * 65],
            ["build", "--harness", "everything"],
            ["build", "--sessions-max", "0"],
            ["build", "--sessions-max", "5001"],
            ["build", "--lines-max", "-1"],
            ["build", "--bytes-max", "268435457"],
            ["build", "--lines-max", "1", "--lines-max", "2"],
        ]
        for argv in bad:
            code, out, err = run(*argv)
            self.assertEqual((code, out), (2, ""), argv)
            self.assertNotEqual(err, "", argv)

    def test_good_flags_parse(self) -> None:
        options = cli.parse_build_args(
            ["--force", "--harness", "kiro", "--sessions-max", "5000", "--stamp", "ok"]
        )
        self.assertEqual(
            options,
            {"--force": "1", "--harness": "kiro", "--sessions-max": "5000", "--stamp": "ok"},
        )


class StoreStateTest(unittest.TestCase):
    def test_coverage_round_trip_and_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            cov = make_coverage()
            store.write_coverage(state, cov)
            self.assertEqual(store.load_coverage(state), cov)
            with self.assertRaises(ValueError):
                store.write_coverage(state, replace(cov, seen=5))
            path = state / "coverage.json"
            raw = json.loads(path.read_text())
            for bad in ({**raw, "extra": 1}, {**raw, "seen": "1"}, {**raw, "seen": 7}):
                path.write_text(json.dumps(bad))
                self.assertIsInstance(store.load_coverage(state), str)
            path.write_text("not json")
            self.assertIsInstance(store.load_coverage(state), str)
            path.unlink()
            self.assertIsInstance(store.load_coverage(state), str)

    def test_skips_round_trip_and_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            skips = [Skip(SESSION_ID, "kiro", "no cwd"), Skip("aaa", "kiro", "over bound")]
            store.write_skips(state, skips)
            self.assertEqual(
                store.load_skips(state),
                sorted(skips, key=lambda s: (s.harness, s.reason, s.session_id)),
            )
            (state / "skips.jsonl").write_text(
                '{"harness":"kiro","reason":"made up","session_id":"a"}\n'
            )
            self.assertIsInstance(store.load_skips(state), str)
            (state / "skips.jsonl").write_text(
                '{"harness":"kiro","reason":"no cwd","session_id":"CANARY 0x"}\n'
            )
            self.assertIsInstance(store.load_skips(state), str)

    def test_skip_reasons_are_the_doc_order(self) -> None:
        self.assertEqual(SKIP_REASONS[0], "headless tree not read in v1")
        self.assertEqual(SKIP_REASONS[2], "no session.json")
        self.assertEqual(SKIP_REASONS[1], "unreadable")
        self.assertEqual(len(set(SKIP_REASONS)), 8)

    def test_backup_copies_missing_and_changed_files_and_never_deletes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out, backup = Path(tmp) / "out", Path(tmp) / "backup"
            store.write_session(out, make_data())
            self.assertEqual(store.backup_events(out, backup), 1)
            self.assertEqual(store.backup_events(out, backup), 0)
            target = backup / "kiro" / f"{SESSION_ID}.jsonl"
            target.write_text("changed")
            self.assertEqual(store.backup_events(out, backup), 1)
            self.assertEqual(
                target.read_bytes(), store.events_path(out, "kiro", SESSION_ID).read_bytes()
            )
            stray = backup / "kiro" / "ffffffff-1111-4222-8333-444444444444.jsonl"
            stray.write_text("old")
            store.events_path(out, "kiro", SESSION_ID).unlink()
            self.assertEqual(store.backup_events(out, backup), 0)
            self.assertTrue(stray.exists() and target.exists())  # never pruned

    def test_backup_leaves_no_temp_file_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out, backup = Path(tmp) / "out", Path(tmp) / "backup"
            store.write_session(out, make_data())
            with mock.patch("traj.store.shutil.copy2", side_effect=OSError("disk")):
                with self.assertRaises(OSError):
                    store.backup_events(out, backup)
            self.assertEqual([p.name for p in (backup / "kiro").iterdir()], [])

    def test_count_without_raw_uses_listed_sessions_including_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            store.write_session(out, make_data())
            stored = store.list_stored(out, "kiro")
            assert isinstance(stored, list)
            self.assertEqual(store.count_without_raw(stored, []), 1)
            self.assertEqual(os.listdir(out / "kiro"), [f"{SESSION_ID}.jsonl"])


if __name__ == "__main__":
    unittest.main()
