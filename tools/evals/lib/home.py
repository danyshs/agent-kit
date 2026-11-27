"""Per-run home builder (design 3.3): KIRO_HOME for Kiro, a plugin dir plus rules for Claude.

A run's home holds only what its arm names: skills and steering copied from the real home,
never hooks, never mcp.json (release 1 has no MCP suites). Expected failures come back as str.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from lib.dataset import (
    FENCE_LINE,
    SKILL_INSTEAD_ARM,
    VALUE_ARMS,
    VALUE_SKILL,
    Arm,
    Target,
    files_under,
    tree_sha,
)

SKILL_FILE: Final = "SKILL.md"
VALUE_RULE: Final = "tiger-style-on-branches"  # the always-on rule the skill+rule arm adds
FORBIDDEN_IN_HOME: Final = ("hooks", "mcp.json")
# a Claude run loads the home as a plugin (--plugin-dir): its skills dir needs this manifest
PLUGIN_MANIFEST: Final = Path(".claude-plugin") / "plugin.json"
PLUGIN_NAME: Final = "eval-arm"
CLAUDE_MD: Final = "CLAUDE.md"  # present: rules load as memory (--add-dir); absent: system prompt
SKILL_ARMS: Final = ("with", "without", "previous")
STEERING_ARMS: Final = ("full", "minus", SKILL_INSTEAD_ARM)


@dataclass(frozen=True)
class HomeDir:
    dir_text: str
    home_sha: str


@dataclass(frozen=True)
class HomePlan:
    skills: tuple[str, ...]  # skill dir names copied from the source home
    steering: tuple[str, ...]  # steering file stems copied from the source home
    snapshot_skill: str | None  # skill whose dir comes from the snapshot, not the source home
    rule_skill: str | None = None  # skill-instead: this steering file is written as a skill


def _plan_value_home(
    arm: Arm, target: Target, extra_steering: tuple[str, ...], extra_skills: tuple[str, ...]
) -> HomePlan | str:
    """V2 arms (6.3). none-ts: no skill, no rule. skill: skill, no rule. skill+rule: both.
    Everything else the case lists (other steering, ponytail) stays in every arm."""
    if target.kind != "skill" or target.name != VALUE_SKILL:
        return f"arm {arm.kind!r} fits only the {VALUE_SKILL} skill"
    with_skill, with_rule = arm.kind != "none-ts", arm.kind == "skill+rule"
    skills = set(extra_skills) - {VALUE_SKILL}
    steering = set(extra_steering) - {VALUE_RULE}
    if with_skill:
        skills.add(VALUE_SKILL)
    if with_rule:
        steering.add(VALUE_RULE)
    assert (VALUE_SKILL in skills) == with_skill, "the skill is present iff the arm says so"
    assert (VALUE_RULE in steering) == with_rule, "the rule is present iff the arm says so"
    return HomePlan(tuple(sorted(skills)), tuple(sorted(steering)), None)


def plan_home(
    arm: Arm,
    target: Target,
    always_on: frozenset[str],
    extra_steering: tuple[str, ...],
    extra_skills: tuple[str, ...],
) -> HomePlan | str:
    """What goes in the home; a str is the reason this arm does not fit the target."""
    assert target.kind in ("skill", "steering", "tool_group"), "target kind is known"
    if arm.kind == "none":
        return HomePlan((), (), None)  # none = the empty home, extras ignored (3.3)
    if arm.kind in VALUE_ARMS:
        return _plan_value_home(arm, target, extra_steering, extra_skills)
    if target.kind == "skill":
        if arm.kind not in SKILL_ARMS:
            return f"arm {arm.kind!r} does not fit a skill target"
        others = tuple(sorted(set(extra_skills) - {target.name}))
        skills = others if arm.kind == "without" else tuple(sorted({*others, target.name}))
        snapshot = target.name if arm.kind == "previous" else None
        return HomePlan(skills, tuple(sorted(set(extra_steering))), snapshot)
    if arm.kind not in STEERING_ARMS:
        return f"arm {arm.kind!r} does not fit a steering target"
    steering = set(always_on) | set(extra_steering)
    if arm.kind == "minus":
        assert arm.minus_file is not None, "a minus arm names its file"
        steering.discard(arm.minus_file)
    assert arm.kind != "full" or always_on <= steering, "full keeps every always-on file"
    skills = tuple(sorted(set(extra_skills)))
    if arm.kind != SKILL_INSTEAD_ARM:
        return HomePlan(skills, tuple(sorted(steering)), None)
    if target.kind != "steering" or target.name not in steering:
        return f"arm {arm.kind!r} needs a steering target the full arm loads"
    if target.name in skills:
        return f"arm {arm.kind!r}: a skill named {target.name!r} is already in the home"
    steering.discard(target.name)
    return HomePlan(skills, tuple(sorted(steering)), None, rule_skill=target.name)


def _copy_dir(src: Path, dest: Path, files_max: int) -> str | None:
    """Copy a skill dir without following symlinks; a str is the reason it could not be."""
    assert files_max > 0 and not dest.exists(), "files_max positive, dest is a fresh path"
    if not (src / SKILL_FILE).is_file():
        return f"{src} has no {SKILL_FILE}"
    files = files_under(src, files_max)
    if files is None:
        return f"{src} has more than {files_max} files"
    shutil.copytree(src, dest, symlinks=False, ignore=shutil.ignore_patterns("__pycache__"))
    assert (dest / SKILL_FILE).is_file(), "the copy has its SKILL.md"
    return None


def _populate(
    plan: HomePlan, src_home: Path, snapshot_dir: Path | None, dest: Path, files_max: int
) -> str | None:
    assert dest.is_dir() and files_max > 0, "dest exists and the file bound is positive"
    assert plan.snapshot_skill is None or plan.snapshot_skill in plan.skills, "snapshot is copied"
    for name in plan.skills:
        src = (
            snapshot_dir
            if name == plan.snapshot_skill and snapshot_dir
            else src_home / "skills" / name
        )
        problem = _copy_dir(src, dest / "skills" / name, files_max)
        if problem:
            return problem
    for name in plan.steering:
        src_file = src_home / "steering" / f"{name}.md"
        if not src_file.is_file():
            return f"steering file {src_file} does not exist"
        (dest / "steering").mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dest / "steering" / src_file.name)
    return None


def _skill_text(name: str, description: str, rule_text: str) -> str:
    """A SKILL.md holding the rule's text verbatim; a Kiro rule's own frontmatter is dropped."""
    assert name and description and "\n" not in description, "one-line description"
    lines = rule_text.split("\n")
    body = rule_text
    if lines[0].strip() == FENCE_LINE:
        closing = [i for i, line in enumerate(lines[1:], start=1) if line.strip() == FENCE_LINE]
        body = "\n".join(lines[closing[0] + 1 :]).lstrip("\n") if closing else rule_text
    text = f'---\nname: {name}\ndescription: "{description}"\n---\n\n{body}'
    assert text.startswith(f"---\nname: {name}\n"), "frontmatter first"
    return text


def _write_rule_skill(src_home: Path, dest: Path, name: str, description: str | None) -> str | None:
    """skill-instead: the steering file as skills/<name>/SKILL.md; a str is why it could not be."""
    assert dest.is_dir() and name, "dest exists and the rule has a name"
    if not description:
        return f"arm {SKILL_INSTEAD_ARM!r} needs a skill description for {name!r}"
    src_file = src_home / "steering" / f"{name}.md"
    if not src_file.is_file():
        return f"steering file {src_file} does not exist"
    skill_dir = dest / "skills" / name
    assert not skill_dir.exists(), "plan_home keeps the name out of the copied skills"
    skill_dir.mkdir(parents=True)
    text = _skill_text(name, description, src_file.read_text(encoding="utf-8"))
    (skill_dir / SKILL_FILE).write_text(text, encoding="utf-8")
    return None


def _write_claude_md(dest: Path, steering: tuple[str, ...]) -> None:
    """A CLAUDE.md holding the arm's steering text. Real use imports the files (@path), but an
    import in an --add-dir CLAUDE.md is not expanded (probe, 2026-11-20): the text goes inline."""
    assert steering and (dest / "steering").is_dir(), "there are rules to inline"
    parts = [(dest / "steering" / f"{name}.md").read_text(encoding="utf-8") for name in steering]
    (dest / CLAUDE_MD).write_text("\n\n".join(parts), encoding="utf-8")
    assert len(parts) == len(steering), "one part per rule file"


def build_home(
    arm: Arm,
    target: Target,
    snapshot_dir: Path | None,
    extra_steering: tuple[str, ...],
    extra_skills: tuple[str, ...],
    *,
    src_home: Path,
    dest: Path,
    always_on: frozenset[str],
    files_max: int,
    plugin: bool = False,
    claude_md: bool = False,
    rule_skill_description: str | None = None,
) -> HomeDir | str:
    """Fill the empty dir `dest` for this arm; a str is the reason it could not be built."""
    assert dest.is_dir() and not any(dest.iterdir()), "dest must be an empty directory"
    assert src_home.is_dir(), "src_home must be a directory"
    plan = plan_home(arm, target, always_on, extra_steering, extra_skills)
    if isinstance(plan, str):
        return plan
    if plan.snapshot_skill and snapshot_dir is None:
        return "the previous arm needs a snapshot dir"
    problem = _populate(plan, src_home, snapshot_dir, dest, files_max)
    if not problem and plan.rule_skill:
        problem = _write_rule_skill(src_home, dest, plan.rule_skill, rule_skill_description)
    if problem:
        return problem
    if plugin:
        (dest / PLUGIN_MANIFEST).parent.mkdir()
        (dest / PLUGIN_MANIFEST).write_text(json.dumps({"name": PLUGIN_NAME}), encoding="utf-8")
    if claude_md and plan.steering:
        _write_claude_md(dest, plan.steering)
    assert not any((dest / name).exists() for name in FORBIDDEN_IN_HOME), "no hooks, no mcp.json"
    sha = tree_sha(dest, files_max)
    if sha is None:
        return f"built home has more than {files_max} files"
    return HomeDir(dir_text=str(dest), home_sha=sha)
