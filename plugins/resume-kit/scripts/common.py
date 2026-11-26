"""Shared types, limits and guards for the resume-kit scripts (a sibling module, not a package)."""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

# limits: placeholders until the next score test (design doc, Limits)
FIX_ROUNDS_MAX = 5
POLISH_ROUNDS_MAX = 5
POLISH_IDLE_ROUNDS = 2
VOTE_RUNS = 5
VOTE_KEEP = 3
PAIRED_RUNS_FIRST = 5
PAIRED_RUNS_MAX = 10
PAIRED_DROP_AT_MOST = 2  # of the first 5: stop early, the change isn't preferred
PAIRED_KEEP_AT_LEAST = 8  # of 10: a fair judge's false keeps are ~5%
FUZZY_CAP_POINTS = 15
EXPRESS_VARIANTS = 8
EXPRESS_SCORES_EACH = 4
EXPRESS_EDITS_MAX = 6
EXPRESS_PASSES_MAX = 3
FINDINGS_PER_CHECK_MAX = 20
CHECKS_MAX = 40
AGENT_RUNS_MAX = 230  # 75 loop + 3 Express passes of 50, rounded up
BUILD_TIMEOUT_S = 120
LINT_TIMEOUT_S = 30
POSTING_BYTES_MAX = 64 * 1024
PDF_BYTES_MAX = 5 * 1024 * 1024
PDF_PAGES_MAX = 3
FACT_TEXT_CHARS_MAX = 2000
AGENT_JSON_BYTES_MAX = 256 * 1024
SECTIONS_MAX = 12
ENTRIES_MAX = 40
BULLETS_PER_ENTRY_MAX = 12
SKILL_LINES_MAX = 12
KEYWORDS_MAX = 200
EVIDENCE_BYTES_MAX = 1024 * 1024
FACTS_MAX = 2000
RULES_MAX = 300
LOCAL_RULING_VERSION = 1000  # sorts after every shipped ruling

SEVERITIES = ("blocker", "major", "nit", "none")
SECTION_KINDS = ("experience", "research", "projects", "education", "skills", "other")
PACKAGES = {
    "pypdf": "pypdf==7.1.1",
    "strictyaml": "strictyaml==1.6.2",
    "yamllint": "yamllint==1.38.0",
}


class InputError(Exception):
    """Bad input from the friend: exit 2, never a pass."""


@dataclass
class Finding:
    rule_id: str
    location_id: str
    severity: str
    evidence: str
    why: str
    action: str
    fixable: bool = True

    def __post_init__(self) -> None:
        assert self.severity in SEVERITIES, self.severity
        assert self.rule_id and self.location_id


@dataclass
class Problem:
    file: str
    line: int
    column: int
    message: str


@dataclass
class FitWarning:
    keyword: str
    reason: str


@dataclass
class Settings:
    page_rule: int | str  # 1, 2 or "none"
    mode: str  # "opinionated" or "flexible"
    evidence: str
    runner: str  # "uv" or "venv"
    build: str = "latexmk -pdf {tex}"
    academic: bool = False
    body_font_pt: float | None = None  # recorded by setup on the first build
    extra: dict[str, Any] = field(default_factory=dict[str, Any])


def require(modules: list[str], runner: str = "uv") -> None:
    """Exits 3 with the install command when a package is missing; never a traceback."""
    missing = [m for m in modules if importlib.util.find_spec(m) is None]
    if not missing:
        return
    pins = " ".join(PACKAGES.get(m, m) for m in missing)
    how = f"uv run --with {pins} <script>" if runner == "uv" else f"python3 -m pip install {pins}"
    print(f"missing package: {', '.join(missing)}. Install: {how}", file=sys.stderr)
    sys.exit(3)


def load_settings(path: Path) -> Settings:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise InputError(f"{path}: {e}") from e
    known = {
        k: raw.pop(k) for k in list(raw) if k in Settings.__dataclass_fields__ and k != "extra"
    }
    s = Settings(**known, extra=raw)
    if s.page_rule not in (1, 2, "none"):
        raise InputError(f'{path}: page_rule must be 1, 2 or "none", got {s.page_rule!r}')
    if s.mode not in ("opinionated", "flexible"):
        raise InputError(f"{path}: mode must be opinionated or flexible, got {s.mode!r}")
    if s.runner not in ("uv", "venv"):
        raise InputError(f"{path}: runner must be uv or venv, got {s.runner!r}")
    return s


def check_pdf_size(path: Path) -> None:
    """Bad input, not a crash: pypdf would read a huge or missing file whole."""
    try:
        size_bytes = path.stat().st_size
    except OSError as e:
        raise InputError(f"{path}: {e}") from e
    if size_bytes > PDF_BYTES_MAX:
        raise InputError(f"{path}: {size_bytes} bytes: PDF_BYTES_MAX ({PDF_BYTES_MAX}) hit")


def write_json(path: Path, obj: Any) -> None:
    data = asdict(obj) if hasattr(obj, "__dataclass_fields__") else obj
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_json(path: Path, max_bytes: int = AGENT_JSON_BYTES_MAX) -> Any:
    try:
        size = path.stat().st_size
        if size > max_bytes:
            raise InputError(f"{path}: {size} bytes, over the limit {max_bytes}")
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise InputError(f"{path}: {e}") from e


SCHEMA_DEPTH_MAX = 8  # our schemas nest 4 deep; deeper input is refused


def schema_errors(obj: Any, schema: dict[str, Any], where: str = "$", depth: int = 0) -> list[str]:
    """The JSON Schema subset our agent schemas use: type, enum, required, properties,
    additionalProperties false, items, maxItems, maxLength. No package, so no install."""
    if depth > SCHEMA_DEPTH_MAX:
        return [f"{where}: nested deeper than {SCHEMA_DEPTH_MAX}"]
    if "enum" in schema:
        return [] if obj in schema["enum"] else [f"{where}: {obj!r} not one of {schema['enum']}"]
    kind = schema.get("type")
    if kind == "string":
        if not isinstance(obj, str):
            return [f"{where}: expected a string"]
        n = schema.get("maxLength")
        return [f"{where}: longer than {n} characters"] if n is not None and len(obj) > n else []
    if kind == "array":
        return array_errors(obj, schema, where, depth)
    if kind == "object":
        return object_errors(obj, schema, where, depth)
    raise AssertionError(f"schema type {kind!r} not supported")


def array_errors(obj: Any, schema: dict[str, Any], where: str, depth: int) -> list[str]:
    if not isinstance(obj, list):
        return [f"{where}: expected a list"]
    items = cast(list[Any], obj)
    n = schema.get("maxItems")
    if n is not None and len(items) > n:
        return [f"{where}: more than {n} items"]
    out: list[str] = []
    for i, x in enumerate(items):
        out += schema_errors(x, schema["items"], f"{where}[{i}]", depth + 1)
    return out


def object_errors(obj: Any, schema: dict[str, Any], where: str, depth: int) -> list[str]:
    if not isinstance(obj, dict):
        return [f"{where}: expected an object"]
    d = cast(dict[str, Any], obj)
    props: dict[str, Any] = schema.get("properties", {})
    out = [f"{where}: missing {k}" for k in schema.get("required", []) if k not in d]
    if schema.get("additionalProperties") is False:
        out += [f"{where}: unexpected key {k}" for k in d if k not in props]
    for k, sub in props.items():
        if k in d:
            out += schema_errors(d[k], sub, f"{where}.{k}", depth + 1)
    return out


def findings_out(path: Path, findings: list[Finding], extra: dict[str, Any] | None = None) -> None:
    write_json(path, {"findings": [asdict(f) for f in findings], **(extra or {})})
