"""Rhino-Owl launcher: the only way a rhino or the Owl starts.

Rule and usage: the rhino-owl rule and skill. Each rhino is a headless claude -p in its own worktree with a
dollar cap, the OS sandbox and a deny list. Its transcript is copied where guard.py counts it.

  rhino.py launch --sheet PATH --session ID [--user-present]
  rhino.py owl    --sheet PATH --session ID [--user-present]
  rhino.py remove --sheet PATH        (only after the user accepts the Owl's rows)

Rhinos start bare (no settings, rules or MCP) to stay cheap; a sheet's opt-in "scaffolding" key
loads chosen settings and rule files into rhinos, at the cost of each rhino's cap.

Exit 0 ok, 1 a run or git step failed, 2 refused (bad sheet, budget, model). Standard library
only; runs on python 3.9.
"""

from __future__ import annotations

import concurrent.futures
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

HERE = Path(__file__).resolve().parent
GUARD_PATH = HERE / "guard.py"  # a copy of the guards plugin's, to share its budget rules
OWL_BRIEF_PATH = HERE.parent / "brief" / "owl-brief.md"
RHINOS_MAX = 4  # same as the 4-launches-per-message rule
CAP_USD_MIN = 0.50  # a headless sonnet start costs ~$0.18 before any work (measured)
CAP_USD_MAX = 10.0
FIELD_CHARS_MAX = 2000
RUN_TIMEOUT_S = 1800  # wall clock per run, on top of the dollar cap
GIT_TIMEOUT_S = 30
SHEET_BYTES_MAX = 256 * 1024
NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
SESSION_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
ROW_FIELDS = ("name", "question", "direction", "axis", "label", "stop_point", "model")
LABELS = ("frontier", "control")
# no CLAUDE.md, rules, hooks or MCP, user's or project's: no startup tax
MCP_OFF = ["--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
SETTING_SOURCES = ("user", "project", "local")  # what claude --setting-sources accepts
RULE_FILES_MAX = 8
RULE_FILE_BYTES_MAX = 64 * 1024
RULE_BYTES_MAX = 128 * 1024  # all rule files together; each byte is paid on every rhino's start
SANDBOX = {"sandbox": {"enabled": True, "autoAllowBashIfSandboxed": True,
                       "allowUnsandboxedCommands": False}}
ALLOWED_TOOLS = "Bash Read Write Edit Glob Grep WebFetch WebSearch"
DENIED_TOOLS = "Agent Bash(sudo:*) Bash(git push:*) Bash(defaults write:*)"
CAPPED_SUBTYPE = "error_max_budget_usd"  # what claude -p reports when the cap stops it


class Refused(Exception):
    pass


@dataclass(frozen=True)
class Rhino:
    name: str
    question: str
    direction: str
    axis: str
    label: str
    stop_point: str
    model: str
    cap_usd: float
    device: str | None


@dataclass(frozen=True)
class Scaffolding:
    """Opt-in instructions for rhinos; the default is none, the cheap start."""
    setting_sources: tuple[str, ...]
    rule_files: tuple[str, ...]
    rules_text: str  # the rule files joined, appended to each rhino's system prompt


NO_SCAFFOLDING = Scaffolding((), (), "")


@dataclass(frozen=True)
class Sheet:
    path: Path
    rhinos: list[Rhino]
    owl_model: str
    owl_cap_usd: float
    scaffolding: Scaffolding


# -- the launch sheet


def text_field(row: dict[str, object], key: str, where: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise Refused(f"{where}: missing {key}")
    if len(value) > FIELD_CHARS_MAX:
        raise Refused(f"{where}: {key} is over {FIELD_CHARS_MAX} characters")
    return value.strip()


def cap_field(row: dict[str, object], key: str, where: str) -> float:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Refused(f"{where}: {key} must be a number of dollars")
    if not CAP_USD_MIN <= value <= CAP_USD_MAX:
        raise Refused(f"{where}: {key} {value} is outside {CAP_USD_MIN}-{CAP_USD_MAX}")
    return float(value)


def parse_rhino(row: object, index: int) -> Rhino:
    where = f"rhino {index + 1}"
    if not isinstance(row, dict):
        raise Refused(f"{where}: not an object")
    fields = {key: text_field(row, key, where) for key in ROW_FIELDS}
    if not NAME_PATTERN.fullmatch(fields["name"]):
        raise Refused(f"{where}: name must match {NAME_PATTERN.pattern}")
    if fields["label"] not in LABELS:
        raise Refused(f"{where}: label must be frontier or control")
    device = row.get("device")
    if device is not None and (not isinstance(device, str) or not NAME_PATTERN.fullmatch(device)):
        raise Refused(f"{where}: device must match {NAME_PATTERN.pattern}")
    return Rhino(cap_usd=cap_field(row, "cap_usd", where), device=device, **fields)


def read_rule_file(path: Path) -> str:
    if not path.is_file() or path.stat().st_size > RULE_FILE_BYTES_MAX:
        raise Refused(f"rule file {path} is missing or over {RULE_FILE_BYTES_MAX} bytes")
    try:
        return path.read_text()
    except UnicodeDecodeError as err:
        raise Refused(f"rule file {path} is not text: {err}") from err


def parse_scaffolding(raw: dict[str, object], base: Path) -> Scaffolding:
    value = raw.get("scaffolding")
    if value is None:
        return NO_SCAFFOLDING
    if not isinstance(value, dict) or set(value) - {"setting_sources", "rule_files"}:
        raise Refused("scaffolding takes only setting_sources and rule_files")
    sources: object = value.get("setting_sources", [])
    files: object = value.get("rule_files", [])
    if not isinstance(sources, list) or not isinstance(files, list):
        raise Refused("scaffolding: setting_sources and rule_files are lists")
    names = [source for source in sources if isinstance(source, str) and source in SETTING_SOURCES]
    if len(names) != len(sources) or len(set(names)) != len(names):
        raise Refused(f"setting_sources: drawn from {SETTING_SOURCES}, no repeats")
    paths = [Path(name).expanduser() for name in files if isinstance(name, str) and name]
    if len(paths) != len(files) or len(paths) > RULE_FILES_MAX:
        raise Refused(f"rule_files: up to {RULE_FILES_MAX} non-empty paths")
    if not names and not paths:
        raise Refused("scaffolding is empty; leave it out")
    rules_text = "\n\n".join(read_rule_file(base / path) for path in paths)
    if len(rules_text.encode()) > RULE_BYTES_MAX:
        raise Refused(f"rule files total over {RULE_BYTES_MAX} bytes")
    return Scaffolding(tuple(names), tuple(str(base / path) for path in paths), rules_text)


def load_sheet(path: Path) -> Sheet:
    if not path.is_file() or path.stat().st_size > SHEET_BYTES_MAX:
        raise Refused(f"sheet {path} is missing or over {SHEET_BYTES_MAX} bytes")
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        raise Refused(f"sheet {path} is not JSON: {err}") from err
    if not isinstance(raw, dict) or not isinstance(raw.get("rhinos"), list):
        raise Refused("sheet needs a rhinos list")
    rows = raw["rhinos"]
    if not 1 <= len(rows) <= RHINOS_MAX:
        raise Refused(f"sheet has {len(rows)} rhinos; allowed 1-{RHINOS_MAX}")
    rhinos = [parse_rhino(row, index) for index, row in enumerate(rows)]
    names = [rhino.name for rhino in rhinos]
    if len(set(names)) != len(names):
        raise Refused("rhino names must be unique")
    owl_model = text_field(raw, "owl_model", "sheet")
    scaffolding = parse_scaffolding(raw, path.resolve().parent)
    sheet = Sheet(path.resolve(), rhinos, owl_model, cap_field(raw, "owl_cap_usd", "sheet"),
                  scaffolding)
    assert len(sheet.rhinos) <= RHINOS_MAX
    return sheet


# -- budget and model, through guard.py so there is one authority


def load_guard() -> ModuleType:
    spec = importlib.util.spec_from_file_location("rom_guard", GUARD_PATH)
    assert spec is not None and spec.loader is not None, GUARD_PATH
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the module up while building classes
    spec.loader.exec_module(module)
    return module


def projects_dir() -> Path:
    return Path(os.environ.get("ROM_PROJECTS_DIR", str(Path.home() / ".claude" / "projects")))


def main_transcript(session: str) -> Path:
    if not SESSION_PATTERN.fullmatch(session):
        raise Refused(f"session id {session!r} is malformed")
    matches = sorted(projects_dir().glob(f"*/{session}.jsonl"))
    if len(matches) != 1:
        raise Refused(f"found {len(matches)} transcripts for session {session}; need exactly 1")
    return matches[0]


def check_budget(main_path: Path, models: list[str], total_usd: float) -> str:
    guard = load_guard()
    prices = guard.load_prices(guard.PRICES_PATH)
    try:
        for model in models:
            guard.check_ceiling(model, guard.rates_for_launch(model, prices).rates, prices)
        budget = guard.budget_line(main_path)
    except guard.Refusal as err:
        raise Refused(str(err)) from err
    if budget.off:
        return f"budget off; this launch caps at ${total_usd:.2f}"
    if budget.dollars is None or budget.since is None:
        raise Refused("no budget line or approved proposal; propose one first")
    spend = guard.spend_usd(guard.subagent_files(main_path), budget.since, prices)
    left_usd = budget.dollars - spend.dollars
    if total_usd > left_usd:
        raise Refused(f"caps total ${total_usd:.2f}, budget has ${left_usd:.2f} left")
    return f"caps total ${total_usd:.2f} of ${left_usd:.2f} left"


# -- running one headless session


def repo_root(start: Path) -> Path:
    out = subprocess.run(["git", "-C", str(start), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True, timeout=GIT_TIMEOUT_S, check=False)
    if out.returncode != 0:
        raise Refused(f"{start} is not inside a git repo")
    return Path(out.stdout.strip())


def claude_command(model: str, cap_usd: float, worktree: str | None, prompt: str,
                   scaffolding: Scaffolding = NO_SCAFFOLDING) -> list[str]:
    sources = ",".join(scaffolding.setting_sources)  # "" loads nothing: the cheap default
    command = [os.environ.get("ROM_CLAUDE_BIN", "claude"), "-p", "--model", model,
               "--max-budget-usd", f"{cap_usd:.2f}", "--output-format", "json",
               "--permission-mode", "acceptEdits", "--allowedTools", ALLOWED_TOOLS,
               "--disallowedTools", DENIED_TOOLS, "--settings", json.dumps(SANDBOX),
               "--setting-sources", sources, *MCP_OFF]
    if scaffolding.rules_text:
        command += ["--append-system-prompt", scaffolding.rules_text]
    if worktree is not None:
        command += ["-w", worktree]
    return command + [prompt]


def run_session(command: list[str], cwd: Path) -> dict[str, object]:
    """Run one claude -p; 'status' is set only when the run itself says capped or crashed."""
    try:
        out = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, timeout=RUN_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        return {"exit": None, "subtype": "timeout", "status": "crashed"}
    result: dict[str, object] = {"exit": out.returncode}
    lines = out.stdout.strip().splitlines()
    try:
        parsed = json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError:
        parsed = None
    if not isinstance(parsed, dict):
        result.update(subtype="no-json", status="crashed", stderr=out.stderr[-2000:])
        return result
    result.update(subtype=parsed.get("subtype"), cost_usd=parsed.get("total_cost_usd"),
                  session=parsed.get("session_id"))
    if parsed.get("subtype") == CAPPED_SUBTYPE:
        result["status"] = "capped"
    elif out.returncode != 0 or parsed.get("is_error"):
        result["status"] = "crashed"
    return result


def copy_transcript(main_path: Path, session: object) -> str | None:
    """Copy a finished run's transcript where guard.py counts it; None if it isn't found."""
    if not isinstance(session, str) or not SESSION_PATTERN.fullmatch(session):
        return None
    found = [path for path in projects_dir().glob(f"*/{session}.jsonl") if path != main_path]
    if len(found) != 1:
        return None
    target = main_path.with_suffix("") / "rhinos" / f"rhino-{session}.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(found[0], target)
    assert target.is_file(), target
    return str(found[0])


# -- rhinos


def rhino_prompt(rhino: Rhino, user_present: bool) -> str:
    kind = ("a route expected to be weaker, run on purpose as a baseline; still do it properly"
            if rhino.label == "control" else "the best current guess")
    device = (f"\nDevice: {rhino.device}. The user is present. Only commands that don't change its"
              " lasting state: no settings, SELinux or reboot changes, and remove nothing that"
              " isn't your own app." if rhino.device else "")
    assert not rhino.device or user_present
    return (
        "You are a rhino in a Rhino-Owl exploration. Build fast. Don't plan or write docs beyond"
        " the log below. Your code will be deleted; only what you observe matters, and failures"
        " count as much as successes.\n"
        f"Question: {rhino.question}\nYour direction: {rhino.direction}\n"
        f"It differs from the other rhinos on: {rhino.axis}\nLabel: {rhino.label} ({kind}).\n"
        f"Stop point: {rhino.stop_point}. Stop there; don't polish.{device}\n"
        "Log: append to rhino.log in the current directory as you go: what you tried, what you"
        " saw (exact errors and outputs), and every outside source you read (URL or path). When"
        " you stop by choice, the last line is 'STATUS: done', or 'STATUS: waiting-human' if a"
        " person must look at or do something physical; then first append the request (what to"
        " do, what to report) to human-queue.md.\n"
        "Stay inside the current directory. No sudo, git push, defaults write or system settings."
    )


def logged_status(log: Path) -> str:
    last = log.read_text().strip().splitlines()[-1:] if log.is_file() else []
    if last and last[0] in ("STATUS: done", "STATUS: waiting-human"):
        return last[0][len("STATUS: "):]
    return "unknown"


def finish_rhino(root: Path, main_path: Path, rhino: Rhino,
                 result: dict[str, object]) -> dict[str, object]:
    worktree = root / ".claude" / "worktrees" / f"rom-{rhino.name}"
    log = worktree / "rhino.log"
    if "status" in result and worktree.is_dir():
        with log.open("a") as handle:  # a capped or crashed rhino can't write its own status
            handle.write(f"STATUS: {result['status']}\n")
    if "status" not in result:
        result["status"] = logged_status(log)
    result.update(name=rhino.name, label=rhino.label, worktree=str(worktree), log=str(log),
                  transcript=copy_transcript(main_path, result.get("session")))
    assert result["status"] in ("done", "waiting-human", "capped", "crashed", "unknown")
    return result


def run_group(root: Path, main_path: Path, group: list[Rhino], user_present: bool,
              scaffolding: Scaffolding) -> list[dict[str, object]]:
    results = []
    for rhino in group:  # one device, one rhino at a time
        command = claude_command(rhino.model, rhino.cap_usd, f"rom-{rhino.name}",
                                 rhino_prompt(rhino, user_present), scaffolding)
        results.append(finish_rhino(root, main_path, rhino, run_session(command, root)))
    return results


def groups_of(rhinos: list[Rhino]) -> list[list[Rhino]]:
    by_device: dict[str, list[Rhino]] = {}
    alone: list[list[Rhino]] = []
    for rhino in rhinos:
        if rhino.device is None:
            alone.append([rhino])
        else:
            by_device.setdefault(rhino.device, []).append(rhino)
    groups = alone + list(by_device.values())
    assert sum(len(group) for group in groups) == len(rhinos)
    return groups


def scaffolding_notice(scaffolding: Scaffolding) -> str:
    sources = ",".join(scaffolding.setting_sources) or "none"
    size = len(scaffolding.rules_text.encode())
    return (f"rhino: NOTICE scaffolding on (setting sources: {sources}; "
            f"{len(scaffolding.rule_files)} rule files, {size} bytes). Rhinos are meant to start "
            "bare, which is what keeps them cheap. Every rhino now pays for this at startup, out "
            "of its own cap; a rhino whose cap goes mostly on startup stops before it builds much. "
            "Raise cap_usd to make up for it.")


def launch(sheet: Sheet, session: str, user_present: bool) -> int:
    if any(rhino.device for rhino in sheet.rhinos) and not user_present:
        raise Refused("a rhino needs a device; hardware rhinos run only with --user-present")
    root = repo_root(sheet.path.parent)
    for rhino in sheet.rhinos:
        if (root / ".claude" / "worktrees" / f"rom-{rhino.name}").exists():
            raise Refused(f"worktree rom-{rhino.name} already exists; remove it or rename")
    main_path = main_transcript(session)
    total_usd = sum(rhino.cap_usd for rhino in sheet.rhinos) + sheet.owl_cap_usd
    models = [rhino.model for rhino in sheet.rhinos] + [sheet.owl_model]
    print(f"rhino: {check_budget(main_path, models, total_usd)}")
    if sheet.scaffolding != NO_SCAFFOLDING:
        print(scaffolding_notice(sheet.scaffolding), file=sys.stderr)
    results: list[dict[str, object]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=RHINOS_MAX) as pool:
        futures = [pool.submit(run_group, root, main_path, group, user_present, sheet.scaffolding)
                   for group in groups_of(sheet.rhinos)]
        for future in futures:
            results.extend(future.result())
    assert len(results) == len(sheet.rhinos)
    results_path = sheet.path.with_suffix(".results.json")
    recorded: dict[str, object] = {"rhinos": results}
    if sheet.scaffolding != NO_SCAFFOLDING:  # the Owl should know the rhinos didn't start bare
        recorded["scaffolding"] = {"setting_sources": list(sheet.scaffolding.setting_sources),
                                   "rule_files": list(sheet.scaffolding.rule_files)}
    results_path.write_text(json.dumps(recorded, indent=2) + "\n")
    for result in results:
        print(f"rhino: {result['name']} {result['status']} ${result.get('cost_usd')}")
    print(f"rhino: results in {results_path}")
    uncounted = [result["name"] for result in results if result["transcript"] is None]
    if uncounted:
        print(f"rhino: transcripts not found for {uncounted}; their spend is NOT counted by"
              " guard.py", file=sys.stderr)
    crashed = any(result["status"] == "crashed" for result in results)
    return 1 if crashed or uncounted else 0


# -- the Owl


def owl(sheet: Sheet, session: str, user_present: bool) -> int:
    if any(rhino.device for rhino in sheet.rhinos) and not user_present:
        raise Refused("the Owl re-runs hardware rhinos; run it only with --user-present")
    results_path = sheet.path.with_suffix(".results.json")
    if not results_path.is_file():
        raise Refused(f"no rhino results at {results_path}; launch first")
    root = repo_root(sheet.path.parent)
    main_path = main_transcript(session)
    print(f"rhino: owl {check_budget(main_path, [sheet.owl_model], sheet.owl_cap_usd)}")
    prompt = (OWL_BRIEF_PATH.read_text() + f"\n\nLaunch sheet: {sheet.path}\n"
              f"Rhino results: {results_path}\nEvidence folder: {evidence_dir(sheet)}\n"
              f"Ledger rows go to: {sheet.path.parent / 'proposed-ledger.md'}\n")
    result = run_session(claude_command(sheet.owl_model, sheet.owl_cap_usd, None, prompt), root)
    result.setdefault("status", "done")
    result["transcript"] = copy_transcript(main_path, result.get("session"))
    recorded = json.loads(results_path.read_text())
    recorded["owl"] = result
    results_path.write_text(json.dumps(recorded, indent=2) + "\n")
    print(f"rhino: owl {result['status']} ${result.get('cost_usd')}")
    if result["transcript"] is None:
        print("rhino: owl transcript not found; its spend is NOT counted by guard.py", file=sys.stderr)
    return 0 if result["status"] == "done" and result["transcript"] is not None else 1


# -- cleanup, after the user accepts the rows


def git(root: Path, *args: str) -> bool:
    out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                         timeout=GIT_TIMEOUT_S, check=False)
    if out.returncode != 0:
        print(f"rhino: git {' '.join(args)} failed: {out.stderr.strip()}", file=sys.stderr)
    return out.returncode == 0


def evidence_dir(sheet: Sheet) -> Path:
    return sheet.path.parent / "rom-evidence" / sheet.path.stem


def remove(sheet: Sheet) -> int:
    # one-way door: refuse unless the Owl finished and left evidence behind
    results_path = sheet.path.with_suffix(".results.json")
    owl_result = json.loads(results_path.read_text()).get("owl") if results_path.is_file() else None
    if not isinstance(owl_result, dict) or owl_result.get("status") != "done":
        raise Refused("the Owl has not finished (status done) for this sheet; nothing removed")
    if not evidence_dir(sheet).is_dir():
        raise Refused(f"no evidence folder at {evidence_dir(sheet)}; nothing removed")
    root = repo_root(sheet.path.parent)
    ok = True
    for rhino in sheet.rhinos:
        worktree = root / ".claude" / "worktrees" / f"rom-{rhino.name}"
        assert worktree.parent.parent.parent == root, worktree
        if worktree.exists():  # -w leaves it locked, so force twice
            ok = git(root, "worktree", "remove", "--force", "--force", str(worktree)) and ok
        ok = git(root, "branch", "-D", f"worktree-rom-{rhino.name}") and ok
        print(f"rhino: removed {worktree}" if not worktree.exists() else f"rhino: kept {worktree}")
    return 0 if ok else 1


# -- entry

USAGE = ("usage: rhino.py launch|owl --sheet PATH --session ID [--user-present]\n"
         "       rhino.py remove --sheet PATH")


def parse_args(argv: list[str]) -> dict[str, str]:
    if not argv or argv[0] not in ("launch", "owl", "remove"):
        raise Refused(USAGE)
    values: dict[str, str] = {"command": argv[0]}
    rest = list(argv[1:])
    while rest:  # each pass consumes at least one item
        flag = rest.pop(0)
        if flag == "--user-present":
            values["user-present"] = "yes"
        elif flag in ("--sheet", "--session") and rest and flag[2:] not in values:
            values[flag[2:]] = rest.pop(0)
        else:
            raise Refused(f"bad argument {flag}\n{USAGE}")
    needed = ["sheet"] + (["session"] if values["command"] != "remove" else [])
    missing = [name for name in needed if name not in values]
    if missing:
        raise Refused(f"missing --{', --'.join(missing)}\n{USAGE}")
    return values


def main(argv: list[str]) -> int:
    try:
        args = parse_args(argv)
        sheet = load_sheet(Path(args["sheet"]))
        present = "user-present" in args
        if args["command"] == "launch":
            return launch(sheet, args["session"], present)
        if args["command"] == "owl":
            return owl(sheet, args["session"], present)
        return remove(sheet)
    except Refused as err:
        print(f"rhino: refused: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
