"""Shared types, limits and guards for the resume-kit scripts (a sibling module, not a package)."""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

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
LOCAL_RULING_VERSION = 1000  # sorts after every Google ruling

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


def findings_out(path: Path, findings: list[Finding], extra: dict[str, Any] | None = None) -> None:
    write_json(path, {"findings": [asdict(f) for f in findings], **(extra or {})})
