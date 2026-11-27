"""Keep a Claude eval run inside its two throwaway folders: path-scoped file tools, and a
sandbox for Bash that can't read the real home. Probed 2026-11-20 (design: evals-isolation)."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Final

READ_TOOLS: Final = ("Read",)  # reads reach the home too: skill reference files live there
WRITE_TOOLS: Final = ("Write", "Edit")  # cwd only, so a run can't change its own skills
PASS_THROUGH_TOOLS: Final = ("Bash", "Skill")  # Bash is caged by the sandbox, Skill by the home
GLOB_CHARS: Final = frozenset("*?[]{}")
# uv's managed Pythons; never ~/.local/bin, which holds the user's own scripts
TOOLCHAIN_READ_DIRS: Final = ("~/.local/share/uv",)
SANDBOX_BLOCKED_TEXT: Final = "Operation not permitted"
# the runner's own venv lives under ~; inherited, it sends the agent's uv into a denied folder
ENV_DROP_KEYS: Final = ("VIRTUAL_ENV",)


def _require(ok: bool, why: str) -> None:
    """An assert that survives python -O: these checks hold the sandbox shut."""
    if not ok:
        raise ValueError(why)


def _rule_path(path: Path) -> str:
    """The absolute form a permission rule wants: //<path>/**."""
    real = os.path.realpath(path)
    _require(os.path.isabs(real), "rule paths are absolute")
    _require(not (GLOB_CHARS & set(real)), f"a glob character in {real!r} would widen the rule")
    _require(not _under_real_home(real), f"{real!r} is under the real home")
    return f"/{real}/**"


def _under_real_home(real: str) -> bool:
    home = os.path.realpath(Path.home())
    return real == home or real.startswith(home + os.sep)


def scoped_allowed_tools(names: tuple[str, ...], cwd: Path, home: Path) -> tuple[str, ...]:
    """--allowedTools rules: file tools limited to cwd (and home for reads), never bare.
    An unknown tool raises: it must be classified before a run may use it."""
    known = (*READ_TOOLS, *WRITE_TOOLS, *PASS_THROUGH_TOOLS)
    unknown = [name for name in names if name not in known]
    if unknown:
        raise ValueError(f"unclassified tools {unknown}; add them to lib/isolation.py first")
    cwd_rule, home_rule = _rule_path(cwd), _rule_path(home)
    rules: list[str] = []
    for name in names:
        if name in READ_TOOLS:
            rules += [f"{name}({cwd_rule})", f"{name}({home_rule})"]
        elif name in WRITE_TOOLS:
            rules.append(f"{name}({cwd_rule})")
        else:
            rules.append(name)
    _require(not set(rules) & {*READ_TOOLS, *WRITE_TOOLS}, "no bare file tool leaves here")
    assert len(rules) >= len(names), "every name became at least one rule"
    return tuple(rules)


def sandbox_settings(cache_dir: Path) -> str:
    """--settings JSON: Bash sandboxed, can't turn the sandbox off, can't read ~ (P11, P12, P17)."""
    real = os.path.realpath(cache_dir)
    _require(os.path.isabs(real) and not _under_real_home(real), "cache dir is outside the home")
    settings = {
        "sandbox": {
            "enabled": True,
            "allowUnsandboxedCommands": False,
            "filesystem": {
                "denyRead": ["~"],
                "allowRead": list(TOOLCHAIN_READ_DIRS),
                "allowWrite": [real],
            },
        }
    }
    text = json.dumps(settings, sort_keys=True)
    _require('"allowUnsandboxedCommands": false' in text, "the escape hatch is shut")
    return text


def isolation_env(cache_dir: Path) -> dict[str, str]:
    """Env for the run: caches into the throwaway cache dir, no global git config (P15, P16)."""
    real = os.path.realpath(cache_dir)
    _require(os.path.isabs(real), "cache dir is absolute")
    env = {"UV_CACHE_DIR": os.path.join(real, "uv"), "XDG_CACHE_HOME": real}
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    assert all(value for value in env.values()), "every variable is set"
    return env


def isolated_environ(base: Mapping[str, str], cache_dir: Path) -> dict[str, str]:
    """base minus ENV_DROP_KEYS, plus isolation_env: the environment a Claude run gets."""
    env = {key: value for key, value in base.items() if key not in ENV_DROP_KEYS}
    env.update(isolation_env(cache_dir))
    _require(not set(ENV_DROP_KEYS) & set(env), "dropped keys stay dropped")
    return env
