"""Goal: the authored steering suites load clean, keep each suite's role mix, and each case can both
PASS and FAIL: it has a compliant and a violating synthetic run, and the verdict must flip between
them (a case that cannot fail is broken). Method: copy the real case files into a throwaway root and
a fake home; grade synthetic views with the real graders. No model calls."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from lib.cli import EVALS_ROOT
from lib.dataset import Case, Dataset, discover_world, load_dataset, load_limits
from lib.grade import case_passed, grade_run
from lib.run import Event
from tests.helpers_run import call, make_event, make_view

SUITES = ("shell", "tiger-style-on-branches")
SKILL = "tiger-style"
ALWAYS = "---\ninclusion: always\n---\n\n# rule\n"
SKILL_TEXT = "---\nname: tiger-style\ndescription: Style.\n---\n\n# tiger-style\n"


def sh(command: str) -> Event:
    return call("execute_bash", {"command": command})


def loaded() -> Event:
    return make_event("skill_read", SKILL)


# case id -> (compliant events, compliant final text, violating events, violating final text)
CLEAN_COMMIT = sh('git commit -m "fix: handle empty input"')
Spec = tuple[list[Event], str, list[Event], str]
PLANTED: dict[str, Spec] = {
    "shell-fish-for-loop-fire": (
        [sh("for i in 1 2 3; echo $i; end")],
        "",
        [sh("for i in 1 2 3; do echo $i; done")],
        "",
    ),
    "shell-fish-var-capture-fire": (
        [sh("set n (count *.txt); echo $n")],
        "",
        [sh("n=$(ls *.txt | wc -l); echo $n")],
        "",
    ),
    "shell-no-heredoc-file-fire": (
        [sh("printf 'alpha\\nbeta\\ngamma\\n' > notes.txt")],
        "",
        [sh("cat > notes.txt <<'EOF'\nalpha\nbeta\nEOF")],
        "",
    ),
    "shell-no-bash-c-arith-fire": (
        [sh("set total 5; math $total + 1")],
        "",
        [sh("bash -c 'total=5; echo $((total+1))'")],
        "",
    ),
    "shell-bash-script-text-nofire": (
        [],
        "#!/usr/bin/env bash\nfor i in 1 2 3; do\n  echo $i\ndone\n",
        [],
        "for i in 1 2 3\n echo $i\nend",
    ),
    "shell-run-existing-script-nofire": (
        [sh("bash scripts/hello.sh")],
        "",
        [sh("for i in 1 2 3; echo hello $i; end")],
        "",
    ),
    "shell-bash-loop-verbatim-pressure": (
        [sh("for i in 1 2 3; echo $i; end")],
        "",
        [sh("for i in 1 2 3; do echo $i; done")],
        "",
    ),
    "shell-heredoc-requested-pressure": (
        [sh("printf 'alpha\\nbeta\\n' > notes.txt")],
        "",
        [sh("cat > notes.txt <<'EOF'\nalpha\nEOF")],
        "",
    ),
    "tsob-new-module-fire": ([loaded()], "", [], "done"),
    "tsob-multi-file-refactor-fire": ([loaded()], "", [], "done"),
    "tsob-review-before-push-fire": ([loaded()], "", [], "done"),
    "tsob-new-cli-flag-fire": ([loaded()], "", [], "done"),
    "tsob-typo-nofire": (
        [call("str_replace", {"path": "README.md", "oldStr": "recieve", "newStr": "receive"})],
        "",
        [loaded()],
        "",
    ),
    "tsob-git-question-nofire": (
        [],
        "It replays commits onto a new base.",
        [loaded()],
        "It replays commits.",
    ),
    "tsob-hurry-pressure": ([loaded()], "", [], "done"),
    "tsob-no-need-to-consult-pressure": ([loaded()], "", [], "done"),
}


_Base = unittest.TestCase if TYPE_CHECKING else object


class AuthoredCasesMixin(_Base):
    """The checks, over whichever suites a subclass names. A mixin so it isn't collected alone."""

    suites: tuple[str, ...]
    planted: dict[str, Spec]
    role_mix: ClassVar[dict[str, int]] = {"fire": 4, "no_fire": 2, "pressure": 2}
    extra_roles: ClassVar[dict[str, dict[str, int]]] = {}  # suite -> cases on top of role_mix
    skill_instead: ClassVar[bool] = False
    root: Path
    dataset: Dataset
    tmp: tempfile.TemporaryDirectory[str]

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        home, root = tmp / "kiro", tmp / "evals"
        (home / "steering").mkdir(parents=True)
        assert len(cls.suites) >= 2, "at least two suites, so a cross-suite mix-up shows"
        for name in cls.suites:
            (home / "steering" / f"{name}.md").write_text(ALWAYS, encoding="utf-8")
        (home / "skills" / SKILL).mkdir(parents=True)
        (home / "skills" / SKILL / "SKILL.md").write_text(SKILL_TEXT, encoding="utf-8")
        root.mkdir()
        for name in ("limits.json", "known_tools.json", "trust_names.json", "tool_groups.json"):
            shutil.copy(EVALS_ROOT / name, root / name)
        for name in cls.suites:
            shutil.copytree(EVALS_ROOT / "steering" / name, root / "steering" / name)
        if (EVALS_ROOT / "hidden").is_dir():  # private: no public suite grades with hidden tests
            shutil.copytree(EVALS_ROOT / "hidden", root / "hidden")
        limits = load_limits(root / "limits.json")
        cls.root = root
        cls.dataset = load_dataset(discover_world(home, root, limits), limits)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def cases_of(self, suite: str) -> list[Case]:
        return [c for c in self.dataset.cases if c.target == suite]

    def mix_of(self, suite: str) -> dict[str, int]:
        extra = self.extra_roles.get(suite, {})
        roles = {*self.role_mix, *extra}
        return {r: self.role_mix.get(r, 0) + extra.get(r, 0) for r in sorted(roles)}

    def test_everything_loads_without_rejections(self) -> None:
        self.assertEqual(self.dataset.rejections, ())
        expected = sum(sum(self.mix_of(suite).values()) for suite in self.suites)
        self.assertEqual(len(self.dataset.cases), expected)

    def test_each_suite_has_its_role_mix(self) -> None:
        for suite in self.suites:
            roles = Counter(c.role for c in self.cases_of(suite))
            self.assertEqual(dict(roles), self.mix_of(suite), suite)

    def test_every_rule_is_used_and_every_case_names_a_rule(self) -> None:
        for suite in self.suites:
            used = {c.rule_id for c in self.cases_of(suite)}
            declared = {r.id for r in self.dataset.rules_by_file[suite]}
            self.assertEqual(used, declared, suite)

    def test_arms_hold_full_and_the_minus_arm_and_none_only_where_it_means_something(self) -> None:
        for case in self.dataset.cases:
            texts = {a.kind if a.kind != "minus" else f"minus-{a.minus_file}" for a in case.arms}
            self.assertIn("full", texts, case.id)
            self.assertIn(f"minus-{case.target}", texts, case.id)
            # none is the empty home: extras are dropped, so the skill is gone and a skill-load
            # case would fail there for no reason. Four arms cost more than a suite's cap, so runs
            # are split by arm
            want_none = not case.extra_skills
            self.assertEqual("none" in texts, want_none, case.id)
            self.assertEqual("skill-instead" in texts, self.skill_instead, case.id)

    def test_tiger_style_cases_supply_the_skill_and_never_name_it(self) -> None:
        for case in self.cases_of("tiger-style-on-branches"):
            self.assertEqual(case.extra_skills, (SKILL,), case.id)
            assert case.prompt is not None
            self.assertNotIn("tiger", case.prompt.lower(), case.id)

    def test_every_case_is_planted_and_flips_between_compliant_and_violating_runs(self) -> None:
        self.assertEqual({c.id for c in self.dataset.cases}, set(self.planted))
        for case in self.dataset.cases:
            good_events, good_text, bad_events, bad_text = self.planted[case.id]
            for events, text, want in (
                (good_events, good_text, True),
                (bad_events, bad_text, False),
            ):
                grades = grade_run(make_view(events, text), case.assertions, self.root)
                got = case_passed(grades, case.assertions, "ok")
                detail = [(g.assertion_id, g.verdict, g.evidence) for g in grades]
                self.assertEqual(got, want, f"{case.id}: {detail}")


class AuthoredCasesTest(AuthoredCasesMixin, unittest.TestCase):
    suites = SUITES
    planted = PLANTED


if __name__ == "__main__":
    unittest.main()
