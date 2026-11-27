"""Eval dataset: limits, validation, coverage and static lint (design sections 4 and 5.1).

No model calls. Expected failures (bad dataset file) come back as Rejection values;
a broken limits.json or an unreadable target dir raises ValueError for the CLI boundary.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, cast

ID_RE: Final = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
FENCE_LINE: Final = "---"
FRONTMATTER_KEY_RE: Final = re.compile(r"([A-Za-z_][A-Za-z0-9_-]*):[ \t]*(.*)")
CLAUDE_MD: Final = "CLAUDE.md"
CLAUDE_IMPORT_RE: Final = re.compile(r"^@~/\.claude/steering/([A-Za-z0-9_.-]+)\.md\s*$")
SKILL_REF_RE: Final = re.compile(
    r"(?:references|assets|agents)/[A-Za-z0-9_./-]*[A-Za-z0-9_]\.[A-Za-z0-9]+"
)
BLOCK_SCALAR_MARKERS: Final = frozenset({">", ">-", "|", "|-"})
INCLUSION_VALUES: Final = frozenset({"always", "manual", "fileMatch", "auto"})
ROLES: Final = ("fire", "no_fire", "pressure")
CASE_TIERS: Final = ("smoke", "standard", "deep")
TAGS: Final = ("capability", "regression")
SKILL_ARMS: Final = ("with", "without", "previous", "none")
STEERING_ARMS: Final = ("full", "none", "skill-instead")
SKILL_INSTEAD_ARM: Final = "skill-instead"  # the target rule leaves the rules and loads as a skill
# a steering suite that uses skill-instead names the skill's description here: {"description": ...}
AS_SKILL_FILE: Final = "as_skill.json"
MINUS_PREFIX: Final = "minus-"
TARGET_KINDS: Final = ("skill", "steering", "tool_group")
HUMAN_GRADER: Final = "human"
# static-tier only: every denied attempt prints [denied] on stderr (design 3.5)
STATIC_ONLY_GRADERS: Final = frozenset({"g_stderr_empty"})
# every ArgSpec.kind _check_arg_value understands
ARG_KINDS: Final = frozenset(
    "string text skill tool tool_group tool_or_null int fraction enum regex regex_list "
    "units planted_path tests_dir identifier".split()
)
# V2 arms (design 6.3): only for skill cases targeting VALUE_SKILL, and always all three together
VALUE_ARMS: Final = ("none-ts", "skill", "skill+rule")
VALUE_SKILL: Final = "tiger-style"
SUITE_KINDS: Final = ("skill", "steering", "value")  # value suites live in value/<name>/
SUITE_DIRS: Final = {"skill": "skills", "steering": "steering", "value": "value"}
# vague phrases a human question may not hide behind (design section 4, G8 stand-in)
BANNED_PHRASES: Final = (
    "looks good",
    "is good",
    "high quality",
    "works well",
    "is reasonable",
    "correct output",
)

# json key in limits.json -> Limits field; keys outside this table belong to later steps
LIMIT_KEYS: Final = {
    "MAX_CASES_PER_SUITE": "cases_per_suite_max",
    "MAX_RUNS_PER_CASE": "runs_per_case_max",
    "MAX_ASSERTIONS_PER_CASE": "assertions_per_case_max",
    "MAX_PROMPT_BYTES": "prompt_bytes_max",
    "MAX_USER_TURNS": "user_turns_max",
    "MAX_FIXTURE_BYTES": "fixture_bytes_max",
    "DESCRIPTION_CHARS_MAX": "description_chars_max",
    "SKILL_BYTES_MAX": "skill_bytes_max",
    "STEERING_BYTES_MAX": "steering_bytes_max",
    "ALWAYS_ON_BYTES_MAX": "always_on_bytes_max",
    "FRONTMATTER_LINES_MAX": "frontmatter_lines_max",
    "TARGETS_PER_KIND_MAX": "targets_per_kind_max",
    "FIXTURE_FILES_MAX": "fixture_files_max",
    "RULES_PER_FILE_MAX": "rules_per_file_max",
    "ARGS_LIST_MAX": "args_list_max",
}


@dataclass(frozen=True)
class Limits:
    cases_per_suite_max: int
    runs_per_case_max: int
    assertions_per_case_max: int
    prompt_bytes_max: int
    user_turns_max: int
    fixture_bytes_max: int
    description_chars_max: int
    skill_bytes_max: int
    steering_bytes_max: int
    always_on_bytes_max: int
    frontmatter_lines_max: int
    targets_per_kind_max: int
    fixture_files_max: int
    rules_per_file_max: int
    args_list_max: int


@dataclass(frozen=True)
class Arm:
    kind: str  # with | without | previous | none | full | minus | skill-instead
    minus_file: str | None  # set iff kind == "minus"


@dataclass(frozen=True)
class Assertion:
    id: str
    grader: str
    args: dict[str, object]
    hard: bool


@dataclass(frozen=True)
class Case:
    id: str
    target: str
    target_kind: str
    rule_id: str | None
    role: str
    prompt: str | None
    user_turns: tuple[str, ...]
    arm_prompts: tuple[tuple[str, str], ...]  # (arm text, prompt): one prompt per arm (6.3)
    fixture: str | None
    trust_tools: tuple[str, ...]
    arms: tuple[Arm, ...]
    extra_skills: tuple[str, ...]
    extra_steering: tuple[str, ...]
    repeats: int | None
    tags: tuple[str, ...]
    tier: str | None
    assertions: tuple[Assertion, ...]
    rule_skill_description: str | None = None  # set iff an arm is skill-instead (as_skill.json)


@dataclass(frozen=True)
class Rule:
    id: str
    source_file: str
    trigger_situation: str
    observable: str
    grader: str


@dataclass(frozen=True)
class Rejection:
    file_text: str
    record_id: str | None
    reason: str


@dataclass(frozen=True)
class Target:
    name: str
    kind: str  # skill | steering | tool_group


@dataclass(frozen=True)
class LintFinding:
    path_text: str
    rule: str
    message: str


@dataclass(frozen=True)
class CoverageRow:
    target: Target
    status: str  # covered | uncovered | uncovered (human only)
    detail: str


@dataclass(frozen=True)
class World:
    root: Path
    skill_names: frozenset[str]
    steering_names: frozenset[str]
    always_on_names: frozenset[str]
    tool_groups: dict[str, tuple[str, ...]]
    known_tools: frozenset[str]
    trust_names: dict[str, str]  # tool name -> --trust-tools name, only the verified ones
    claude: bool = False  # a Claude home: steering has no frontmatter, CLAUDE.md imports it


@dataclass(frozen=True)
class Dataset:
    cases: tuple[Case, ...]
    rules_by_file: dict[str, tuple[Rule, ...]]
    rejections: tuple[Rejection, ...]
    rejected_suites: frozenset[str]  # "<kind>/<name>" of suites with any rejection


@dataclass(frozen=True)
class ArgSpec:
    kind: str
    required: bool
    choices: tuple[str, ...] = ()
    low: int = 0


def _arg(
    kind: str, *, choices: tuple[str, ...] = (), low: int = 0, required: bool = True
) -> ArgSpec:
    assert kind in ARG_KINDS, f"unknown arg kind {kind}"
    assert (kind == "enum") == bool(choices), "choices belong to enum args and only those"
    return ArgSpec(kind=kind, required=required, choices=choices, low=low)


_SCOPES: Final = ("final_text", "first_line", "write_inputs")
_NO_ARGS: Final[dict[str, ArgSpec]] = {}
# grader name -> exact args schema (design 3.5 table); g_hidden_tests_pass is built at step 5
GRADER_ARGS: Final[dict[str, dict[str, ArgSpec]]] = {
    "g_skill_read": {"skill": _arg("skill")},
    "g_skill_not_read": {"skill": _arg("skill")},
    "g_tool_used": {
        "tool": _arg("tool"),
        "min_count": _arg("int", low=1, required=False),
        "tool_group": _arg("tool_group", required=False),
    },
    "g_tool_not_used": {"tool": _arg("tool"), "tool_group": _arg("tool_group", required=False)},
    "g_tool_args_match": {
        "tool": _arg("tool"),
        "arg_regex": _arg("regex"),
        "expect": _arg("enum", choices=("present", "absent")),
    },
    "g_calls_max": {"tool": _arg("tool_or_null"), "max": _arg("int", low=0)},
    "g_text_matches": {"regex": _arg("regex"), "scope": _arg("enum", choices=_SCOPES)},
    "g_text_absent": {"regex": _arg("regex"), "scope": _arg("enum", choices=_SCOPES)},
    "g_fish_parses": _NO_ARGS,
    "g_no_heredoc": _NO_ARGS,
    "g_commit_message_clean": {"banned_regex": _arg("regex_list")},
    "g_planted_found": {
        "planted": _arg("planted_path"),
        "mode": _arg("enum", choices=("recall", "recall_outside", "precision", "severity")),
        "min": _arg("fraction"),
    },
    "g_clean_control_quiet": _NO_ARGS,
    "g_citation_valid": _NO_ARGS,
    "g_function_length_max": {"max_lines": _arg("int", low=1)},
    "g_assertions_per_function_min": {"min": _arg("int", low=1)},
    "g_loops_bounded": _NO_ARGS,
    "g_units_in_names": {"names": _arg("units")},
    "g_pyright_clean": _NO_ARGS,
    "g_no_outside_read": _NO_ARGS,
    "g_stderr_empty": _NO_ARGS,
    "g_hidden_tests_pass": {"tests_dir": _arg("tests_dir"), "module": _arg("identifier")},
}
HUMAN_ARGS: Final[dict[str, ArgSpec]] = {"question": _arg("text")}


# ---------------------------------------------------------------- json helpers


def _read_json(path: Path) -> tuple[object, str]:
    """Returns (value, "") or (None, reason); never raises on bad file content."""
    assert path.name, "path must name a file"
    assert path.suffix == ".json", "only json files are read here"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:
        return None, f"cannot read: {err}"
    except UnicodeDecodeError as err:
        return None, f"not utf-8: {err}"
    try:
        value: object = json.loads(text)
    except json.JSONDecodeError as err:
        return None, f"invalid json: {err}"
    return value, ""


def _as_dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    raw = cast(dict[object, object], value)
    out: dict[str, object] = {}
    for key, item in raw.items():
        if not isinstance(key, str):
            return None
        out[key] = item
    assert len(out) == len(raw), "every key was kept"
    assert all(isinstance(key, str) for key in out), "keys are strings"
    return out


def _as_str_list(value: object, *, items_max: int) -> list[str] | None:
    """A list of strings no longer than items_max, else None."""
    assert items_max > 0, "items_max must be positive"
    if not isinstance(value, list):
        return None
    raw = cast(list[object], value)
    if len(raw) > items_max:
        return None
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            return None
        out.append(item)
    assert len(out) == len(raw) <= items_max, "all items kept, within the bound"
    return out


def _is_int(value: object) -> bool:
    # bool is an int subclass; json true must not pass as 1
    result = isinstance(value, int) and not isinstance(value, bool)
    assert not (result and isinstance(value, bool)), "bools are never ints here"
    assert result == (type(value) is int), "exact int only"
    return result


def _is_inside(root: Path, candidate: Path) -> bool:
    """True when candidate's real path is root's real path or below it."""
    real_root = root.resolve()
    real_candidate = candidate.resolve()
    assert real_root.is_absolute() and real_candidate.is_absolute(), "resolve gives absolute paths"
    inside = real_candidate == real_root or real_root in real_candidate.parents
    assert inside == real_candidate.is_relative_to(real_root), "two containment tests agree"
    return inside


# ---------------------------------------------------------------- limits and world


def load_limits(path: Path) -> Limits:
    """Reads the step-1 constants from limits.json; ValueError on any bad entry."""
    value, error = _read_json(path)
    if error:
        raise ValueError(f"{path}: {error}")
    raw = _as_dict(value)
    if raw is None:
        raise ValueError(f"{path}: top level must be an object")
    fields: dict[str, int] = {}
    for json_key, field in LIMIT_KEYS.items():
        item = raw.get(json_key)
        if not _is_int(item) or cast(int, item) <= 0:
            raise ValueError(f"{path}: {json_key} must be a positive integer")
        fields[field] = cast(int, item)
    assert len(fields) == len(LIMIT_KEYS), "every limit key must land in a field"
    assert all(number > 0 for number in fields.values()), "every limit is positive"
    return Limits(**fields)


def _list_dir(path: Path, limit_count: int) -> list[Path]:
    """Sorted entries of a directory, ValueError past limit_count (no silent truncation)."""
    assert limit_count > 0, "limit_count must be positive"
    try:
        entries = sorted(path.iterdir())
    except OSError as err:
        raise ValueError(f"cannot list {path}: {err}") from err
    if len(entries) > limit_count:
        raise ValueError(f"{path} has {len(entries)} entries, limit is {limit_count}")
    assert len(entries) <= limit_count, "never truncated, never over the bound"
    assert entries == sorted(entries), "stable order"
    return entries


def _read_text_or_raise(path: Path) -> str:
    """Boundary read for files discovery needs: a broken one stops the run with a reason."""
    assert path.suffix == ".md", "discovery reads markdown only"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        raise ValueError(f"cannot read {path}: {err}") from err
    assert isinstance(text, str), "text read"
    return text


def parse_frontmatter(text: str, lines_max: int) -> dict[str, str] | str:
    """Top-level `key: value` pairs and folded/literal block scalars; a str is the error."""
    assert lines_max > 0, "lines_max must be positive"
    lines = text.split("\n", lines_max + 2)
    assert len(lines) <= lines_max + 3, "split is bounded"
    if not lines or lines[0].strip() != FENCE_LINE:
        return "no opening --- on line 1"
    fields: dict[str, str] = {}
    key_open: str | None = None  # key whose block scalar is still collecting lines
    for line in lines[1 : lines_max + 1]:
        if line.strip() == FENCE_LINE:
            assert key_open is None or key_open in fields, "an open key was recorded"
            return {key: value.strip() for key, value in fields.items()}
        match = FRONTMATTER_KEY_RE.fullmatch(line.rstrip())
        indented = line[:1] in (" ", "\t")
        if key_open is not None and (indented or not line.strip()):
            fields[key_open] = f"{fields[key_open]} {line.strip()}"
            continue
        if indented or match is None:
            return f"unparseable line: {line[:40]!r}"
        key, value = match.group(1), match.group(2).strip()
        key_open = key if value in BLOCK_SCALAR_MARKERS else None
        fields[key] = "" if key_open else value.strip("\"'")
    return f"no closing --- within {lines_max} lines"


def claude_imports(home: Path, limits: Limits) -> frozenset[str] | None:
    """Steering stems that <home>/CLAUDE.md imports (@~/.claude/steering/<name>.md): Claude's
    always-on set. None when the home has no CLAUDE.md, i.e. it is a Kiro home."""
    path = home / CLAUDE_MD
    if not path.is_file():
        return None
    lines = _read_text_or_raise(path).splitlines()[: limits.targets_per_kind_max * 8]
    names = {m.group(1) for m in (CLAUDE_IMPORT_RE.match(line) for line in lines) if m}
    assert len(names) <= len(lines), "at most one import per line"
    return frozenset(names)


def discover_world(kiro_home: Path, root: Path, limits: Limits) -> World:
    """Targets come from disk; only tool names and groups are hand-kept data. A home with a
    CLAUDE.md is Claude's: its imports are the always-on set, not frontmatter."""
    imports = claude_imports(kiro_home, limits)
    skills_dir = kiro_home / "skills"
    steering_dir = kiro_home / "steering"
    skills = {
        entry.name
        for entry in _list_dir(skills_dir, limits.targets_per_kind_max)
        if (entry / "SKILL.md").is_file()
    }
    steering: set[str] = set()
    always_on: set[str] = set()
    for entry in _list_dir(steering_dir, limits.targets_per_kind_max):
        if entry.suffix != ".md" or not entry.is_file():
            continue
        steering.add(entry.stem)
        if imports is not None:
            if entry.stem in imports:
                always_on.add(entry.stem)
            continue
        fields = parse_frontmatter(_read_text_or_raise(entry), limits.frontmatter_lines_max)
        if not isinstance(fields, str) and fields.get("inclusion") == "always":
            always_on.add(entry.stem)
    tools, groups = _load_tool_data(root)
    assert always_on <= steering, "always-on files are a subset of steering files"
    assert len(skills) <= limits.targets_per_kind_max, "skill count is bounded"
    return World(
        root=root,
        skill_names=frozenset(skills),
        steering_names=frozenset(steering),
        always_on_names=frozenset(always_on),
        tool_groups=groups,
        known_tools=tools,
        trust_names=_load_trust_names(root, tools),
        claude=imports is not None,
    )


def _load_trust_names(root: Path, tools: frozenset[str]) -> dict[str, str]:
    """v3 trust names differ from tool names (execute_bash is trusted as run_command)."""
    value, error = _read_json(root / "trust_names.json")
    raw = _as_dict(value)
    if error or raw is None:
        raise ValueError(f"trust_names.json: {error or 'top level must be an object'}")
    names: dict[str, str] = {}
    for tool, trust in raw.items():
        if tool not in tools or not isinstance(trust, str) or not trust:
            raise ValueError(f"trust_names.json: {tool!r} must map a known tool to a name")
        names[tool] = trust
    assert names, "at least one trustable tool"
    assert set(names) <= tools, "trustable tools are known tools"
    return names


def _load_tool_data(root: Path) -> tuple[frozenset[str], dict[str, tuple[str, ...]]]:
    assert root.is_dir(), "root must be the dataset directory"
    tools_value, error = _read_json(root / "known_tools.json")
    if error:
        raise ValueError(f"known_tools.json: {error}")
    tools = _as_str_list(tools_value, items_max=256)
    if not tools:
        raise ValueError("known_tools.json: must be a non-empty list of strings")
    groups_value, error = _read_json(root / "tool_groups.json")
    groups_raw = _as_dict(groups_value)
    if error or groups_raw is None:
        raise ValueError(f"tool_groups.json: {error or 'top level must be an object'}")
    groups: dict[str, tuple[str, ...]] = {}
    for name, members_value in groups_raw.items():
        members = _as_str_list(members_value, items_max=256)
        if not members or not set(members) <= set(tools):
            raise ValueError(f"tool_groups.json: group {name!r} has missing or unknown tools")
        groups[name] = tuple(members)
    assert groups, "at least one tool group"
    assert all(set(members) <= set(tools) for members in groups.values()), "groups use known tools"
    return frozenset(tools), groups


def collect_targets(world: World) -> list[Target]:
    """Every skill, steering file and tool group, sorted by kind then name."""
    targets = [Target(name, "skill") for name in sorted(world.skill_names)]
    targets += [Target(name, "steering") for name in sorted(world.steering_names)]
    targets += [Target(name, "tool_group") for name in sorted(world.tool_groups)]
    expected = len(world.skill_names) + len(world.steering_names) + len(world.tool_groups)
    assert len(targets) == expected, "one target per skill, steering file and group"
    assert len({(t.kind, t.name) for t in targets}) == len(targets), "targets are unique"
    return targets


# ---------------------------------------------------------------- assertion args


def _check_regex(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return "must be a non-empty regex string"
    try:
        compiled = re.compile(value)
    except re.error as err:
        return f"invalid regex: {err}"
    assert compiled.pattern == value, "accepted means the compiled pattern is the value"
    assert isinstance(value, str) and value, "accepted regex is a non-empty string"
    return None


def _check_units(value: object, limits: Limits) -> str | None:
    assert limits.args_list_max > 0, "list bound is positive"
    if (
        not isinstance(value, list)
        or not value
        or len(cast(list[object], value)) > limits.args_list_max
    ):
        return "must be a non-empty list of {identifier, suffixes}"
    for item in cast(list[object], value):
        entry = _as_dict(item)
        if entry is None or set(entry) != {"identifier", "suffixes"}:
            return "each item must be exactly {identifier, suffixes}"
        suffixes = _as_str_list(entry["suffixes"], items_max=limits.args_list_max)
        if not isinstance(entry["identifier"], str) or not entry["identifier"]:
            return "identifier must be a non-empty string"
        if not suffixes or not all(suffixes):
            return "suffixes must be a non-empty list of non-empty strings"
    count = len(cast(list[object], value))
    assert isinstance(value, list) and 0 < count <= limits.args_list_max, "bounded, non-empty"
    return None


def _check_file_under(world: World, value: object, subdir: str, *, want_dir: bool) -> str | None:
    assert subdir in ("planted", "hidden"), "only answer-key dirs are checked here"
    if not isinstance(value, str) or not value:
        return f"must be a path under {subdir}/"
    candidate = world.root / value
    if not _is_inside(world.root / subdir, candidate):
        return f"{value!r} is not under {subdir}/"
    if want_dir and not candidate.is_dir():
        return f"{value!r} is not an existing directory"
    if not want_dir and not candidate.is_file():
        return f"{value!r} is not an existing file"
    assert candidate.exists(), "an accepted path exists"
    return None


def _check_arg_value(spec: ArgSpec, value: object, world: World, limits: Limits) -> str | None:
    """None when value fits the spec, else the reason."""
    kind = spec.kind
    assert kind in ARG_KINDS, f"unknown arg kind {kind}"
    if kind in ("string", "text") and not (isinstance(value, str) and value.strip()):
        return "must be a non-empty string"
    if kind == "text":
        assert isinstance(value, str) and value.strip(), "text was checked non-empty above"
        lowered = value.lower()
        banned = [phrase for phrase in BANNED_PHRASES if phrase in lowered]
        return f"banned vague phrase {banned[0]!r}" if banned else None
    if kind in ("skill", "tool", "tool_group"):
        pool = {"skill": world.skill_names, "tool": world.known_tools}.get(
            kind, frozenset(world.tool_groups)
        )
        # isinstance first: a json list or object is unhashable and would raise in `in`
        return None if isinstance(value, str) and value in pool else _unknown(value, kind)
    if kind == "tool_or_null":
        known = value is None or (isinstance(value, str) and value in world.known_tools)
        return None if known else _unknown(value, "tool")
    return _check_arg_value_rest(spec, value, world, limits)


def _unknown(value: object, kind: str) -> str:
    reason = f"{value!r} is not a known {kind}"
    assert kind in ARG_KINDS, "the kind is a real arg kind"
    assert repr(value) in reason and kind in reason, "reason names the kind and the value"
    return reason


def _check_arg_value_rest(spec: ArgSpec, value: object, world: World, limits: Limits) -> str | None:
    kind = spec.kind
    assert kind in ARG_KINDS, f"unknown arg kind {kind}"
    assert spec.low >= 0, "lower bound is not negative"
    if kind == "int":
        ok = _is_int(value) and cast(int, value) >= spec.low
        return None if ok else f"must be an integer >= {spec.low}"
    if kind == "fraction":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        ok = ok and 0.0 <= float(cast(float, value)) <= 1.0
        return None if ok else "must be a number in [0, 1]"
    if kind == "enum":
        return None if value in spec.choices else f"must be one of {list(spec.choices)}"
    if kind == "regex":
        return _check_regex(value)
    if kind == "regex_list":
        if (
            not isinstance(value, list)
            or not value
            or len(cast(list[object], value)) > limits.args_list_max
        ):
            return "must be a non-empty list of regex strings"
        for item in cast(list[object], value):
            problem = _check_regex(item)
            if problem:
                return problem
        return None
    if kind == "units":
        return _check_units(value, limits)
    if kind == "planted_path":
        return _check_file_under(world, value, "planted", want_dir=False)
    if kind == "identifier":
        ok = isinstance(value, str) and value.isidentifier() and value.isascii()
        return None if ok else "must be an ascii python identifier"
    assert kind == "tests_dir", f"unhandled arg kind {kind}"
    return _check_file_under(world, value, "hidden", want_dir=True)


def check_args(
    schema: dict[str, ArgSpec], args: dict[str, object], world: World, limits: Limits
) -> str | None:
    """Exact-schema check: no unknown keys, no missing required keys, every value typed."""
    assert all(isinstance(spec, ArgSpec) for spec in schema.values()), "schema holds ArgSpecs"
    unknown = sorted(set(args) - set(schema))
    if unknown:
        return f"unknown args {unknown}"
    for name, spec in schema.items():
        if name not in args:
            if spec.required:
                return f"missing arg {name!r}"
            continue
        problem = _check_arg_value(spec, args[name], world, limits)
        if problem:
            return f"arg {name!r}: {problem}"
    assert set(args) <= set(schema), "accepted args are all in the schema"
    assert all(name in args for name, spec in schema.items() if spec.required), "required present"
    return None


def _parse_assertion(
    raw: object, seen_ids: set[str], world: World, limits: Limits
) -> Assertion | str:
    entry = _as_dict(raw)
    if entry is None or set(entry) != {"id", "grader", "args", "hard"}:
        return "assertion must be exactly {id, grader, args, hard}"
    assertion_id, grader, hard = entry["id"], entry["grader"], entry["hard"]
    args = _as_dict(entry["args"])
    if not isinstance(assertion_id, str) or not ID_RE.fullmatch(assertion_id):
        return f"assertion id {assertion_id!r} must match {ID_RE.pattern}"
    if assertion_id in seen_ids:
        return f"duplicate assertion id {assertion_id!r}"
    if not isinstance(hard, bool) or args is None or not isinstance(grader, str):
        return f"assertion {assertion_id}: hard must be bool, args an object, grader a string"
    if grader in STATIC_ONLY_GRADERS:
        return f"assertion {assertion_id}: {grader} is static-tier only"
    if grader == HUMAN_GRADER and hard:
        return f"assertion {assertion_id}: a human assertion must have hard = false"
    schema = HUMAN_ARGS if grader == HUMAN_GRADER else GRADER_ARGS.get(grader)
    if schema is None:
        return f"assertion {assertion_id}: unknown grader {grader!r}"
    problem = check_args(schema, args, world, limits)
    if problem:
        return f"assertion {assertion_id}: {problem}"
    assert assertion_id not in seen_ids, "ids are unique within a case"
    assert grader != HUMAN_GRADER or not hard, "human assertions are never hard"
    return Assertion(id=assertion_id, grader=grader, args=args, hard=hard)


def _parse_assertions(raw: object, world: World, limits: Limits) -> tuple[Assertion, ...] | str:
    assert limits.assertions_per_case_max > 0, "assertion bound is positive"
    if not isinstance(raw, list) or not raw:
        return "assertions must be a non-empty list"
    items = cast(list[object], raw)
    if len(items) > limits.assertions_per_case_max:
        return f"{len(items)} assertions, limit is {limits.assertions_per_case_max}"
    parsed: list[Assertion] = []
    seen: set[str] = set()
    for item in items:
        result = _parse_assertion(item, seen, world, limits)
        if isinstance(result, str):
            return result
        seen.add(result.id)
        parsed.append(result)
    if not any(a.hard and a.grader != HUMAN_GRADER for a in parsed):
        return "needs at least one hard assertion with a code grader"
    assert 0 < len(parsed) <= limits.assertions_per_case_max, "bounded, non-empty"
    assert len(seen) == len(parsed), "one unique id per assertion"
    return tuple(parsed)


# ---------------------------------------------------------------- arms and cases


def parse_arm(text: str, world: World) -> Arm | str:
    """'with' | 'without' | 'previous' | 'none' | 'full' | 'minus-<always-on file>' |
    'skill-instead'."""
    assert text, "arm text must be non-empty"
    assert world.always_on_names <= world.steering_names, "always-on files are steering files"
    if text.startswith(MINUS_PREFIX):
        name = text[len(MINUS_PREFIX) :]
        if name not in world.always_on_names:
            return f"arm {text!r}: {name!r} is not an always-on steering file"
        return Arm(kind="minus", minus_file=name)
    if text in SKILL_ARMS or text in STEERING_ARMS or text in VALUE_ARMS:
        assert text != "minus", "bare minus is not an arm"
        return Arm(kind=text, minus_file=None)
    return f"unknown arm {text!r}"


def _check_arms(raw: object, target_kind: str, target: str, world: World) -> tuple[Arm, ...] | str:
    texts = _as_str_list(raw, items_max=64)
    assert target_kind in TARGET_KINDS, "target kind is validated before arms"
    if not texts or len(set(texts)) != len(texts):
        return "arms must be a non-empty list of unique strings"
    arms: list[Arm] = []
    for text in texts:
        arm = parse_arm(text, world)
        if isinstance(arm, str):
            return arm
        arms.append(arm)
    kinds = {arm.kind for arm in arms}
    if kinds & set(VALUE_ARMS):
        if target_kind != "skill" or target != VALUE_SKILL or kinds != set(VALUE_ARMS):
            return f"arms {list(VALUE_ARMS)} go together, on skill cases targeting {VALUE_SKILL}"
    elif target_kind == "skill":
        if not kinds <= set(SKILL_ARMS) or "with" not in kinds:
            return "skill arms must be a subset of with/without/previous/none including with"
    elif not kinds <= {*STEERING_ARMS, "minus"} or "full" not in kinds or len(arms) < 2:
        return "steering arms must include full and one other of none/minus-<file>/skill-instead"
    if SKILL_INSTEAD_ARM in kinds:
        if target_kind != "steering" or target not in world.always_on_names:
            return f"arm {SKILL_INSTEAD_ARM!r} fits only an always-on steering target"
    assert len(arms) == len(texts), "one Arm per text"
    return tuple(arms)


def _check_names(
    raw: object, pool: frozenset[str], what: str, limits: Limits
) -> tuple[str, ...] | str:
    names = _as_str_list(raw, items_max=limits.args_list_max)
    assert what, "the field name is used in the reason"
    if names is None or not set(names) <= pool:
        return f"{what} must be a list of existing names"
    assert len(names) <= limits.args_list_max, "name list is bounded"
    return tuple(names)


def _prompt_ok(value: object, limits: Limits) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value.encode()) <= limits.prompt_bytes_max
    )


def _check_prompts(
    entry: dict[str, object], limits: Limits
) -> tuple[str | None, tuple[str, ...], tuple[tuple[str, str], ...]] | str:
    """Exactly one of prompt, user_turns, arm_prompts; arm_prompts keys are matched to the
    case's arms later (_check_arm_prompts), they are only typed here."""
    prompt, turns_raw, per_arm = (
        entry.get("prompt"),
        entry.get("user_turns"),
        entry.get("arm_prompts"),
    )
    assert limits.prompt_bytes_max > 0, "prompt bound is positive"
    if sum(x is not None for x in (prompt, turns_raw, per_arm)) != 1:
        return "exactly one of prompt, user_turns and arm_prompts is required"
    if prompt is not None:
        if _prompt_ok(prompt, limits):
            return cast(str, prompt), (), ()
        return f"prompt must be non-empty, at most {limits.prompt_bytes_max} bytes"
    if per_arm is not None:
        mapping = _as_dict(per_arm)
        if not mapping or not all(_prompt_ok(v, limits) for v in mapping.values()):
            return (
                f"arm_prompts must map arms to non-empty prompts under {limits.prompt_bytes_max} B"
            )
        return None, (), tuple((arm, cast(str, text)) for arm, text in mapping.items())
    turns = _as_str_list(turns_raw, items_max=limits.user_turns_max)
    if not turns or any(not t.strip() or len(t.encode()) > limits.prompt_bytes_max for t in turns):
        return f"user_turns must be 1..{limits.user_turns_max} non-empty prompts"
    assert len(turns) <= limits.user_turns_max, "turn count is bounded"
    assert all(len(t.encode()) <= limits.prompt_bytes_max for t in turns), "turns are bounded"
    return None, tuple(turns), ()


def _check_arm_prompts(per_arm: tuple[tuple[str, str], ...], arms: tuple[Arm, ...]) -> str | None:
    """One prompt per arm, no more and no fewer: a missing arm would run with no prompt."""
    if not per_arm:
        return None
    assert arms, "arms are validated before their prompts"
    wanted = {arm.kind if arm.minus_file is None else f"minus-{arm.minus_file}" for arm in arms}
    given = [arm for arm, _ in per_arm]
    if set(given) != wanted or len(given) != len(wanted):
        return f"arm_prompts keys {sorted(given)} must be exactly the arms {sorted(wanted)}"
    assert len(per_arm) == len(arms), "one prompt per arm"
    return None


def _tree_bytes(path: Path, files_max: int) -> int | None:
    """Total file bytes under path; None when more than files_max files."""
    assert files_max > 0, "files_max must be positive"
    if path.is_file():
        return path.stat().st_size
    total, count = 0, 0
    for dir_path, _dirs, names in os.walk(path):
        for name in names:
            count += 1
            if count > files_max:
                return None
            total += (Path(dir_path) / name).stat().st_size
    assert total >= 0 and count <= files_max, "walk stayed within its bound"
    return total


def _check_fixture(raw: object, world: World, limits: Limits) -> str | None:
    assert limits.fixture_bytes_max > 0, "fixture byte bound is positive"
    if not isinstance(raw, str) or not raw:
        return "fixture must be a path string"
    path = world.root / raw
    if not _is_inside(world.root, path) or not path.exists():
        return f"fixture {raw!r} must exist under the dataset root"
    # the answer key must never sit in a cwd the model can read (design 3.4.1)
    if _is_inside(world.root / "planted", path) or _is_inside(world.root / "hidden", path):
        return f"fixture {raw!r} must not be inside planted/ or hidden/"
    size = _tree_bytes(path, limits.fixture_files_max)
    if size is None or size > limits.fixture_bytes_max:
        return f"fixture {raw!r} exceeds {limits.fixture_bytes_max} bytes or file count"
    assert 0 <= size <= limits.fixture_bytes_max, "accepted fixture is within budget"
    return None


_CASE_KEYS: Final = frozenset(
    "id target target_kind rule_id role prompt user_turns arm_prompts fixture trust_tools arms "
    "extra_skills extra_steering repeats tags tier assertions".split()
)
_CASE_REQUIRED: Final = ("id", "target", "target_kind", "role", "trust_tools", "arms", "assertions")


def _check_identity(
    entry: dict[str, object], suite_kind: str, suite_name: str, world: World
) -> str | None:
    """Keys, id, target and role: everything that places the case in a suite."""
    assert suite_kind in SUITE_KINDS, "suite_kind is skill, steering or value"
    unknown = sorted(set(entry) - _CASE_KEYS)
    missing = [key for key in _CASE_REQUIRED if key not in entry]
    if unknown or missing:
        return f"unknown keys {unknown}, missing keys {missing}"
    case_id, target, kind = entry["id"], entry["target"], entry["target_kind"]
    if not isinstance(case_id, str) or not ID_RE.fullmatch(case_id):
        return f"id {case_id!r} must match {ID_RE.pattern}"
    if not isinstance(target, str) or not target:
        return "target must be a non-empty string"
    if kind not in TARGET_KINDS or entry["role"] not in ROLES:
        return f"target_kind must be one of {TARGET_KINDS} and role one of {ROLES}"
    assert kind in TARGET_KINDS and entry["role"] in ROLES, "kind and role are validated"
    if suite_kind == "value":  # a value suite holds skill cases only, aimed at any real skill
        if not (kind == "skill" and target in world.skill_names):
            return "a value suite holds skill cases that target an existing skill"
        return None
    if kind == "skill" and not (suite_kind == "skill" and target == suite_name):
        return f"skill case must sit in skills/{target}/ and target an existing skill"
    if kind == "steering" and not (suite_kind == "steering" and target == suite_name):
        return f"steering case must sit in steering/{target}/ and target that file"
    if kind == "tool_group" and not (suite_kind == "steering" and target in world.tool_groups):
        return "tool_group case must sit in a steering suite and name a tool group"
    return None


def _check_rule_id(entry: dict[str, object], kind: str, rule_ids: frozenset[str]) -> str | None:
    assert kind in TARGET_KINDS, "kind is validated before the rule id"
    rule_id = entry.get("rule_id")
    if kind == "skill":
        return "rule_id is for steering cases only" if rule_id is not None else None
    ok = isinstance(rule_id, str) and rule_id in rule_ids
    assert not ok or rule_id in rule_ids, "an accepted rule id names a known rule"
    return None if ok else f"rule_id {rule_id!r} names no Rule in this suite's rules.json"


def _check_optional(entry: dict[str, object], limits: Limits) -> str | None:
    assert limits.runs_per_case_max > 0, "repeat bound is positive"
    repeats, tags, tier = entry.get("repeats"), entry.get("tags", []), entry.get("tier")
    if repeats is not None and not (
        _is_int(repeats) and 1 <= cast(int, repeats) <= limits.runs_per_case_max
    ):
        return f"repeats must be 1..{limits.runs_per_case_max}"
    tag_list = _as_str_list(tags, items_max=2)
    if tag_list is None or len(tag_list) > 1 or not set(tag_list) <= set(TAGS):
        return f"tags: at most one of {TAGS}"
    if tier is not None and tier not in CASE_TIERS:
        return f"tier must be one of {CASE_TIERS}"
    assert repeats is None or 1 <= cast(int, repeats) <= limits.runs_per_case_max, "repeats ok"
    return None


def _parse_case(
    entry: dict[str, object],
    suite_kind: str,
    suite_name: str,
    rule_ids: frozenset[str],
    world: World,
    limits: Limits,
) -> Case | str:
    """First reason a case is invalid, else the Case. Split in checks to stay readable."""
    problem = _check_identity(entry, suite_kind, suite_name, world)
    if problem:
        return problem
    kind = cast(str, entry["target_kind"])
    problem = _check_rule_id(entry, kind, rule_ids) or _check_optional(entry, limits)
    if problem:
        return problem
    prompts = _check_prompts(entry, limits)
    arms = _check_arms(entry["arms"], kind, cast(str, entry["target"]), world)
    extra_skills = _check_names(
        entry.get("extra_skills", []), world.skill_names, "extra_skills", limits
    )
    extra_steering = _check_names(
        entry.get("extra_steering", []), world.steering_names, "extra_steering", limits
    )
    trust = _as_str_list(entry["trust_tools"], items_max=limits.args_list_max)
    assertions = _parse_assertions(entry["assertions"], world, limits)
    if "fixture" in entry:
        problem = _check_fixture(entry["fixture"], world, limits)
        if problem:
            return problem
    if trust is None or not set(trust) <= set(world.trust_names) or len(set(trust)) != len(trust):
        return "trust_tools must be a list of unique tool names listed in trust_names.json"
    for part in (prompts, arms, extra_skills, extra_steering, assertions):
        if isinstance(part, str):
            return part
    assert not isinstance(prompts, str) and not isinstance(arms, str)
    assert not isinstance(extra_skills, str) and not isinstance(extra_steering, str)
    assert not isinstance(assertions, str)
    problem = _check_arm_prompts(prompts[2], arms)
    if problem:
        return problem
    return Case(
        id=cast(str, entry["id"]),
        target=cast(str, entry["target"]),
        target_kind=kind,
        rule_id=cast("str | None", entry.get("rule_id")),
        role=cast(str, entry["role"]),
        prompt=prompts[0],
        user_turns=prompts[1],
        arm_prompts=prompts[2],
        fixture=cast("str | None", entry.get("fixture")),
        trust_tools=tuple(trust),
        arms=arms,
        extra_skills=extra_skills,
        extra_steering=extra_steering,
        repeats=cast("int | None", entry.get("repeats")),
        tags=tuple(cast(list[str], entry.get("tags", []))),
        tier=cast("str | None", entry.get("tier")),
        assertions=assertions,
    )


def load_cases(
    path: Path,
    suite_kind: str,
    rule_ids: frozenset[str],
    world: World,
    limits: Limits,
) -> tuple[tuple[Case, ...], list[Rejection]]:
    """All cases of one file, or no cases and every reason: the whole file is rejected."""
    assert suite_kind in SUITE_KINDS, "suite_kind is skill, steering or value"
    file_text = str(path)
    value, error = _read_json(path)
    if error:
        return (), [Rejection(file_text, None, error)]
    if not isinstance(value, list) or not value:
        return (), [Rejection(file_text, None, "top level must be a non-empty list of cases")]
    items = cast(list[object], value)
    if len(items) > limits.cases_per_suite_max:
        reason = f"{len(items)} cases, limit is {limits.cases_per_suite_max}"
        return (), [Rejection(file_text, None, reason)]
    cases: list[Case] = []
    rejections: list[Rejection] = []
    seen: set[str] = set()
    for item in items:
        entry = _as_dict(item)
        record_id = entry.get("id") if entry else None
        record_id = record_id if isinstance(record_id, str) else None
        if entry is None:
            rejections.append(Rejection(file_text, None, "case must be an object"))
        elif record_id in seen:
            rejections.append(Rejection(file_text, record_id, "duplicate case id in file"))
        else:
            result = _parse_case(entry, suite_kind, path.parent.name, rule_ids, world, limits)
            if isinstance(result, str):
                rejections.append(Rejection(file_text, record_id, result))
            else:
                cases.append(result)
        if record_id:
            seen.add(record_id)
    kept = () if rejections else tuple(cases)
    assert not (kept and rejections), "a file with any rejection yields no cases"
    assert len(kept) <= limits.cases_per_suite_max, "case count is bounded"
    return kept, rejections


def load_rules(
    path: Path, world: World, limits: Limits
) -> tuple[tuple[Rule, ...], list[Rejection]]:
    """Rules of one steering file (rules.json); rejected whole on any violation."""
    assert limits.rules_per_file_max > 0, "rule bound is positive"
    file_text = str(path)
    value, error = _read_json(path)
    if error:
        return (), [Rejection(file_text, None, error)]
    if not isinstance(value, list) or not value:
        return (), [Rejection(file_text, None, "top level must be a non-empty list of rules")]
    items = cast(list[object], value)
    if len(items) > limits.rules_per_file_max:
        return (), [Rejection(file_text, None, f"more than {limits.rules_per_file_max} rules")]
    rules: list[Rule] = []
    rejections: list[Rejection] = []
    for item in items:
        result = _parse_rule(item, path.parent.name, {r.id for r in rules}, world)
        if isinstance(result, str):
            rejections.append(Rejection(file_text, None, result))
        else:
            rules.append(result)
    kept = () if rejections else tuple(rules)
    assert not (kept and rejections), "a file with any rejection yields no rules"
    assert len(kept) <= limits.rules_per_file_max, "rule count is bounded"
    return kept, rejections


def _parse_rule(raw: object, suite_name: str, seen_ids: set[str], world: World) -> Rule | str:
    keys = {"id", "source_file", "trigger_situation", "observable", "grader"}
    entry = _as_dict(raw)
    if entry is None or set(entry) != keys:
        return f"rule must be exactly {sorted(keys)}"
    texts = {key: entry[key] for key in keys}
    if not all(isinstance(v, str) and v.strip() for v in texts.values()):
        return "every rule field must be a non-empty string"
    rule = Rule(**cast(dict[str, str], texts))
    if not ID_RE.fullmatch(rule.id) or rule.id in seen_ids:
        return f"rule id {rule.id!r} must match {ID_RE.pattern} and be unique"
    if rule.source_file != suite_name or rule.source_file not in world.steering_names:
        return f"source_file {rule.source_file!r} must be the suite's own existing steering file"
    if rule.grader != HUMAN_GRADER and rule.grader not in GRADER_ARGS:
        return f"rule {rule.id}: unknown grader {rule.grader!r}"
    assert rule.source_file in world.steering_names, "a rule belongs to a real steering file"
    assert rule.grader == HUMAN_GRADER or rule.grader in GRADER_ARGS, "grader is known"
    return rule


# ---------------------------------------------------------------- dataset


def _uses_rule_skill(case: Case) -> bool:
    return any(arm.kind == SKILL_INSTEAD_ARM for arm in case.arms)


def _rule_skill_description(path: Path, limits: Limits) -> tuple[str | None, str | None]:
    """(description, None) or (None, reason). One line with no quote or backslash, so the
    SKILL.md the arm writes needs no escaping."""
    assert limits.description_chars_max > 0, "description bound is positive"
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as err:
        return None, f"a skill-instead arm needs {AS_SKILL_FILE}: {err}"
    raw = cast(dict[str, object], value) if isinstance(value, dict) else {}
    description = raw.get("description")
    if set(raw) != {"description"} or not isinstance(description, str) or not description:
        return None, f'{AS_SKILL_FILE} must be {{"description": "<non-empty text>"}}'
    if len(description) > limits.description_chars_max:
        return None, f"description: {len(description)} chars > {limits.description_chars_max}"
    if any(ch in description for ch in '\r\n"\\'):
        return None, "description: one line, no double quote or backslash"
    assert description and len(description) <= limits.description_chars_max, "checked above"
    return description, None


def attach_rule_skill(
    suite_dir: Path, cases: tuple[Case, ...], limits: Limits
) -> tuple[tuple[Case, ...], Rejection | None]:
    """Give a steering suite's skill-instead cases the description in as_skill.json; no cases
    and a rejection when they need one and it is missing or bad. Both suite loaders call this."""
    if not any(_uses_rule_skill(c) for c in cases):
        return cases, None
    description, reason = _rule_skill_description(suite_dir / AS_SKILL_FILE, limits)
    if reason is not None:
        return (), Rejection(str(suite_dir / AS_SKILL_FILE), None, reason)
    assert description is not None, "no reason means a description"
    attached = tuple(
        replace(c, rule_skill_description=description) if _uses_rule_skill(c) else c for c in cases
    )
    assert len(attached) == len(cases), "one case out per case in"
    return attached, None


def load_dataset(world: World, limits: Limits) -> Dataset:
    """Loads rules.json and cases.json of every suite dir; a bad file rejects its suite."""
    assert limits.targets_per_kind_max > 0, "suite bound is positive"
    cases: list[Case] = []
    rules_by_file: dict[str, tuple[Rule, ...]] = {}
    rejections: list[Rejection] = []
    rejected: set[str] = set()
    seen_ids: dict[str, str] = {}
    for suite_kind, names in (
        ("skill", world.skill_names),
        ("steering", world.steering_names),
        ("value", frozenset[str]()),
    ):
        suite_root = world.root / SUITE_DIRS[suite_kind]
        if not suite_root.is_dir():
            continue
        for suite_dir in _list_dir(suite_root, limits.targets_per_kind_max):
            suite_key = f"{suite_kind}/{suite_dir.name}"
            found = _load_suite(suite_dir, suite_kind, names, world, limits, seen_ids)
            cases += found[0]
            rules_by_file.update(found[1])
            rejections += found[2]
            if found[2]:
                rejected.add(suite_key)
    assert len({c.id for c in cases}) == len(cases), "case ids are unique across the dataset"
    return Dataset(tuple(cases), rules_by_file, tuple(rejections), frozenset(rejected))


def _load_suite(
    suite_dir: Path,
    suite_kind: str,
    names: frozenset[str],
    world: World,
    limits: Limits,
    seen_ids: dict[str, str],
) -> tuple[list[Case], dict[str, tuple[Rule, ...]], list[Rejection]]:
    """One suite dir: rules first (steering), then cases; ids claimed across suites."""
    assert suite_kind in SUITE_KINDS, "suite_kind is skill, steering or value"
    if not suite_dir.is_dir():
        return [], {}, []
    if suite_kind != "value" and suite_dir.name not in names:
        reason = f"suite dir names no existing {suite_kind}"
        return [], {}, [Rejection(str(suite_dir), None, reason)]
    rules: tuple[Rule, ...] = ()
    rejections: list[Rejection] = []
    if suite_kind == "steering" and (suite_dir / "rules.json").is_file():
        rules, rejections = load_rules(suite_dir / "rules.json", world, limits)
    cases: tuple[Case, ...] = ()
    cases_path = suite_dir / "cases.json"
    if cases_path.is_file():
        rule_ids = frozenset(rule.id for rule in rules)
        cases, case_rejections = load_cases(cases_path, suite_kind, rule_ids, world, limits)
        rejections += case_rejections
    if suite_kind == "steering":
        cases, sidecar = attach_rule_skill(suite_dir, cases, limits)
        if sidecar is not None:
            rejections.append(sidecar)
    clashes = [c.id for c in cases if c.id in seen_ids]
    if clashes:
        reason = f"case id {clashes[0]!r} already used in {seen_ids[clashes[0]]}"
        rejections.append(Rejection(str(cases_path), clashes[0], reason))
    if rejections:
        return [], {}, rejections
    for case in cases:
        seen_ids[case.id] = str(cases_path)
    assert all(c.id in seen_ids for c in cases), "every returned case id is claimed"
    return list(cases), ({suite_dir.name: rules} if rules else {}), []


# ---------------------------------------------------------------- coverage


def _has_hard_code_assertion(case: Case) -> bool:
    assert case.assertions, "a validated case has assertions"
    assert all(isinstance(a, Assertion) for a in case.assertions), "assertions are parsed"
    return any(a.hard and a.grader != HUMAN_GRADER for a in case.assertions)


def _roles_for(target: Target, cases: tuple[Case, ...]) -> set[str]:
    """Roles of the cases that count toward covering target."""
    assert target.kind in TARGET_KINDS, "target kind is known"
    roles: set[str] = set()
    for case in cases:
        if not _has_hard_code_assertion(case):
            continue
        if target.kind == "tool_group":
            named = any(a.hard and a.args.get("tool_group") == target.name for a in case.assertions)
            if named and case.target_kind != "skill":
                roles.add(case.role)
        elif case.target == target.name and case.target_kind == target.kind:
            roles.add(case.role)
    assert roles <= set(ROLES), "roles come from validated cases"
    return roles


def classify_coverage(targets: list[Target], dataset: Dataset) -> list[CoverageRow]:
    """covered needs a fire and a no_fire case with a hard code assertion (design section 4).

    A steering file whose rules are all graded `human` reports `uncovered (human only)`.
    """
    rows: list[CoverageRow] = []
    assert len({(t.kind, t.name) for t in targets}) == len(targets), "targets are unique"
    for target in targets:
        key = f"{target.kind}/{target.name}"
        roles = _roles_for(target, dataset.cases)
        rules = dataset.rules_by_file.get(target.name, ()) if target.kind == "steering" else ()
        if key in dataset.rejected_suites:
            rows.append(CoverageRow(target, "uncovered", "dataset file rejected"))
        elif {"fire", "no_fire"} <= roles:
            rows.append(CoverageRow(target, "covered", ""))
        elif rules and all(rule.grader == HUMAN_GRADER for rule in rules):
            rows.append(CoverageRow(target, "uncovered (human only)", "all rules graded by human"))
        else:
            missing = sorted({"fire", "no_fire"} - roles)
            rows.append(CoverageRow(target, "uncovered", f"missing roles {missing}"))
    assert len(rows) == len(targets), "one row per target"
    return rows


# ---------------------------------------------------------------- static lint


def lint_skill(skill_dir: Path, limits: Limits) -> list[LintFinding]:
    """Frontmatter parses, name matches dir, description length, refs exist, byte budget."""
    skill_file = skill_dir / "SKILL.md"
    where = str(skill_file)
    assert skill_dir.name, "skill_dir must name a directory"
    assert limits.skill_bytes_max > 0, "skill byte budget is positive"
    try:
        text = skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        return [LintFinding(where, "readable", f"cannot read: {err}")]
    findings: list[LintFinding] = []
    size_bytes = len(text.encode())
    if size_bytes > limits.skill_bytes_max:
        findings.append(
            LintFinding(where, "byte-budget", f"{size_bytes} > {limits.skill_bytes_max}")
        )
    fields = parse_frontmatter(text, limits.frontmatter_lines_max)
    if isinstance(fields, str):
        return findings + [LintFinding(where, "frontmatter", fields)]
    if fields.get("name") != skill_dir.name:
        findings.append(LintFinding(where, "name", f"name {fields.get('name')!r} != dir name"))
    description = fields.get("description", "")
    if not description:
        findings.append(LintFinding(where, "description", "missing or empty"))
    elif len(description) > limits.description_chars_max:
        message = f"{len(description)} chars > {limits.description_chars_max}"
        findings.append(LintFinding(where, "description-length", message))
    for ref in sorted(set(SKILL_REF_RE.findall(text))):
        if not (skill_dir / ref).exists():
            findings.append(LintFinding(where, "missing-reference", ref))
    assert all(f.path_text == where for f in findings), "every finding names the skill file"
    return findings


def lint_steering(path: Path, limits: Limits, frontmatter: bool = True) -> list[LintFinding]:
    """Frontmatter parses with a known `inclusion` (Kiro only), and the file is within its
    byte budget."""
    where = str(path)
    assert path.suffix == ".md", "steering files are .md"
    assert limits.steering_bytes_max > 0, "steering byte budget is positive"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        return [LintFinding(where, "readable", f"cannot read: {err}")]
    findings: list[LintFinding] = []
    size_bytes = len(text.encode())
    if size_bytes > limits.steering_bytes_max:
        findings.append(
            LintFinding(where, "byte-budget", f"{size_bytes} > {limits.steering_bytes_max}")
        )
    if not frontmatter:
        return findings
    fields = parse_frontmatter(text, limits.frontmatter_lines_max)
    if isinstance(fields, str):
        return findings + [LintFinding(where, "frontmatter", fields)]
    if fields.get("inclusion") not in INCLUSION_VALUES:
        findings.append(LintFinding(where, "inclusion", f"got {fields.get('inclusion')!r}"))
    assert all(f.path_text == where for f in findings), "every finding names the steering file"
    return findings


def load_lint_baseline(path: Path) -> frozenset[tuple[str, str, str]]:
    """Accepted findings as (rule, path relative to the kiro home, message); keyed by message
    so a shifted line cannot hide a new problem (debugging-discipline #4)."""
    assert path.suffix == ".json", "the baseline is a json file"
    value, error = _read_json(path)
    if error:
        raise ValueError(f"{path}: {error}")
    if not isinstance(value, list):
        raise ValueError(f"{path}: top level must be a list")
    accepted: set[tuple[str, str, str]] = set()
    for item in cast(list[object], value):
        entry = _as_dict(item)
        if entry is None or set(entry) != {"rule", "path", "message"}:
            raise ValueError(f"{path}: each entry must be exactly {{rule, path, message}}")
        if not all(isinstance(v, str) and v for v in entry.values()):
            raise ValueError(f"{path}: every entry field must be a non-empty string")
        accepted.add(
            (cast(str, entry["rule"]), cast(str, entry["path"]), cast(str, entry["message"]))
        )
    assert len(accepted) <= len(cast(list[object], value)), "dedup never grows the list"
    assert all(all(part for part in triple) for triple in accepted), "no empty parts"
    return frozenset(accepted)


def split_baseline(
    findings: list[LintFinding], baseline: frozenset[tuple[str, str, str]]
) -> tuple[list[LintFinding], list[LintFinding]]:
    """(new, known): known findings match the baseline exactly."""
    new = [f for f in findings if (f.rule, f.path_text, f.message) not in baseline]
    known = [f for f in findings if (f.rule, f.path_text, f.message) in baseline]
    assert len(new) + len(known) == len(findings), "every finding is new or known"
    assert all((f.rule, f.path_text, f.message) in baseline for f in known), "known are baselined"
    return new, known


def lint_all(kiro_home: Path, world: World, limits: Limits) -> list[LintFinding]:
    """Per-file lint plus the total weight of the always-on set; paths relative to kiro_home."""
    assert kiro_home.is_dir(), "kiro_home must be a directory"
    findings: list[LintFinding] = _lint_files(kiro_home, world, limits)
    relative = [
        LintFinding(_relative_text(f.path_text, kiro_home), f.rule, f.message) for f in findings
    ]
    assert len(relative) == len(findings), "relativizing keeps every finding"
    return relative


def _relative_text(path_text: str, kiro_home: Path) -> str:
    prefix = str(kiro_home) + os.sep
    result = path_text[len(prefix) :] if path_text.startswith(prefix) else path_text
    assert len(result) <= len(path_text), "relativizing never grows the path"
    assert not result.startswith(os.sep) or not path_text.startswith(prefix), "prefix removed"
    return result


def _lint_files(kiro_home: Path, world: World, limits: Limits) -> list[LintFinding]:
    assert kiro_home.is_dir(), "kiro_home must be a directory"
    assert world.always_on_names <= world.steering_names, "always-on files are steering files"
    findings: list[LintFinding] = []
    for name in sorted(world.skill_names):
        findings += lint_skill(kiro_home / "skills" / name, limits)
    always_on_bytes = 0
    for name in sorted(world.steering_names):
        path = kiro_home / "steering" / f"{name}.md"
        findings += lint_steering(path, limits, frontmatter=not world.claude)
        if name in world.always_on_names:
            always_on_bytes += path.stat().st_size
    if always_on_bytes > limits.always_on_bytes_max:
        message = f"{always_on_bytes} > {limits.always_on_bytes_max}"
        findings.append(LintFinding(str(kiro_home / "steering"), "always-on-budget", message))
    return findings
