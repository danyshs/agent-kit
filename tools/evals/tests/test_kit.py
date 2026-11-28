"""Goal: kit-home turns a checkout's plugins into the home the runner's --shipped mode
expects: every rule json becomes always-on steering, CLAUDE.md imports the lot, skills copy.
Method: a tiny fake kit in a throwaway dir; assert on the built files and on discovery."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from lib.kit import KIT_HOME_USAGE, build_shipped_home, parse_kit_home_args, run_kit_home
from tests.helpers import build_tree, world_and_limits, write_json

RULE_FRAME: dict[str, object] = {
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": "",
    }
}


def reason(built: str | None) -> str:
    """Narrow a build result to its failure text, so assertIn gets a visible string."""
    assert isinstance(built, str), f"the build should have failed: {built}"
    return built


def rule_plugin(kit: Path, name: str, *, skill: bool = False, filename: str = "") -> None:
    """A plugin folder with one rules/<name>.1.json and, when asked, a same-named skill."""
    base = kit / "plugins" / name
    frame: dict[str, object] = json.loads(json.dumps(RULE_FRAME))
    hook = frame["hookSpecificOutput"]
    assert isinstance(hook, dict), "frame comes from RULE_FRAME"
    stem = filename or name
    hook["additionalContext"] = f"rule: {stem}\n\n# {stem}\nDo it.\n"
    write_json(base / "rules" / f"{stem}.1.json", frame)
    if skill:
        skill_dir = base / "skills" / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\n---\nbody\n", encoding="utf-8")


class KitHomeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.kit = base / "kit"
        rule_plugin(self.kit, "alpha", skill=True)
        rule_plugin(self.kit, "beta")
        _home, self.root = build_tree(base)
        self.out = base / "out"
        self.out.mkdir()

    def test_builds_rules_claude_md_and_skills_then_discovers(self) -> None:
        self.assertIsNone(build_shipped_home(self.kit, self.out, 50))
        self.assertEqual(
            sorted(p.name for p in (self.out / "steering").glob("*.md")), ["alpha.md", "beta.md"]
        )
        alpha = (self.out / "steering" / "alpha.md").read_text(encoding="utf-8")
        self.assertTrue(alpha.startswith("rule: alpha\n\n# alpha"), "the json wrapper never leaks")
        self.assertEqual(
            (self.out / "CLAUDE.md").read_text(encoding="utf-8").splitlines(),
            ["@~/.claude/steering/alpha.md", "@~/.claude/steering/beta.md"],
        )
        self.assertTrue((self.out / "skills" / "alpha" / "SKILL.md").is_file())
        world, _limits = world_and_limits(self.out, self.root)
        self.assertTrue(world.claude, "a shipped home is a Claude home")
        self.assertEqual(
            (world.steering_names, world.skill_names),
            (frozenset({"alpha", "beta"}), frozenset({"alpha"})),
        )
        self.assertEqual(world.always_on_names, world.steering_names, "every rule is always on")

    def test_missing_plugins_or_a_wrong_rule_is_a_reason(self) -> None:
        bad = self.kit / "plugins" / "gamma" / "rules" / "gamma.1.json"
        write_json(bad, {"hookSpecificOutput": {"hookEventName": "SessionStart", "nope": ""}})
        self.assertIn(
            "must inject a rule named gamma", reason(build_shipped_home(self.kit, self.out, 50))
        )
        renamed = Path(self.tmp.name) / "renamed"
        renamed.mkdir()
        write_json(
            bad,
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": "rule: not-gamma\n\n# wrong name\n",
                }
            },
        )
        self.assertIn(
            "must inject a rule named gamma", reason(build_shipped_home(self.kit, renamed, 50))
        )
        bad.write_text("not json{", encoding="utf-8")
        again = Path(self.tmp.name) / "out2"
        again.mkdir()
        self.assertIn("is not JSON", reason(build_shipped_home(self.kit, again, 50)))
        nowhere = Path(self.tmp.name) / "out3"
        nowhere.mkdir()
        self.assertIn(
            "has no plugins/", reason(build_shipped_home(self.kit / "not-a-kit", nowhere, 50))
        )

    def test_two_plugins_with_the_same_rule_or_skill_name_is_a_reason(self) -> None:
        rule_plugin(self.kit, "delta", filename="alpha")
        self.assertIn("the same", reason(build_shipped_home(self.kit, self.out, 50)))
        shutil.rmtree(self.kit / "plugins" / "delta")
        rule_plugin(self.kit, "eta", filename="u")
        shutil.rmtree(self.kit / "plugins" / "eta" / "rules")
        (self.kit / "plugins" / "eta" / "skills" / "alpha").mkdir(parents=True)
        (self.kit / "plugins" / "eta" / "skills" / "alpha" / "SKILL.md").write_text(
            "x", encoding="utf-8"
        )
        self.assertIn("skill named alpha", reason(build_shipped_home(self.kit, self.out, 50)))

    def test_run_kit_home_usage_missing_out_and_full_out(self) -> None:
        self.assertEqual(run_kit_home([], self.root), ([KIT_HOME_USAGE], 2))
        absent = str(self.out / "nope")
        lines, code = run_kit_home(["--out", absent, "--kit", str(self.kit)], self.root)
        self.assertEqual((code, lines[0]), (1, f"kit-home: {absent} does not exist"))
        (self.out / "occupied").write_text("x", encoding="utf-8")
        _lines, code = run_kit_home(["--out", str(self.out), "--kit", str(self.kit)], self.root)
        self.assertEqual(code, 1)

    def test_run_kit_home_builds_and_ends_done(self) -> None:
        lines, code = run_kit_home(["--out", str(self.out), "--kit", str(self.kit)], self.root)
        self.assertEqual(code, 0, lines)
        self.assertEqual(lines[-1], "kit-home: done")
        self.assertTrue(lines[1].startswith("kit-home: kit=") and "rules=2" in lines[1], lines[1])

    def test_parse_defaults_the_kit_to_the_export_parent(self) -> None:
        self.assertEqual(
            parse_kit_home_args(["--out", "/tmp/z"], Path("/kit/tools/evals")),
            (Path("/tmp/z"), Path("/kit")),
        )
        self.assertEqual(
            parse_kit_home_args(["--out", "/tmp/z", "--kit", "/k"], Path("/x/y")),
            (Path("/tmp/z"), Path("/k")),
        )
        self.assertEqual(parse_kit_home_args(["--kit", "/k"], Path("/x/y")), KIT_HOME_USAGE)


if __name__ == "__main__":
    unittest.main()
