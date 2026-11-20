"""Builders for a throwaway kiro home and dataset root; the real ~/.kiro is never touched."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from lib.cli import EVALS_ROOT
from lib.dataset import Limits, World, discover_world, load_limits

SKILL_TEXT = "---\nname: alpha\ndescription: Does alpha things.\n---\n\n# alpha\n"
STEERING_ALWAYS = "---\ninclusion: always\n---\n\n# rule a\n"
STEERING_MANUAL = "---\ninclusion: manual\n---\n\n# rule b\n"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def build_tree(tmp: Path) -> tuple[Path, Path]:
    """(kiro_home, root): skill alpha, always-on rule-a, manual rule-b, planted/hidden dirs."""
    kiro_home, root = tmp / "kiro", tmp / "evals"
    (kiro_home / "skills" / "alpha").mkdir(parents=True)
    (kiro_home / "skills" / "alpha" / "SKILL.md").write_text(SKILL_TEXT, encoding="utf-8")
    (kiro_home / "steering").mkdir()
    (kiro_home / "steering" / "rule-a.md").write_text(STEERING_ALWAYS, encoding="utf-8")
    (kiro_home / "steering" / "rule-b.md").write_text(STEERING_MANUAL, encoding="utf-8")
    root.mkdir()
    for name in ("limits.json", "known_tools.json", "trust_names.json", "tool_groups.json"):
        shutil.copy(EVALS_ROOT / name, root / name)
    write_json(root / "lint_baseline.json", [])
    (root / "planted").mkdir()
    (root / "planted" / "fx.json").write_text('{"items": []}', encoding="utf-8")
    (root / "hidden" / "task").mkdir(parents=True)
    (root / "skills" / "alpha" / "fixtures").mkdir(parents=True)
    (root / "skills" / "alpha" / "fixtures" / "a.py").write_text("x = 1\n", encoding="utf-8")
    return kiro_home, root


def world_and_limits(kiro_home: Path, root: Path) -> tuple[World, Limits]:
    limits = load_limits(root / "limits.json")
    return discover_world(kiro_home, root, limits), limits


def assertion(
    grader: str = "g_tool_used",
    args: dict[str, object] | None = None,
    *,
    hard: bool = True,
    assertion_id: str = "a1",
) -> dict[str, object]:
    chosen: dict[str, object] = {"tool": "read_file"} if args is None else args
    return {"id": assertion_id, "grader": grader, "args": chosen, "hard": hard}


def steering_case(case_id: str = "c1", role: str = "fire", **over: object) -> dict[str, object]:
    case: dict[str, object] = {
        "id": case_id,
        "target": "rule-a",
        "target_kind": "steering",
        "rule_id": "r1",
        "role": role,
        "prompt": "do the thing",
        "trust_tools": ["read_file"],
        "arms": ["full", "none"],
        "assertions": [assertion()],
    }
    case.update(over)
    return case


def skill_case(case_id: str = "s1", role: str = "fire", **over: object) -> dict[str, object]:
    case: dict[str, object] = {
        "id": case_id,
        "target": "alpha",
        "target_kind": "skill",
        "role": role,
        "prompt": "use alpha",
        "trust_tools": [],
        "arms": ["with", "without"],
        "assertions": [assertion("g_skill_read", {"skill": "alpha"})],
    }
    case.update(over)
    return case


def rule(rule_id: str = "r1", grader: str = "g_tool_used") -> dict[str, str]:
    return {
        "id": rule_id,
        "source_file": "rule-a",
        "trigger_situation": "when asked",
        "observable": "a read call",
        "grader": grader,
    }
