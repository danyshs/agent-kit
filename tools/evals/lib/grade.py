"""Code graders and the append-only ledger (design 3.5, 5.3).

Graders are pure functions over a RunView and validated args (load_cases checks the args).
No grader passes on exit status alone, no evidence is FAIL, UNKNOWN never passes.
"""

from __future__ import annotations

import ast
import itertools
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, cast

from lib.dataset import Assertion
from lib.hidden import is_solution_name, run_hidden_tests
from lib.run import FAILURE_STATUSES, Event, Grade, RunRecord, RunView, Verdict, arm_text

Result = tuple[Verdict, str]  # (verdict, evidence)
Grader = Callable[[RunView, dict[str, object], Path], Result]

WRITE_TOOLS: Final = ("fs_write", "str_replace")
EXEC_TOOL: Final = "execute_bash"
PASSING_STATUSES: Final = ("ok", "denied_only")
IMPLICIT_ASSERTION_ID: Final = "no_outside_read"
HEREDOC_RE: Final = re.compile(r"<<-?\s*['\"]?\w+")
FINDING_RE: Final = re.compile(r"^FINDING (P[0-3]) (\S+):(\d+) (.+)$", re.MULTILINE)
FINDING_PREFIX: Final = "FINDING "
FINDINGS_DONE: Final = "FINDINGS_DONE"
FENCE_RE: Final = re.compile(r"```([A-Za-z0-9_+-]*)\n(.*?)```", re.DOTALL)
PYTHON_FENCE_TAGS: Final = ("", "python", "py")
PYRIGHT_STRICT_HEADER: Final = "# pyright: strict\n"
FINDING_LINE_TOLERANCE: Final = 3  # proposed (3.4.1)
# grader-local bounds; each is far above any real extracted snippet
AST_NODES_MAX: Final = 200_000
FINDINGS_MAX: Final = 1_000
FISH_TIMEOUT_S: Final = 10.0
PYRIGHT_TIMEOUT_S: Final = 90.0


# ---------------------------------------------------------------- run-view helpers


def _args_of(event: Event) -> dict[str, object]:
    assert event.kind == "tool_call", "only tool calls carry args"
    try:
        value: object = json.loads(event.args_json or "{}")
    except json.JSONDecodeError:
        return {}
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _calls(view: RunView, tool: str | None) -> list[Event]:
    """Attempted calls: every tool_call event, whatever its result (denied ones count)."""
    calls = [e for e in view.events if e.kind == "tool_call" and (tool is None or e.name == tool)]
    assert all(e.kind == "tool_call" for e in calls), "only tool_call events"
    return calls


def _commands(view: RunView) -> list[str]:
    commands: list[str] = []
    for event in _calls(view, EXEC_TOOL):
        command = _args_of(event).get("command")
        if isinstance(command, str):
            commands.append(command)
    assert len(commands) <= len(_calls(view, EXEC_TOOL)), "at most one command per call"
    return commands


def _scope_text(view: RunView, scope: str) -> str:
    assert scope in ("final_text", "first_line", "write_inputs"), "scope is validated"
    if scope == "final_text":
        return view.final_text
    if scope == "first_line":
        lines = [line for line in view.final_text.splitlines() if line.strip()]
        return lines[0] if lines else ""
    parts = [e.args_json or "" for tool in WRITE_TOOLS for e in _calls(view, tool)]
    assert all(isinstance(part, str) for part in parts), "write inputs are text"
    return "\n".join(parts)


def _str_arg(args: dict[str, object], key: str) -> str:
    value = args.get(key)
    assert isinstance(value, str), f"arg {key} is a validated string"
    return value


def _num_arg(args: dict[str, object], key: str) -> float:
    value = args.get(key)
    assert isinstance(value, (int, float)) and not isinstance(value, bool), f"arg {key} numeric"
    return float(value)


# ---------------------------------------------------------------- skill and tool graders


def g_skill_read(view: RunView, args: dict[str, object], root: Path) -> Result:
    skill = _str_arg(args, "skill")
    hits = [e for e in view.events if e.kind == "skill_read" and e.name == skill]
    return ("PASS" if hits else "FAIL"), f"{len(hits)} skill_read events for {skill}"


def g_skill_not_read(view: RunView, args: dict[str, object], root: Path) -> Result:
    skill = _str_arg(args, "skill")
    hits = [e for e in view.events if e.kind == "skill_read" and e.name == skill]
    return ("PASS" if not hits else "FAIL"), f"{len(hits)} skill_read events for {skill}"


def g_tool_used(view: RunView, args: dict[str, object], root: Path) -> Result:
    tool, min_count = (
        _str_arg(args, "tool"),
        int(_num_arg(args, "min_count")) if "min_count" in args else 1,
    )
    count = len(_calls(view, tool))
    assert min_count >= 1, "min_count is at least 1"
    assert count >= 0 and tool, "a counted tool name"
    return (
        "PASS" if count >= min_count else "FAIL"
    ), f"{count} attempted {tool} calls, need {min_count}"


def g_tool_not_used(view: RunView, args: dict[str, object], root: Path) -> Result:
    tool = _str_arg(args, "tool")
    count = len(_calls(view, tool))
    return ("PASS" if count == 0 else "FAIL"), f"{count} attempted {tool} calls"


def g_tool_args_match(view: RunView, args: dict[str, object], root: Path) -> Result:
    tool, regex, expect = (
        _str_arg(args, "tool"),
        _str_arg(args, "arg_regex"),
        _str_arg(args, "expect"),
    )
    calls = _calls(view, tool)
    assert expect in ("present", "absent"), "expect is validated"
    assert regex, "the regex is non-empty"
    if not calls:
        return "FAIL", f"no attempted {tool} call: no evidence"
    matching = [e for e in calls if re.search(regex, e.args_json or "")]
    assert len(matching) <= len(calls), "matches are a subset of calls"
    ok = bool(matching) if expect == "present" else not matching
    return (
        "PASS" if ok else "FAIL"
    ), f"{len(matching)} of {len(calls)} {tool} calls match, expect {expect}"


def g_calls_max(view: RunView, args: dict[str, object], root: Path) -> Result:
    tool, limit = args.get("tool"), int(_num_arg(args, "max"))
    assert tool is None or isinstance(tool, str), "tool is a name or null"
    count = len(_calls(view, tool))
    return ("PASS" if count <= limit else "FAIL"), f"{count} attempted calls, max {limit}"


# ---------------------------------------------------------------- text and shell graders


def g_text_matches(view: RunView, args: dict[str, object], root: Path) -> Result:
    text = _scope_text(view, _str_arg(args, "scope"))
    if not text.strip():
        return "FAIL", "scope text is empty: no evidence"
    found = re.search(_str_arg(args, "regex"), text, re.MULTILINE) is not None
    return ("PASS" if found else "FAIL"), f"regex {'found' if found else 'not found'} in scope"


def g_text_absent(view: RunView, args: dict[str, object], root: Path) -> Result:
    text = _scope_text(view, _str_arg(args, "scope"))
    if not text.strip():
        return "FAIL", "scope text is empty: no evidence"
    found = re.search(_str_arg(args, "regex"), text, re.MULTILINE) is not None
    return ("FAIL" if found else "PASS"), f"regex {'found' if found else 'not found'} in scope"


def _fish_problem(command: str) -> str | None:
    """None when `fish -n` parses the command with empty stderr; never executes it."""
    assert command.strip(), "a command to parse"
    assert FISH_TIMEOUT_S > 0, "fish gets a positive timeout"
    try:
        done = subprocess.run(
            ["fish", "--no-execute", "-c", command],
            capture_output=True,
            text=True,
            timeout=FISH_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "fish -n timed out"
    clean = done.returncode == 0 and done.stderr == ""
    return None if clean else (done.stderr.strip() or f"exit {done.returncode}")


def g_fish_parses(view: RunView, args: dict[str, object], root: Path) -> Result:
    commands = _commands(view)
    if not commands:
        return "FAIL", "no attempted execute_bash call: no evidence"
    if shutil.which("fish") is None:
        return "UNKNOWN", "fish is not installed"
    problems = [p for p in (_fish_problem(c) for c in commands) if p is not None]
    assert len(problems) <= len(commands), "problems are a subset of commands"
    assert all(problems), "a problem always has text"
    return ("FAIL" if problems else "PASS"), (
        problems[0][:200] if problems else f"{len(commands)} commands parse"
    )


def g_no_heredoc(view: RunView, args: dict[str, object], root: Path) -> Result:
    commands = _commands(view)
    if not commands:
        return "FAIL", "no attempted execute_bash call: no evidence"
    hits = [c for c in commands if HEREDOC_RE.search(c)]
    return ("FAIL" if hits else "PASS"), f"{len(hits)} of {len(commands)} commands have a heredoc"


def g_commit_message_clean(view: RunView, args: dict[str, object], root: Path) -> Result:
    banned = cast(list[str], args.get("banned_regex"))
    assert isinstance(banned, list) and banned, "banned_regex is a validated non-empty list"
    commits = [c for c in _commands(view) if "git commit" in c]
    if not commits:
        return "FAIL", "no attempted git commit: no evidence"
    hits = [c for c in commits if any(re.search(pattern, c) for pattern in banned)]
    assert len(hits) <= len(commits), "hits are a subset of commits"
    return (
        "FAIL" if hits else "PASS"
    ), f"{len(hits)} of {len(commits)} commits match a banned regex"


def g_stderr_empty(view: RunView, args: dict[str, object], root: Path) -> Result:
    return (
        "PASS" if view.stderr_text == "" else "FAIL"
    ), f"stderr is {len(view.stderr_text)} chars"


def g_no_outside_read(view: RunView, args: dict[str, object], root: Path) -> Result:
    ok = view.status != "contaminated"
    return ("PASS" if ok else "FAIL"), f"run status {view.status}"


# ---------------------------------------------------------------- findings graders


@dataclass(frozen=True)
class Finding:
    severity: str
    file: str
    line: int
    text: str


@dataclass(frozen=True)
class Planted:
    id: str
    file: str
    line: int
    in_category: bool
    allowed_severities: tuple[str, ...]


def parse_findings(final_text: str) -> list[Finding] | None:
    """Findings, or None when there is no parse: FINDINGS_DONE not last, or a bad FINDING line."""
    assert FINDINGS_MAX > 0, "the finding bound is positive"
    lines = [line.strip() for line in final_text.splitlines() if line.strip()]
    if not lines or lines[-1] != FINDINGS_DONE:
        return None
    body = lines[:-1]
    if len(body) > FINDINGS_MAX:
        return None
    findings: list[Finding] = []
    for line in body:
        if not line.startswith(FINDING_PREFIX):
            continue
        match = FINDING_RE.fullmatch(line)
        if match is None:
            return None
        findings.append(
            Finding(match.group(1), match.group(2), int(match.group(3)), match.group(4))
        )
    assert len(findings) <= len(body), "a finding is a body line"
    return findings


def load_planted(path: Path) -> list[Planted] | str:
    assert path.suffix == ".json", "planted files are json"
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as err:
        return f"cannot read planted file: {err}"
    root_obj = cast(dict[str, object], value) if isinstance(value, dict) else {}
    items = root_obj.get("items")
    if not isinstance(items, list):
        return "planted file has no items list"
    planted: list[Planted] = []
    for raw in cast(list[object], items):
        item = cast(dict[str, object], raw) if isinstance(raw, dict) else {}
        severities = item.get("allowed_severities")
        if not (
            isinstance(item.get("id"), str)
            and isinstance(item.get("file"), str)
            and isinstance(item.get("line"), int)
            and isinstance(severities, list)
        ):
            return "planted item needs id, file, line, allowed_severities"
        planted.append(
            Planted(
                cast(str, item["id"]),
                cast(str, item["file"]),
                cast(int, item["line"]),
                item.get("in_tiger_style_categories") is True,
                tuple(str(s) for s in cast(list[object], severities)),
            )
        )
    assert len(planted) == len(cast(list[object], items)), "every item parsed"
    return planted


def load_extras(path: Path) -> list[Planted] | str:
    """Acceptable extras (fixture v2): real issues outside the key. They count neither for nor
    against precision. An absent key means none, so v1 files grade as before."""
    assert path.suffix == ".json", "planted files are json"
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as err:
        return f"cannot read planted file: {err}"
    root_obj = cast(dict[str, object], value) if isinstance(value, dict) else {}
    raw_list = root_obj.get("acceptable_extras", [])
    if not isinstance(raw_list, list):
        return "acceptable_extras must be a list"
    extras: list[Planted] = []
    for raw in cast(list[object], raw_list):
        item = cast(dict[str, object], raw) if isinstance(raw, dict) else {}
        if not (
            isinstance(item.get("id"), str)
            and isinstance(item.get("file"), str)
            and isinstance(item.get("line"), int)
            and isinstance(item.get("note"), str)
        ):
            return "acceptable extra needs id, file, line, note"
        extras.append(
            Planted(
                cast(str, item["id"]), cast(str, item["file"]), cast(int, item["line"]), False, ()
            )
        )
    assert len(extras) == len(cast(list[object], raw_list)), "every extra parsed"
    assert len({e.id for e in extras}) == len(extras), "extra ids are unique"
    return extras


def match_findings(findings: list[Finding], items: list[Planted]) -> dict[int, Planted]:
    """One-to-one greedy (3.4.1): closest same-file item within tolerance; ties go to the lower
    planted line, then the lower id. Rule name and free text are never used."""
    taken: dict[int, Planted] = {}
    used: set[str] = set()
    for index, finding in enumerate(findings):
        near = [
            item
            for item in items
            if item.id not in used
            and item.file == finding.file
            and abs(finding.line - item.line) <= FINDING_LINE_TOLERANCE
        ]
        if near:
            best = min(near, key=lambda item: (abs(finding.line - item.line), item.line, item.id))
            taken[index] = best
            used.add(best.id)
    assert len(taken) == len(used), "each planted item is taken at most once"
    assert len(taken) <= min(len(findings), len(items)), "matches are one-to-one"
    return taken


def _ratio(numerator: int, denominator: int) -> float:
    assert 0 <= numerator <= denominator and denominator > 0, "a ratio of a subset"
    return numerator / denominator


def _planted_verdict(
    mode: str,
    minimum: float,
    findings: list[Finding],
    items: list[Planted],
    extras: list[Planted],
) -> Result:
    taken = match_findings(findings, items)
    assert mode in ("recall", "recall_outside", "precision", "severity"), "mode is validated"
    assert 0.0 <= minimum <= 1.0, "min is a fraction"
    if mode == "recall_outside":  # 6.3: do the 2 defects outside the checklist get found?
        outside = {i.id for i in items if not i.in_category}
        if not outside:
            return "FAIL", "no outside-category items: no evidence"
        hits = len({item.id for item in taken.values()} & outside)
        value = _ratio(hits, len(outside))
        verdict: Verdict = "PASS" if value >= minimum else "FAIL"
        return verdict, f"outside recall {value:.2f} (min {minimum}); {hits}/{len(outside)}"
    if mode == "recall":
        if not items:
            return "FAIL", "no planted items: no evidence"
        inside = [i for i in items if i.in_category]
        got = {item.id for item in taken.values()}
        detail = (
            f"in-category {len(got & {i.id for i in inside})}/{len(inside)}, "
            f"outside {len(got - {i.id for i in inside})}/{len(items) - len(inside)}"
        )
        value = _ratio(len(taken), len(items))
        return (
            "PASS" if value >= minimum else "FAIL"
        ), f"recall {value:.2f} (min {minimum}); {detail}"
    if mode == "precision":
        if not findings:
            return "FAIL", "no findings: no evidence"
        # acceptable extras (fixture v2) leave the denominator: real, but not in the key
        spare = [f for i, f in enumerate(findings) if i not in taken]
        extra_hits = len(match_findings(spare, extras)) if extras else 0
        counted = len(findings) - extra_hits
        assert counted >= len(taken) and counted > 0, "extras only remove unmatched findings"
        value = _ratio(len(taken), counted)
        note = f"; {extra_hits} acceptable extras excluded" if extras else ""
        return (
            ("PASS" if value >= minimum else "FAIL"),
            f"precision {value:.2f} (min {minimum}{note}); {len(taken)}/{counted}",
        )
    assert mode == "severity", "mode is validated"
    if not taken:
        return "FAIL", "no matched findings: no evidence"
    allowed = sum(1 for i, item in taken.items() if findings[i].severity in item.allowed_severities)
    value = _ratio(allowed, len(taken))
    return (
        ("PASS" if value >= minimum else "FAIL"),
        f"severity {value:.2f} (min {minimum}); {allowed}/{len(taken)}",
    )


def g_planted_found(view: RunView, args: dict[str, object], root: Path) -> Result:
    findings = parse_findings(view.final_text)
    if findings is None:
        return "FAIL", "no parse: FINDINGS_DONE missing or a FINDING line is malformed"
    items = load_planted(root / _str_arg(args, "planted"))
    if isinstance(items, str):
        return "UNKNOWN", items
    extras = load_extras(root / _str_arg(args, "planted"))
    if isinstance(extras, str):
        return "UNKNOWN", extras
    return _planted_verdict(_str_arg(args, "mode"), _num_arg(args, "min"), findings, items, extras)


def g_clean_control_quiet(view: RunView, args: dict[str, object], root: Path) -> Result:
    findings = parse_findings(view.final_text)
    if findings is None:
        return "FAIL", "no parse: FINDINGS_DONE missing or a FINDING line is malformed"
    loud = [f for f in findings if f.severity in ("P0", "P1")]
    return ("FAIL" if loud else "PASS"), f"{len(loud)} P0/P1 findings of {len(findings)}"


def _cites_real_line(cwd: Path, finding: Finding) -> bool:
    assert cwd.is_absolute(), "cwd is resolved"
    assert finding.file, "a finding names a file"
    path = (cwd / finding.file).resolve()
    if not (path == cwd or cwd in path.parents) or not path.is_file():
        return False
    try:
        length = len(path.read_text(encoding="utf-8").splitlines())
    except (OSError, UnicodeDecodeError):
        return False
    return 1 <= finding.line <= length


def g_citation_valid(view: RunView, args: dict[str, object], root: Path) -> Result:
    findings = parse_findings(view.final_text)
    if not findings:
        return "FAIL", "no parse or no findings: no evidence"
    cwd = Path(view.cwd).resolve()
    bad = [f for f in findings if not _cites_real_line(cwd, f)]
    return ("FAIL" if bad else "PASS"), f"{len(bad)} of {len(findings)} citations invalid"


# ---------------------------------------------------------------- extracted-code graders


def extracted_python(view: RunView) -> list[str]:
    """Python sources: first attempted fs_write per .py path, else python fenced blocks (3.4.1)."""
    first_per_path: dict[str, str] = {}
    for event in _calls(view, "fs_write"):
        args = _args_of(event)
        path, text = args.get("path"), args.get("text")
        if isinstance(path, str) and isinstance(text, str) and path.endswith(".py"):
            first_per_path.setdefault(path, text)
    if first_per_path:
        return list(first_per_path.values())
    blocks = [
        m.group(2) for m in FENCE_RE.finditer(view.final_text) if m.group(1) in PYTHON_FENCE_TAGS
    ]
    assert all(isinstance(block, str) for block in blocks), "blocks are text"
    assert len(first_per_path) == 0, "fences are only the fallback when no .py write exists"
    return blocks


def _trees(view: RunView) -> list[ast.Module] | str:
    sources = extracted_python(view)
    assert all(isinstance(source, str) for source in sources), "sources are text"
    if not sources:
        return "no extractable code: no evidence"
    trees: list[ast.Module] = []
    for source in sources:
        try:
            trees.append(ast.parse(source))
        except SyntaxError as err:
            return f"extracted code does not parse: {err.msg} line {err.lineno}"
    assert len(trees) == len(sources), "one tree per source"
    return trees


def _functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    nodes = itertools.islice(ast.walk(tree), AST_NODES_MAX)
    funcs = [n for n in nodes if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert len(funcs) <= AST_NODES_MAX, "bounded by the node cap"
    return funcs


def _own_assert_count(func: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Assert statements in func, not counting nested function bodies."""
    stack: list[ast.AST] = list(func.body)
    count = 0
    for _ in range(AST_NODES_MAX):
        if not stack:
            break
        node = stack.pop()
        if isinstance(node, ast.Assert):
            count += 1
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(node))
    assert count >= 0, "count is not negative"
    assert not stack, "the function body fit within AST_NODES_MAX nodes"
    return count


def _over_trees(view: RunView, check: Callable[[ast.Module], list[str]]) -> Result:
    trees = _trees(view)
    if isinstance(trees, str):
        return "FAIL", trees
    problems = [p for tree in trees for p in check(tree)]
    return ("FAIL" if problems else "PASS"), (
        problems[0] if problems else f"{len(trees)} sources ok"
    )


def _length_lines(func: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """def line to last line, minus the docstring, as the rule counts it. Decorators sit above
    func.lineno, so they are already outside."""
    length = (func.end_lineno or func.lineno) - func.lineno + 1
    first = func.body[0]
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        length -= (first.end_lineno or first.lineno) - first.lineno + 1
    assert length >= 1, "the def line always counts"
    return length


def g_function_length_max(view: RunView, args: dict[str, object], root: Path) -> Result:
    limit = int(_num_arg(args, "max_lines"))
    assert limit >= 1, "max_lines is validated positive"
    assert AST_NODES_MAX > 0, "the node cap is positive"

    def check(tree: ast.Module) -> list[str]:
        return [
            f"{f.name} is {length} lines, max {limit}"
            for f in _functions(tree)
            if (length := _length_lines(f)) > limit
        ]

    return _over_trees(view, check)


def g_assertions_per_function_min(view: RunView, args: dict[str, object], root: Path) -> Result:
    minimum = int(_num_arg(args, "min"))
    assert minimum >= 1, "min is validated positive"
    assert AST_NODES_MAX > 0, "the node cap is positive"

    def check(tree: ast.Module) -> list[str]:
        return [
            f"{f.name} has {_own_assert_count(f)} asserts, min {minimum}"
            for f in _functions(tree)
            if _own_assert_count(f) < minimum
        ]

    return _over_trees(view, check)


def g_loops_bounded(view: RunView, args: dict[str, object], root: Path) -> Result:
    assert AST_NODES_MAX > 0, "the node cap is positive"
    assert not args, "g_loops_bounded takes no args"

    def check(tree: ast.Module) -> list[str]:
        nodes = itertools.islice(ast.walk(tree), AST_NODES_MAX)
        whiles = [n for n in nodes if isinstance(n, ast.While)]
        return [
            f"while at line {w.lineno} compares no bound variable"
            for w in whiles
            if not any(isinstance(n, ast.Compare) for n in ast.walk(w.test))
        ]

    return _over_trees(view, check)


def _identifiers(trees: list[ast.Module]) -> list[str]:
    """Every name the code binds or uses; comments and strings are not in the AST."""
    assert trees, "at least one parsed source"
    found: list[str] = []
    for tree in trees:
        for node in itertools.islice(ast.walk(tree), AST_NODES_MAX):
            if isinstance(node, ast.Name):
                found.append(node.id)
            elif isinstance(node, ast.arg):
                found.append(node.arg)
            elif isinstance(node, ast.Attribute):
                found.append(node.attr)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.append(node.name)
            elif isinstance(node, ast.keyword) and node.arg is not None:
                found.append(node.arg)
    assert len(found) <= AST_NODES_MAX, "bounded by the node cap"
    return found


def g_units_in_names(view: RunView, args: dict[str, object], root: Path) -> Result:
    trees = _trees(view)
    if isinstance(trees, str):
        return "FAIL", trees
    names = cast(list[dict[str, object]], args.get("names"))
    assert isinstance(names, list) and names, "names is a validated non-empty list"
    identifiers = _identifiers(trees)
    assert all(isinstance(t, str) for t in identifiers), "names are text"
    for entry in names:
        identifier, suffixes = (
            _str_arg(entry, "identifier"),
            tuple(cast(list[str], entry["suffixes"])),
        )
        tokens = [t for t in identifiers if t.startswith(identifier)]
        if not tokens:
            return "FAIL", f"{identifier} does not occur: no evidence"
        bad = [t for t in tokens if not t.endswith(suffixes)]
        if bad:
            return "FAIL", f"{bad[0]} does not end with one of {list(suffixes)}"
    return "PASS", f"{len(names)} identifiers carry their unit suffix"


def _pyright_counts(sources: list[str]) -> tuple[int, int] | str:
    """(errors, warnings) from pyright's JSON summary, or why it could not run."""
    exe = shutil.which("pyright")
    assert sources and all(isinstance(source, str) for source in sources), "sources to check"
    if exe is None:
        return "pyright is not installed"
    with tempfile.TemporaryDirectory(prefix="evpyright-") as scratch:
        for number, source in enumerate(sources):
            (Path(scratch) / f"snippet_{number}.py").write_text(
                PYRIGHT_STRICT_HEADER + source, encoding="utf-8"
            )
        try:
            done = subprocess.run(
                [exe, "--outputjson", scratch],
                capture_output=True,
                text=True,
                timeout=PYRIGHT_TIMEOUT_S,
                check=False,
                cwd=scratch,
            )
        except subprocess.TimeoutExpired:
            return "pyright timed out"
    try:
        summary = cast(dict[str, object], json.loads(done.stdout))["summary"]
        counts = cast(dict[str, int], summary)
        errors, warnings = counts["errorCount"], counts["warningCount"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return f"pyright output not understood (exit {done.returncode})"
    assert errors >= 0 and warnings >= 0, "counts are not negative"
    return errors, warnings


def g_pyright_clean(view: RunView, args: dict[str, object], root: Path) -> Result:
    sources = extracted_python(view)
    if not sources:
        return "FAIL", "no extractable code: no evidence"
    counts = _pyright_counts(sources)
    if isinstance(counts, str):
        return "UNKNOWN", counts
    errors, warnings = counts
    assert errors >= 0 and warnings >= 0, "counts are not negative"
    assert isinstance(sources[0], str), "sources are text"
    return (
        "PASS" if errors == 0 and warnings == 0 else "FAIL"
    ), f"pyright {errors} errors, {warnings} warnings"


FISH_FENCE_TAGS: Final = ("fish",)


def extracted_files(view: RunView, module: str) -> dict[str, str]:
    """Solution files by name: the first attempted fs_write per .py/.fish base name, else the
    first python and first fish fenced block, saved as <module>.py / <module>.fish (3.4.1)."""
    assert module and module.isidentifier(), "the module name is validated"
    files: dict[str, str] = {}
    for event in _calls(view, "fs_write"):
        args = _args_of(event)
        path, text = args.get("path"), args.get("text")
        if isinstance(path, str) and isinstance(text, str):
            name = Path(path).name
            if name and is_solution_name(name):
                files.setdefault(name, text)
    if files:
        return files
    for tags, suffix in ((PYTHON_FENCE_TAGS[1:], ".py"), (FISH_FENCE_TAGS, ".fish")):
        blocks = [m.group(2) for m in FENCE_RE.finditer(view.final_text) if m.group(1) in tags]
        if blocks:
            files[module + suffix] = blocks[0]
    assert all(is_solution_name(name) for name in files), "only safe names leave this function"
    return files


def g_hidden_tests_pass(view: RunView, args: dict[str, object], root: Path) -> Result:
    tests_dir = root / _str_arg(args, "tests_dir")
    files = extracted_files(view, _str_arg(args, "module"))
    if not files:
        return "FAIL", "no extractable code: no evidence"
    assert tests_dir.is_dir(), "tests_dir is validated by load_cases"
    verdict, evidence = run_hidden_tests(files, tests_dir)
    assert evidence, "a verdict always carries evidence"
    return verdict, evidence


GRADERS: Final[dict[str, Grader]] = {
    "g_skill_read": g_skill_read,
    "g_skill_not_read": g_skill_not_read,
    "g_tool_used": g_tool_used,
    "g_tool_not_used": g_tool_not_used,
    "g_tool_args_match": g_tool_args_match,
    "g_calls_max": g_calls_max,
    "g_text_matches": g_text_matches,
    "g_text_absent": g_text_absent,
    "g_fish_parses": g_fish_parses,
    "g_no_heredoc": g_no_heredoc,
    "g_commit_message_clean": g_commit_message_clean,
    "g_planted_found": g_planted_found,
    "g_clean_control_quiet": g_clean_control_quiet,
    "g_citation_valid": g_citation_valid,
    "g_function_length_max": g_function_length_max,
    "g_assertions_per_function_min": g_assertions_per_function_min,
    "g_loops_bounded": g_loops_bounded,
    "g_units_in_names": g_units_in_names,
    "g_pyright_clean": g_pyright_clean,
    "g_hidden_tests_pass": g_hidden_tests_pass,
    "g_no_outside_read": g_no_outside_read,
    "g_stderr_empty": g_stderr_empty,
}


# ---------------------------------------------------------------- grading and ledger


def grade_run(view: RunView, assertions: tuple[Assertion, ...], root: Path) -> list[Grade]:
    """One Grade per assertion plus the implicit g_no_outside_read (applies to every case)."""
    assert assertions, "a case has assertions"
    grades: list[Grade] = []
    for assertion in assertions:
        if assertion.grader == "human":
            verdict, evidence = "UNKNOWN", "human grade pending"
        elif view.status in FAILURE_STATUSES:
            verdict, evidence = "UNKNOWN", f"run status {view.status}: nothing to grade"
        elif assertion.grader not in GRADERS:
            verdict, evidence = "UNKNOWN", f"grader {assertion.grader} is not built yet"
        else:
            verdict, evidence = GRADERS[assertion.grader](view, assertion.args, root)
        grades.append(Grade(assertion.id, verdict, assertion.grader, evidence))
    verdict, evidence = g_no_outside_read(view, {}, root)
    grades.append(Grade(IMPLICIT_ASSERTION_ID, verdict, "g_no_outside_read", evidence))
    assert len(grades) == len(assertions) + 1, "one grade per assertion plus the implicit one"
    return grades


def case_passed(grades: list[Grade], assertions: tuple[Assertion, ...], status: str) -> bool:
    """All hard assertions PASS, the implicit one too, and the run finished (ok/denied_only)."""
    hard_ids = {a.id for a in assertions if a.hard} | {IMPLICIT_ASSERTION_ID}
    verdicts = {g.assertion_id: g.verdict for g in grades}
    passed = status in PASSING_STATUSES and all(verdicts.get(i) == "PASS" for i in hard_ids)
    assert hard_ids >= {IMPLICIT_ASSERTION_ID}, "the implicit assertion is always required"
    return passed


def graded(
    record: RunRecord, view: RunView, assertions: tuple[Assertion, ...], root: Path
) -> RunRecord:
    grades = grade_run(view, assertions, root)
    assert grades[-1].assertion_id == IMPLICIT_ASSERTION_ID, "the implicit grade is last"
    result = replace(
        record, grades=tuple(grades), case_passed=case_passed(grades, assertions, record.status)
    )
    assert not (result.case_passed and result.status not in PASSING_STATUSES), (
        "only finished runs pass"
    )
    return result


def record_to_json(record: RunRecord) -> dict[str, object]:
    row: dict[str, object] = {
        "run_id": record.run_id,
        "case_id": record.case_id,
        "arm": arm_text(record.arm),
        "repeat_index": record.repeat_index,
        "engine": record.engine,
        "model_id": record.model_id,
        "cli_version": record.cli_version,
        "home_sha": record.home_sha,
        "status": record.status,
        "wall_ms": record.wall_ms,
        "credits": record.credits,
        "context_pct_last": record.context_pct_last,
        "tool_call_count": record.tool_call_count,
        "denied_count": record.denied_count,
        "skills_read": list(record.skills_read),
        "session_id": record.session_id,
        "stream_path": record.stream_path,
        "case_passed": record.case_passed,
        "note": record.note,
        "grades": [
            {
                "assertion_id": g.assertion_id,
                "verdict": g.verdict,
                "grader": g.grader,
                "evidence": g.evidence,
            }
            for g in record.grades
        ],
    }
    assert row["arm"] and row["model_id"], "every line carries its arm and model (5.5)"
    assert row["home_sha"] and row["engine"], "every line carries its home sha and engine"
    return row


def append_run(ledger_path: Path, record: RunRecord) -> str | None:
    """Append one line; None on success, else the reason. No update or delete function exists."""
    line = json.dumps(record_to_json(record), separators=(",", ":"))
    assert "\n" not in line, "one record is one line"
    assert json.loads(line)["run_id"] == record.run_id, "the line round-trips"
    try:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with ledger_path.open("a", encoding="utf-8") as ledger:
            ledger.write(line + "\n")
            ledger.flush()
            os.fsync(ledger.fileno())
    except OSError as err:
        return f"ledger write failed: {err}"
    return None
