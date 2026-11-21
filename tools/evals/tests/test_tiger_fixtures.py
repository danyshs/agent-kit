"""Goal: the tiger-style suite data is well-formed and its planted answer keys point at the
defects (design 3.4.1, 6, 9). Method: load the real files and check structure; every anchor regex
must match the planted line; a perfect synthetic answer passes recall and one citing line + 10
fails. Not a lint: a wrong planted line makes the review score meaningless."""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from typing import cast

from lib.cli import EVALS_ROOT
from lib.dataset import discover_world, load_cases, load_limits
from lib.grade import GRADERS, load_extras, load_planted
from tests.helpers_run import make_view

SUITE = EVALS_ROOT / "skills" / "tiger-style"
FIXTURES = SUITE / "fixtures"
PLANTED_NAMES = ("rev_py_planted", "rev_fish_planted", "rev_lua_planted")
ANCHORS = {  # fixture -> item id -> regex the planted line must match
    "rev_py_planted": {
        "p1": r"^\s*while True:",
        "p2": r"^def prune\(",
        "p3": r"^TIMEOUT = ",
        "p4": r"ordered\[-count - 1 :\]",
        "p5": r"shell=True",
    },
    "rev_lua_planted": {
        "p1": r"^\s*while true do",
        "p2": r"^function M\.clear\(",
        "p3": r"^local TIMEOUT = ",
        "p4": r"local first = #lines - count",
        "p5": r'os\.execute\("rm -rf "',
    },
    "rev_fish_planted": {
        "p1": r"^\s*while true",
        "p2": r"^function _archive ",
        "p3": r"SHIP_TIMEOUT",
        "p4": r"math \$count \+ 1",
        "p5": r"^\s*eval rm -rf",
    },
}
EXTRA_ANCHORS = {  # fixture -> acceptable extra id -> regex the extra's line must match (v2 key)
    "rev_py_planted": {
        "x1": r"assert url\.startswith",
        "x2": r'open\(cache_path\(name\), "wb"\)',
        "x3": r"for _ in range\(1_000_000\)",
        "x4": r"for _ in range\(1_000_000\)",
        "x5": r"except OSError",
        "x6": r"os\.listdir",
        "x7": r"\bfor n in names\b",
        "x8": r"def newest",
        "x9": r"subprocess\.run",
    },
    "rev_lua_planted": {
        "x1": r'name:find\("/", 1, true\)',
        "x2": r"assert\(MAX_LINES > 0",
        "x3": r"if not file then",
        "x4": r"if #lines >= MAX_LINES",
        "x5": r"return ok ~= nil",
        "x6": r"assert\(type\(count\) == \"number\"",
        "x7": r"assert\(TIMEOUT > 0",
    },
    "rev_fish_planted": {
        "x1": r"ls -tr",
        "x2": r"^function _wait_for_log",
        "x3": r"count is a number",
        "x4": r"set -l names \(_log_names\)",
        "x5": r"mv \$LOG_DIR",
        "x6": r"for name in \(_oldest 5\)",
        "x7": r"for name in \(_oldest 5\)",
    },
}
FORBIDDEN_WORDS = ("planted", "violation", "bug", "vulnerable", "TODO", "FIXME")
TRIGGER_PHRASES = (
    "tiger style",
    "tigerstyle",
    "tiger-style",
    "assertion density",
    "bounded loops",
    "power of ten",
    "units-in-names",
)
NAMED_STYLE_RE = re.compile(r"tiger style|tigerstyle|tiger-style", re.IGNORECASE)
PLANTED_PER_FIXTURE = 5
IN_CATEGORY_PER_FIXTURE = 3
CELL_SIZE = 8


def post_image_lines(fixture: str, file: str) -> list[str]:
    return (FIXTURES / fixture / file).read_text(encoding="utf-8").splitlines()


def diff_added_lines(fixture: str) -> dict[str, list[str]]:
    """file -> its added lines, from change.diff."""
    result: dict[str, list[str]] = {}
    current = ""
    for line in (FIXTURES / fixture / "change.diff").read_text(encoding="utf-8").splitlines():
        if line.startswith("+++ b/"):
            current = line[len("+++ b/") :]
            result[current] = []
        elif line.startswith("+") and current:
            result[current].append(line[1:])
    return result


class PlantedFilesTest(unittest.TestCase):
    def test_each_item_points_at_its_defect(self) -> None:
        for name in PLANTED_NAMES:
            items = load_planted(EVALS_ROOT / "planted" / f"{name}.json")
            assert not isinstance(items, str), items
            self.assertEqual(len(items), PLANTED_PER_FIXTURE, name)
            self.assertEqual(sum(i.in_category for i in items), IN_CATEGORY_PER_FIXTURE, name)
            for item in items:
                lines = post_image_lines(name, item.file)
                self.assertLessEqual(item.line, len(lines), f"{name} {item.id}")
                anchor = ANCHORS[name][item.id]
                self.assertRegex(lines[item.line - 1], anchor, f"{name} {item.id} line moved")

    def test_a_wrong_anchor_would_be_caught(self) -> None:
        lines = post_image_lines("rev_py_planted", "src/mirror.py")
        self.assertNotRegex(lines[10 - 1 + 5], ANCHORS["rev_py_planted"]["p3"], "planted twin")

    def test_each_acceptable_extra_points_at_its_issue(self) -> None:
        for name in PLANTED_NAMES:
            path = EVALS_ROOT / "planted" / f"{name}.json"
            extras = load_extras(path)
            assert not isinstance(extras, str), extras
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["fixture_version"], 2, name)
            self.assertEqual({e.id for e in extras}, set(EXTRA_ANCHORS[name]), name)
            for extra in extras:
                lines = post_image_lines(name, extra.file)
                self.assertLessEqual(extra.line, len(lines), f"{name} {extra.id}")
                anchor = EXTRA_ANCHORS[name][extra.id]
                self.assertRegex(lines[extra.line - 1], anchor, f"{name} {extra.id} line moved")

    def test_clean_control_has_no_items(self) -> None:
        self.assertEqual(load_planted(EVALS_ROOT / "planted" / "rev_py_clean.json"), [])

    def test_severities_follow_the_category(self) -> None:
        for name in PLANTED_NAMES:
            raw = json.loads((EVALS_ROOT / "planted" / f"{name}.json").read_text(encoding="utf-8"))
            for item in cast(list[dict[str, object]], raw["items"]):
                expected = {
                    "unitless_name": ["P2", "P3"],
                    "shell_injection": ["P0"],
                }.get(cast(str, item["category"]), ["P0", "P1"])
                self.assertEqual(item["allowed_severities"], expected, f"{name} {item['id']}")


class DiffTest(unittest.TestCase):
    def test_change_diff_adds_exactly_the_files_content(self) -> None:
        for name in (*PLANTED_NAMES, "rev_py_clean"):
            added = diff_added_lines(name)
            self.assertTrue(added, name)
            for file, lines in added.items():
                self.assertEqual(lines, post_image_lines(name, file), f"{name} {file}")

    def test_diff_is_a_new_file_diff_so_post_image_lines_start_at_one(self) -> None:
        for name in (*PLANTED_NAMES, "rev_py_clean"):
            text = (FIXTURES / name / "change.diff").read_text(encoding="utf-8")
            self.assertEqual(text.count("--- /dev/null"), text.count("@@ -0,0 +1,"), name)

    def test_fixture_text_does_not_hint_at_the_defects(self) -> None:
        for name in (*PLANTED_NAMES, "rev_py_clean"):
            for path in (FIXTURES / name).rglob("*"):
                if path.is_file():
                    text = path.read_text(encoding="utf-8").lower()
                    for word in FORBIDDEN_WORDS:
                        self.assertNotIn(word.lower(), text, f"{path.name}: {word}")

    def test_file_sizes_are_in_the_documented_range(self) -> None:
        for name in PLANTED_NAMES:
            added = diff_added_lines(name)
            for file, lines in added.items():
                self.assertTrue(50 <= len(lines) <= 90, f"{name} {file} has {len(lines)} lines")


class RecallTest(unittest.TestCase):
    def grade(self, name: str, offset: int) -> str:
        items = load_planted(EVALS_ROOT / "planted" / f"{name}.json")
        assert not isinstance(items, str)
        text = "".join(f"FINDING P0 {i.file}:{i.line + offset} x\n" for i in items)
        view = make_view([], final_text=text + "FINDINGS_DONE\n")
        args: dict[str, object] = {"planted": f"planted/{name}.json", "mode": "recall", "min": 1.0}
        return GRADERS["g_planted_found"](view, args, EVALS_ROOT)[0]

    def test_perfect_answer_passes_and_line_plus_ten_fails(self) -> None:
        for name in PLANTED_NAMES:
            self.assertEqual(self.grade(name, 0), "PASS", name)
            self.assertEqual(self.grade(name, 10), "FAIL", name)


SKILL_TEXT = "---\nname: tiger-style\ndescription: Style.\n---\n\n# tiger-style\n"


class CasesFileTest(unittest.TestCase):
    def setUp(self) -> None:
        # a throwaway home holding just the skill, so this runs the same on any machine
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        (home / "steering").mkdir()
        (home / "skills" / "tiger-style").mkdir(parents=True)
        (home / "skills" / "tiger-style" / "SKILL.md").write_text(SKILL_TEXT, encoding="utf-8")
        limits = load_limits(EVALS_ROOT / "limits.json")
        world = discover_world(home, EVALS_ROOT, limits)
        self.cases, rejections = load_cases(
            SUITE / "cases.json", "skill", frozenset(), world, limits
        )
        self.assertEqual(rejections, [])
        self.limits = limits

    def prompts(self, prefix: str) -> list[str]:
        return [c.prompt for c in self.cases if c.id.startswith(prefix) and c.prompt is not None]

    def test_counts_and_unique_ids(self) -> None:
        self.assertEqual(len(self.cases), 58)
        self.assertEqual(len({c.id for c in self.cases}), 58)
        cells = ("trig-pos-dev-", "trig-pos-held-", "trig-neg-dev-", "trig-neg-held-")
        for cell in cells + ("trig-pos-held2-", "trig-neg-held2-"):
            self.assertEqual(len(self.prompts(cell)), CELL_SIZE, cell)

    def test_positives_carry_a_trigger_phrase_and_negatives_do_not(self) -> None:
        for case in self.cases:
            if case.id.startswith("trig-pos-") and "-held2-" not in case.id:
                prompt = (case.prompt or "").lower()
                self.assertTrue(any(p in prompt for p in TRIGGER_PHRASES), case.id)
        for prompt in self.prompts("trig-pos-held2-"):  # topic-only: never names the style
            self.assertIsNone(NAMED_STYLE_RE.search(prompt), prompt)
        for prompt in self.prompts("trig-neg"):
            self.assertIsNone(NAMED_STYLE_RE.search(prompt), prompt)
            if "tigerbeetle" in prompt.lower():
                self.assertNotIn("apply", prompt.lower(), prompt)

    def test_no_prompt_repeats_across_the_splits(self) -> None:
        prompts = [c.prompt for c in self.cases]
        self.assertEqual(len(set(prompts)), len(prompts))

    def test_activation_cases_run_one_arm_and_the_rest_two(self) -> None:
        for case in self.cases:
            arms = [a.kind for a in case.arms]
            expected = ["with"] if case.id.startswith("trig-") else ["with", "without"]
            self.assertEqual(arms, expected, case.id)

    def test_python_write_cases_have_the_five_mechanical_graders(self) -> None:
        wanted = {
            "g_function_length_max",
            "g_assertions_per_function_min",
            "g_loops_bounded",
            "g_units_in_names",
            "g_pyright_clean",
        }
        for case in self.cases:
            graders = {a.grader for a in case.assertions}
            if case.id.startswith("wr-py"):
                self.assertEqual(graders, wanted, case.id)
            elif case.id.startswith("wr-"):
                self.assertEqual(graders, {"g_tool_used", "g_text_matches", "g_text_absent"})

    def test_unit_lists_carry_every_suffix_the_skill_names(self) -> None:
        time_suffixes = {"_ms", "_s", "_seconds"}  # skill: _ms, _s; _seconds added in v2
        size_suffixes = {"_bytes", "_gb"}  # skill: _bytes, _gb
        seen = 0
        for case in self.cases:
            for assertion in case.assertions:
                if assertion.grader != "g_units_in_names":
                    continue
                for entry in cast(list[dict[str, list[str]]], assertion.args["names"]):
                    wanted = size_suffixes if "_bytes" in entry["suffixes"] else time_suffixes
                    self.assertEqual(set(entry["suffixes"]), wanted, case.id)
                    seen += 1
        self.assertEqual(seen, 7, "wr-py-01..04 name 7 identifiers")

    def test_fish_and_lua_unit_regexes_accept_seconds_and_reject_bare(self) -> None:
        for case_id, stem in (("wr-fish-01", "timeout"), ("wr-lua-01", "delay")):
            case = next(c for c in self.cases if c.id == case_id)
            regex = str(next(a for a in case.assertions if a.id == "unit_suffix").args["regex"])
            for good in ("_ms", "_s", "_seconds"):
                self.assertRegex(f"set {stem}{good} 3", regex, f"{case_id} {good}")
            self.assertNotRegex(f"set {stem} 3", regex, f"{case_id}: planted twin, no suffix")
            self.assertNotRegex(f"set {stem}_secs 3", regex, f"{case_id}: unlisted spelling")

    def test_prompts_fit_the_byte_limit(self) -> None:
        for case in self.cases:
            prompt = case.prompt or ""
            self.assertLessEqual(len(prompt.encode()), self.limits.prompt_bytes_max, case.id)

    def test_review_cases_point_at_real_planted_files(self) -> None:
        for case in self.cases:
            if not case.id.startswith("rev-"):
                continue
            self.assertIsNotNone(case.fixture, case.id)
            self.assertTrue((EVALS_ROOT / str(case.fixture) / "change.diff").is_file(), case.id)


if __name__ == "__main__":
    unittest.main()
