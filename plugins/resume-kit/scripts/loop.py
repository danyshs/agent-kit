# /// script
# requires-python = ">=3.10"
# ///
"""The loop's state machine: which round comes next, and when the loop stops.

Usage: loop.py --state <runs/state.json> start [--express]
       loop.py --state <file> record-runs <n>          after every agent launch, retries too
       loop.py --state <file> fix-done <report.json>
       loop.py --state <file> polish-done --kept yes|no [--report <report.json>]
       loop.py --state <file> express-done --winner yes|no [--report <report.json>]
       loop.py --state <file> wait <reason>
       loop.py --state <file> resume
       loop.py --state <file> next                    one JSON line: {"action", "verdict"}
       loop.py --self-test
Every stop is decided here; the skill only does what `next` says. A kept change needs the
report of the round that kept it, so a change that brings back a major returns to the fix
phase. Exit 0 ok, 2 bad input, a cap hit, or a command the state doesn't allow.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # sibling module, not a package
from common import InputError

RECORD_RUNS_MAX = 50  # one launch batch: an Express pass is 8 variants + 32 scorers + 10 judges
REASON_CHARS_MAX = 200
STATE_BYTES_MAX = 4096
COUNTERS = {  # field: its ceiling, so a corrupt or hand-edited state fails closed
    "fix_rounds": common.FIX_ROUNDS_MAX,
    "polish_rounds": common.POLISH_ROUNDS_MAX,
    "express_passes": common.EXPRESS_PASSES_MAX,
    "idle_rounds": max(common.POLISH_ROUNDS_MAX, common.EXPRESS_PASSES_MAX),
    "agent_runs": common.AGENT_RUNS_MAX + RECORD_RUNS_MAX,
    "kept": common.POLISH_ROUNDS_MAX * common.FIX_ROUNDS_MAX + common.EXPRESS_PASSES_MAX,
}
PHASES = ("fix", "polish", "express")
ACTIONS = {"fix": "fix-round", "polish": "polish-round", "express": "express-pass"}
ROUND_CAP = "not ready, round cap reached"
POLISHED = "polished"
POLISH_CAP = "ready, polish cap reached"


def new_state(express: bool) -> dict[str, Any]:
    s: dict[str, Any] = {k: 0 for k in COUNTERS}
    s.update(phase="fix", mode="express" if express else "loop", waiting=None, ending=None)
    return s


def check_state(s: Any, where: str) -> dict[str, Any]:
    """The state as written by this script, or bad input: never guess at a broken file."""
    if not isinstance(s, dict):
        raise InputError(f"{where}: not an object")
    d = cast(dict[str, Any], s)
    want = {*COUNTERS, "phase", "mode", "waiting", "ending"}
    if set(d) != want:
        raise InputError(f"{where}: fields {sorted(set(d) ^ want)} wrong")
    for k, ceiling in COUNTERS.items():
        v = d[k]
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= ceiling:
            raise InputError(f"{where}: {k} = {v!r}, not 0..{ceiling}")
    if d["phase"] not in PHASES or d["mode"] not in ("loop", "express"):
        raise InputError(f"{where}: phase {d['phase']!r} or mode {d['mode']!r} unknown")
    for k in ("waiting", "ending"):
        if d[k] is not None and (not isinstance(d[k], str) or not d[k]):
            raise InputError(f"{where}: {k} must be null or text")
    return d


def open_round(s: dict[str, Any], phase: str) -> None:
    """A round's result may only land in the phase that ran it, on a live, unpaused loop."""
    if s["ending"] is not None:
        raise InputError(f"the loop has ended: {s['ending']}")
    if s["waiting"] is not None:
        raise InputError(f"the loop is waiting ({s['waiting']}); run resume first")
    if s["phase"] != phase:
        raise InputError(f"the loop is in the {s['phase']} phase, not {phase}")


def fix_done(s: dict[str, Any], ready: bool) -> None:
    open_round(s, "fix")
    s["fix_rounds"] += 1
    if ready:
        s["phase"] = "express" if s["mode"] == "express" else "polish"
        s["idle_rounds"] = 0
    elif s["fix_rounds"] >= common.FIX_ROUNDS_MAX:
        s["ending"] = ROUND_CAP
    assert s["fix_rounds"] <= common.FIX_ROUNDS_MAX, "fix ran past its cap"


def improve_done(s: dict[str, Any], phase: str, kept: bool, ready: bool | None) -> None:
    """One polish round or Express pass. Kept: the change stays and ready says whether the
    page still is. Two idle rounds in a row is polished; the round cap is the other stop."""
    open_round(s, phase)
    if kept and ready is None:
        raise InputError(
            "a kept change needs --report: the round's checks decide if it stays ready"
        )
    counter, cap = (
        ("polish_rounds", common.POLISH_ROUNDS_MAX)
        if phase == "polish"
        else ("express_passes", common.EXPRESS_PASSES_MAX)
    )
    s[counter] += 1
    s["idle_rounds"] = 0 if kept else s["idle_rounds"] + 1
    s["kept"] += kept
    if kept and not ready:
        s["phase"] = "fix"  # the fix count carries on; it is not reset by a detour
        if s["fix_rounds"] >= common.FIX_ROUNDS_MAX:
            s["ending"] = ROUND_CAP
    elif s["idle_rounds"] >= common.POLISH_IDLE_ROUNDS:
        s["ending"] = POLISHED
    elif s[counter] >= cap:
        s["ending"] = POLISH_CAP
    assert s[counter] <= cap, f"{counter} ran past its cap"


def record_runs(s: dict[str, Any], n: int) -> None:
    if not 1 <= n <= RECORD_RUNS_MAX:
        raise InputError(f"record-runs takes 1..{RECORD_RUNS_MAX}, got {n}")
    if s["agent_runs"] >= common.AGENT_RUNS_MAX:
        raise InputError(f"AGENT_RUNS_MAX hit ({s['agent_runs']} runs): launch nothing more")
    s["agent_runs"] += n


def next_step(s: dict[str, Any]) -> dict[str, Any]:
    if s["ending"] is not None:
        return {"action": "done", "verdict": s["ending"]}
    if s["agent_runs"] >= common.AGENT_RUNS_MAX:
        base = "not ready" if s["phase"] == "fix" else "ready"
        return {"action": "done", "verdict": f"{base}, run budget spent"}
    if s["waiting"] is not None:
        return {"action": "wait", "verdict": None, "reason": s["waiting"]}
    return {"action": ACTIONS[s["phase"]], "verdict": None}


def report_ready(path: Path) -> bool:
    ready = common.read_json(path).get("ready")
    if not isinstance(ready, bool):
        raise InputError(f"{path}: no ready field; is it a report.py report.json?")
    return ready


def load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise InputError(f"{path}: no loop state; run start first")
    return check_state(common.read_json(path, STATE_BYTES_MAX), str(path))


def save(path: Path, s: dict[str, Any]) -> None:
    """Checked, then written whole through a temp file, so a crash never leaves half a state."""
    check_state(s, "new state")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def yes_no(argv: list[str], flag: str) -> bool:
    if flag not in argv or argv.index(flag) + 1 >= len(argv):
        raise InputError(f"{flag} yes|no is required")
    value = argv[argv.index(flag) + 1]
    if value not in ("yes", "no"):
        raise InputError(f"{flag} takes yes or no, got {value!r}")
    return value == "yes"


def optional_report(argv: list[str]) -> bool | None:
    if "--report" not in argv:
        return None
    i = argv.index("--report")
    if i + 1 >= len(argv):
        raise InputError("--report needs a path")
    return report_ready(Path(argv[i + 1]))


def apply(state_path: Path, cmd: str, rest: list[str]) -> dict[str, Any]:
    """Runs one command against the saved state and saves the result; returns what next says."""
    if cmd == "start":
        if state_path.is_file() and load(state_path)["ending"] is None:
            raise InputError(f"{state_path}: a loop is in progress; finish it or remove the file")
        s = new_state("--express" in rest)
    else:
        s = load(state_path)
    if cmd == "record-runs":
        if len(rest) != 1 or not rest[0].isdigit():
            raise InputError("record-runs needs a whole number")
        record_runs(s, int(rest[0]))
    elif cmd == "fix-done":
        if len(rest) != 1:
            raise InputError("fix-done needs <report.json>")
        fix_done(s, report_ready(Path(rest[0])))
    elif cmd in ("polish-done", "express-done"):
        flag = "--kept" if cmd == "polish-done" else "--winner"
        improve_done(s, cmd.split("-")[0], yes_no(rest, flag), optional_report(rest))
    elif cmd == "wait":
        reason = " ".join(rest).strip()
        if not reason or len(reason) > REASON_CHARS_MAX:
            raise InputError(f"wait needs a reason of 1..{REASON_CHARS_MAX} characters")
        s["waiting"] = reason
    elif cmd == "resume":
        if s["waiting"] is None:
            raise InputError("the loop is not waiting")
        s["waiting"] = None
    elif cmd not in ("start", "next"):
        raise InputError(f"unknown command {cmd}")
    if cmd != "next":
        save(state_path, s)
    return next_step(s)


# -- self-test: every ending, the detour back to fix, the budget, wait and resume


def run(s: dict[str, Any], steps: Sequence[tuple[str, bool, bool | None]]) -> dict[str, Any]:
    """steps: (fix|polish|express, ready-or-kept, ready-after-kept)."""
    for phase, flag, ready in steps:
        if phase == "fix":
            fix_done(s, flag)
        else:
            improve_done(s, phase, flag, ready)
    return next_step(s)


def refused(fn: Any, *args: Any) -> bool:
    try:
        fn(*args)
    except InputError:
        return True
    return False


def test_fix_endings() -> None:
    """Five not-ready rounds end the loop; a sixth round is refused, never run."""
    s = new_state(False)
    assert next_step(s) == {"action": "fix-round", "verdict": None}
    out = run(s, [("fix", False, None)] * common.FIX_ROUNDS_MAX)
    assert out == {"action": "done", "verdict": ROUND_CAP}, out
    assert refused(fix_done, s, True) and s["fix_rounds"] == common.FIX_ROUNDS_MAX


def test_polish_endings() -> None:
    """Two idle rounds in a row: polished. Kept, idle, kept... never two idle: the cap."""
    s = new_state(False)
    assert run(s, [("fix", True, None)]) == {"action": "polish-round", "verdict": None}
    assert run(s, [("polish", False, None), ("polish", False, None)])["verdict"] == POLISHED
    s = new_state(False)
    steps: list[tuple[str, bool, bool | None]] = [("fix", True, None)]
    steps += [("polish", i % 2 == 0, True if i % 2 == 0 else None) for i in range(5)]
    out = run(s, steps)
    assert out["verdict"] == POLISH_CAP and s["polish_rounds"] == 5 and s["kept"] == 3, (out, s)
    assert refused(improve_done, s, "polish", False, None)
    s = new_state(False)
    run(s, [("fix", True, None)])
    assert refused(improve_done, s, "polish", True, None), "kept without a report passed"
    assert s["polish_rounds"] == 0, "a refused round still counted"


def test_detour() -> None:
    """A kept change that brings back a major returns to fix; the fix count carries on."""
    s = new_state(False)
    run(s, [("fix", False, None), ("fix", False, None), ("fix", True, None)])
    assert run(s, [("polish", True, False)]) == {"action": "fix-round", "verdict": None}
    assert s["fix_rounds"] == 3 and s["phase"] == "fix"
    out = run(s, [("fix", False, None), ("fix", False, None)])
    assert out["verdict"] == ROUND_CAP and s["fix_rounds"] == common.FIX_ROUNDS_MAX, s
    s = new_state(False)
    run(s, [("fix", False, None)] * 4 + [("fix", True, None)])
    assert run(s, [("polish", True, False)])["verdict"] == ROUND_CAP, "detour at the cap"


def test_express() -> None:
    """Express starts from ready; two passes with no winner is polished; three passes cap it."""
    s = new_state(True)
    assert run(s, [("fix", True, None)]) == {"action": "express-pass", "verdict": None}
    assert run(s, [("express", False, None)] * 2)["verdict"] == POLISHED
    s = new_state(True)
    steps: list[tuple[str, bool, bool | None]] = [("fix", True, None), ("express", True, True)]
    steps += [("express", False, None), ("express", True, True)]
    assert run(s, steps)["verdict"] == POLISH_CAP and s["express_passes"] == 3
    assert refused(improve_done, new_state(True), "polish", False, None), "wrong phase passed"


def test_budget_and_wait() -> None:
    """The budget ends the loop in either phase and refuses more launches; wait pauses it."""
    s = new_state(False)
    while s["agent_runs"] + RECORD_RUNS_MAX < common.AGENT_RUNS_MAX:  # bounded: 4 passes
        record_runs(s, RECORD_RUNS_MAX)
    record_runs(s, common.AGENT_RUNS_MAX - 1 - s["agent_runs"])
    assert next_step(s)["action"] == "fix-round", "one run under the budget ended the loop"
    record_runs(s, 1)
    assert s["agent_runs"] == common.AGENT_RUNS_MAX
    assert next_step(s)["verdict"] == "not ready, run budget spent"
    assert refused(record_runs, s, 1) and refused(record_runs, new_state(False), 0)
    s["phase"] = "polish"
    assert next_step(s)["verdict"] == "ready, run budget spent"
    s = new_state(False)
    s["waiting"] = "build failed: missing font"
    assert next_step(s)["action"] == "wait" and refused(fix_done, s, True)
    s["waiting"] = None
    assert run(s, [("fix", True, None)])["action"] == "polish-round"


def test_cli() -> None:
    """The file round-trip, a report's ready field, and a broken state refused."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        st = d / "runs" / "state.json"
        common.write_json(d / "ok.json", {"ready": True, "verdict": "ready"})
        common.write_json(d / "bad.json", {"verdict": "ready"})
        assert apply(st, "start", [])["action"] == "fix-round"
        assert refused(apply, st, "start", []), "start over a live loop passed"
        assert refused(apply, st, "fix-done", [str(d / "bad.json")])
        assert apply(st, "fix-done", [str(d / "ok.json")])["action"] == "polish-round"
        assert apply(st, "wait", ["need", "a", "photo"])["action"] == "wait"
        assert apply(st, "resume", [])["action"] == "polish-round"
        assert apply(st, "record-runs", ["5"]) and load(st)["agent_runs"] == 5
        raw = json.loads(st.read_text(encoding="utf-8"))
        for broken in ({**raw, "fix_rounds": 9}, {**raw, "extra": 1}, {**raw, "phase": "x"}):
            st.write_text(json.dumps(broken), encoding="utf-8")
            assert refused(apply, st, "next", []), broken


TESTS = (
    test_fix_endings,
    test_polish_endings,
    test_detour,
    test_express,
    test_budget_and_wait,
    test_cli,
)


def self_test() -> int:
    for t in TESTS:
        t()
        print(f"ok {t.__name__}")
    print(f"self-test passed ({len(TESTS)} tests)")
    return 0


def main(argv: list[str]) -> int:
    if argv == ["--self-test"]:
        return self_test()
    if len(argv) < 3 or argv[0] != "--state":
        print(__doc__, file=sys.stderr)
        return 2
    try:
        print(json.dumps(apply(Path(argv[1]), argv[2], argv[3:])))
        return 0
    except InputError as e:
        print(f"loop: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
