"""The shipped-kit home builder behind `eval kit-home`, one subcommand.

The kit as installed is the agent-kit checkout this tool ships inside: every plugin
rule (rules/*.1.json, the additionalContext a SessionStart hook injects) becomes
steering/<name>.md, the home's CLAUDE.md imports the lot (Claude's always-on set),
and every plugin skill is copied. The rerun then passes the home as
`eval run --home DIR --shipped`, and the runner copies the kit's skills into every
arm (lib.home.plan_home)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from lib.dataset import as_dict, discover_world, files_under, load_limits, tree_sha
from lib.home import copy_skill_dir
from lib.run import RunLimits, load_run_limits

KIT_HOME_USAGE: Final = "usage: eval kit-home --out DIR [--kit DIR]"
RULE_SUFFIX: Final = ".1.json"
CLAUDE_IMPORT: Final = "@~/.claude/steering/{}.md"
STATUS_EXIT_BAD_DATA: Final = 1
STATUS_EXIT_USAGE: Final = 2


def parse_kit_home_args(argv: list[str], evals_root: Path) -> tuple[Path, Path] | str:
    """(--out dir, kit root) or a usage text. The kit defaults to the checkout this tool
    ships inside (agent-kit/tools/evals -> agent-kit); a private tree has no plugins/ there
    and must name one with --kit."""
    out: str | None = None
    kit: str | None = None
    rest, index = argv, 0
    while index < len(rest):  # at most 4 args, all flag-value pairs
        flag = rest[index]
        if flag in ("--out", "--kit") and index + 1 < len(rest):
            if flag == "--out":
                out = rest[index + 1]
            else:
                kit = rest[index + 1]
            index += 2
        else:
            return KIT_HOME_USAGE
    if out is None or (kit is not None and kit.strip() == ""):
        return KIT_HOME_USAGE
    kit_root = Path(kit).expanduser() if kit else evals_root.parent.parent
    return Path(out).expanduser(), kit_root


def build_shipped_home(kit_root: Path, dest: Path, files_max: int) -> str | None:
    """Fill the empty dir `dest` with the kit as installed; a str is the reason it failed."""
    assert dest.is_dir() and not any(dest.iterdir()) and files_max > 0, "dest is empty, bound set"
    plugin_dir = kit_root / "plugins"
    if not plugin_dir.is_dir():
        return f"{kit_root} has no plugins/"
    rule_files = sorted(plugin_dir.glob(f"*/rules/*{RULE_SUFFIX}"))
    skill_dirs = sorted(plugin_dir.glob("*/skills/*/"))
    if not rule_files:
        return f"{plugin_dir} holds no rules"
    names = [path.name.removesuffix(RULE_SUFFIX) for path in rule_files]
    assert len(names) == len(rule_files), "one rule per file"
    if len(set(names)) != len(names):
        doubles = {name for name in names if names.count(name) > 1}
        return f"two plugins ship a rule named the same: {sorted(doubles)}"
    for rule_path, name in zip(rule_files, names, strict=True):
        try:
            value: object = json.loads(rule_path.read_text(encoding="utf-8"))
        except ValueError:
            return f"{rule_path} is not JSON"
        frame = as_dict(value)
        hook = as_dict(frame.get("hookSpecificOutput")) if frame is not None else None
        data = hook.get("additionalContext") if hook is not None else None
        if not isinstance(data, str) or not data.startswith(f"rule: {name}\n"):
            return f"{rule_path} must inject a rule named {name}"
        (dest / "steering").mkdir(exist_ok=True)
        (dest / "steering" / f"{name}.md").write_text(data.strip() + "\n", encoding="utf-8")
    import_lines = [CLAUDE_IMPORT.format(name) for name in names]
    (dest / "CLAUDE.md").write_text("\n".join(import_lines) + "\n", encoding="utf-8")
    for skill in skill_dirs:
        if (dest / "skills" / skill.name).exists():
            return f"two plugins ship a skill named {skill.name}"
        problem = copy_skill_dir(skill, dest / "skills" / skill.name, files_max)
        if problem:
            return problem
    if tree_sha(dest, files_max) is None:
        return f"built home has more than {files_max} files"
    return None


def run_kit_home(argv: list[str], evals_root: Path) -> tuple[list[str], int]:
    """`eval kit-home --out DIR [--kit DIR]`: builds the home, then discovers it the same
    way `eval run --home` will, so a home the runner would reject never prints done."""
    parsed = parse_kit_home_args(argv, evals_root)
    if isinstance(parsed, str):
        return [parsed], STATUS_EXIT_USAGE
    out, kit_root = parsed
    limits: RunLimits = load_run_limits(evals_root / "limits.json")
    if not out.exists():
        return [f"kit-home: {out} does not exist"], STATUS_EXIT_BAD_DATA
    if not out.is_dir() or any(out.iterdir()):
        return [f"kit-home: {out} must be an empty directory"], STATUS_EXIT_BAD_DATA
    problem = build_shipped_home(kit_root, out, limits.home_files_max)
    if problem is not None:
        return [f"kit-home: {problem}"], STATUS_EXIT_BAD_DATA
    world = discover_world(out, evals_root, load_limits(evals_root / "limits.json"))
    assert world.claude, "a shipped home is a Claude home"
    assert world.always_on_names == world.steering_names, "every shipped rule is always on"
    files = files_under(out, limits.home_files_max) or []
    lines = [
        f"kit-home: out={out}",
        f"kit-home: kit={kit_root} rules={len(world.steering_names)} "
        f"skills={len(world.skill_names)} files={len(files)}",
        "kit-home: done",
    ]
    return lines, 0
