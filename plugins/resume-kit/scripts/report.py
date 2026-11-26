# /// script
# requires-python = ">=3.10"
# dependencies = ["strictyaml==1.6.2", "yamllint==1.38.0", "pypdf==7.1.1"]
# ///
"""Merges every check's findings into one report: the verdict, the counts, then each finding.

Usage: report.py <findings.json>... --resume <resume.json> --out <dir>
                 [--state <state.json>] [--settings <file>] [--posting <name>]
       report.py --self-test
Writes report.md and report.json. Without --state it is a one-off check (/resume check).
Exit 0 written, 2 bad input.
"""

from __future__ import annotations

import datetime as dt
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # sibling module, not a package
from common import Finding, InputError

common.require(["strictyaml", "yamllint", "pypdf"])
import resume_parse as rp  # noqa: E402  (imports below sit after the package guard on purpose)

import rules  # noqa: E402

DEDUCT_POINTS = {"blocker": 20, "major": 5, "nit": 1, "none": 0}  # design doc, Fuzzy scoring
ORDER = {"blocker": 0, "major": 1, "nit": 2, "none": 3}
INPUTS_MAX = 12  # lint, keywords, evidence, a tally, with room
TRUTH_LINE = (
    "truth gate: numbers, dates, terms and links match the evidence; wording judged by vote;"
    " private terms checked against the listed terms only"
)
CHECK_STATE: dict[str, Any] = {
    "phase": "check",
    "fix_rounds": 0,
    "polish_rounds": 0,
    "express_passes": 0,
    "idle_rounds": 0,
    "agent_runs": 0,
    "kept": [],
    "waiting": None,
}


def is_ready(findings: list[Finding]) -> bool:
    """0 fixable blockers and 0 majors; a blocker no edit can fix (tenure) doesn't count."""
    return not any(
        (f.severity == "blocker" and f.fixable) or f.severity == "major" for f in findings
    )


def verdict(findings: list[Finding], state: dict[str, Any], friend_rule_changes: int) -> str:
    """The Goal 5 endings; anything else is a round still in progress."""
    ready = is_ready(findings)
    base = "ready" if ready else "not ready"
    if state["agent_runs"] >= common.AGENT_RUNS_MAX:
        text = f"{base}, run budget spent"
    elif not ready and state["fix_rounds"] >= common.FIX_ROUNDS_MAX:
        text = "not ready, round cap reached"
    elif ready and state["idle_rounds"] >= common.POLISH_IDLE_ROUNDS:
        text = "polished"
    elif ready and state["polish_rounds"] >= common.POLISH_ROUNDS_MAX:
        text = "ready, polish cap reached"
    else:
        text = base
    if friend_rule_changes:
        text += f", {friend_rule_changes} friend rule changes"
    return text


def readiness(findings: list[Finding], fuzzy: set[str]) -> int:
    """For reference only; the verdict comes from the counts. Fuzzy deductions are capped."""
    sharp = sum(DEDUCT_POINTS[f.severity] for f in findings if f.rule_id not in fuzzy)
    soft = sum(DEDUCT_POINTS[f.severity] for f in findings if f.rule_id in fuzzy)
    return max(0, 100 - sharp - min(soft, common.FUZZY_CAP_POINTS))


def load_inputs(paths: list[Path]) -> tuple[list[Finding], dict[str, Any]]:
    if not 1 <= len(paths) <= INPUTS_MAX:
        raise InputError(f"{len(paths)} findings files: need 1 to {INPUTS_MAX}")
    findings: list[Finding] = []
    extra: dict[str, Any] = {"fit_warnings": [], "paid_years": None, "fit": None}
    for p in paths:
        d = common.read_json(p, common.EVIDENCE_BYTES_MAX)
        try:
            findings += [Finding(**f) for f in d["findings"]]
        except (KeyError, TypeError, AssertionError) as e:
            raise InputError(f"{p}: not a findings file: {e}") from e
        extra["fit_warnings"] += d.get("fit_warnings", [])
        extra["paid_years"] = d.get("paid_years", extra["paid_years"])
        extra["fit"] = d.get("fit", extra["fit"])
    findings.sort(key=lambda f: (ORDER[f.severity], f.rule_id, f.location_id))
    return findings, extra


def names(r: rp.Resume) -> dict[str, str]:
    """Location ID to what a friend calls it: the entry, or the entry and its bullet number."""
    out = {"timeline": "the timeline", "skills": "Technical Skills", "page": "the page"}
    for s in r.sections:
        out[s.id] = s.title
        for e in s.entries:
            out[e.id] = e.title
            for k, b in enumerate(e.bullets, 1):
                out[b.id] = f"{e.title}, bullet {k}"
    return out


def clean(text: str) -> str:
    """The reviewer's report rules: no em or en dashes."""
    return text.replace(" \u2014 ", ", ").replace("\u2014", ", ").replace("\u2013", "-")


def next_command(findings: list[Finding], posting: str, ending: bool) -> str:
    if any(f.rule_id == "truth-gate" and "no fact backs" in f.evidence for f in findings):
        return "/resume-setup  (add the facts the truth gate asked for, or cut those claims)"
    if ending:
        return "none: the loop has ended; read the findings left above"
    return f"/resume {posting}".rstrip()


def source_of(rule_id: str, active: dict[str, rules.Rule]) -> str:
    rule = active.get(rule_id)
    act = rule.active() if rule else None
    return act.source if act else "fixed check"


def finding_block(
    i: int, f: Finding, where: dict[str, str], active: dict[str, rules.Rule]
) -> list[str]:
    unfixable = "" if f.fixable else " (no edit fixes this; it doesn't block ready)"
    return [
        f"### {i}. {f.severity}: {f.rule_id} at {where.get(f.location_id, f.location_id)}"
        f" (`{f.location_id}`){unfixable}",
        f"- Rule source: {source_of(f.rule_id, active)}",
        f"- Evidence: {f.evidence}",
        f"- Why it matters in a 15-second screen: {f.why}",
        f"- Action: {f.action}",
        "",
    ]


def fit_lines(extra: dict[str, Any]) -> list[str]:
    """Fit describes and never blocks, so it gets its own section, one warning per line."""
    fit, years = extra["fit"], extra["paid_years"]
    head = (
        f"level {fit['level']} the posting" if fit else "level not judged (the scorer has not run)"
    )
    if years is not None:
        head += f", {years:g} paid years"
    out = ["## Fit (describes, never blocks)", "", head, ""]
    out += [f"- {w['keyword']}: {w['reason']}" for w in extra["fit_warnings"]]
    notes: list[str] = list(fit["notes"]) if fit else []
    out += [f"- {n}" for n in notes]
    return out + [""]


def render(findings: list[Finding], extra: dict[str, Any], ctx: dict[str, Any]) -> str:
    counts = {s: sum(f.severity == s for f in findings) for s in ("blocker", "major", "nit")}
    state = ctx["state"]
    lines = [
        f"# Resume report: {ctx['posting'] or 'no posting named'}",
        "",
        f"{ctx['date']} | mode {ctx['mode']} | phase {state['phase']}, fix round"
        f" {state['fix_rounds']}, polish round {state['polish_rounds']}",
        "",
        f"**Verdict: {ctx['verdict']}**",
        "",
        f"{counts['blocker']} blockers | {counts['major']} majors | {counts['nit']} nits",
        f"Readiness {ctx['readiness']}/100 (for reference; the verdict comes from the counts)",
        TRUTH_LINE,
    ]
    if extra["fit"] is None:
        lines.append("Scorer: not run, so only the sharp checks are counted here")
    if state["waiting"]:
        lines.append(f"Paused, waiting on the friend: {state['waiting']}")
    lines += [""] + fit_lines(extra) + ["## Findings, worst first", ""]
    for i, f in enumerate(findings, 1):
        lines += finding_block(i, f, ctx["names"], ctx["rules"])
    if not findings:
        lines += ["None.", ""]
    lines += ["## Next", "", f"`{ctx['next']}`", ""]
    text = clean("\n".join(lines))
    assert "\u2014" not in text
    return text


def build(paths: list[Path], opts: dict[str, str | None]) -> tuple[str, dict[str, Any]]:
    findings, extra = load_inputs(paths)
    state = dict(CHECK_STATE)
    if opts["state"]:
        state.update(common.read_json(Path(opts["state"])))
    mode, changes = "opinionated", 0
    local = None
    if opts["settings"]:
        settings_path = Path(opts["settings"])
        mode = common.load_settings(settings_path).mode
        if mode == "flexible":
            local = settings_path.parent / "rules.local.json"
            changes = len(common.read_json(local).get("rulings", [])) if local.exists() else 0
    active = rules.load(local=local)
    fuzzy = {k for k, v in active.items() if v.kind == "fuzzy"}
    text_verdict = verdict(findings, state, changes)
    ending = verdict(findings, state, 0) not in ("ready", "not ready")
    posting = opts["posting"] or ""
    ctx: dict[str, Any] = {
        "posting": posting,
        "date": dt.date.today().strftime("%-d %B %Y"),
        "mode": mode,
        "state": state,
        "verdict": text_verdict,
        "readiness": readiness(findings, fuzzy),
        "names": names(rp.load_resume(Path(opts["resume"] or ""))),
        "rules": active,
        "next": next_command(findings, posting, ending),
    }
    summary = {
        "verdict": text_verdict,
        "ready": is_ready(findings),
        "readiness": ctx["readiness"],
        "findings": [asdict(f) for f in findings],
        "fit": extra,
        "next": ctx["next"],
    }
    return render(findings, extra, ctx), summary


def test_verdict() -> None:
    """Every Goal 5 ending, and the unfixable blocker that doesn't stop ready."""
    s = dict(CHECK_STATE)
    major = [Finding("spill", "acme-b1", "major", "e", "w", "a")]
    tenure = [Finding("fit-tenure", "timeline", "blocker", "e", "w", "a", fixable=False)]
    assert verdict([], s, 0) == "ready" and verdict(tenure, s, 0) == "ready"
    assert verdict(major, s, 0) == "not ready"
    assert verdict(major, {**s, "fix_rounds": 5}, 0) == "not ready, round cap reached"
    assert verdict(major, {**s, "fix_rounds": 4}, 0) == "not ready"
    assert verdict([], {**s, "idle_rounds": 2}, 0) == "polished"
    assert verdict([], {**s, "idle_rounds": 1}, 0) == "ready"
    assert verdict([], {**s, "polish_rounds": 5}, 0) == "ready, polish cap reached"
    assert verdict(major, {**s, "agent_runs": 230}, 0) == "not ready, run budget spent"
    assert verdict([], {**s, "agent_runs": 229}, 2) == "ready, 2 friend rule changes"


def test_readiness() -> None:
    """Blocker 20, major 5, nit 1; the fuzzy part stops at 15 points."""
    sharp = [Finding("spill", "x", sev, "e", "w", "a") for sev in ("blocker", "major", "nit")]
    assert readiness(sharp, set()) == 74
    soft = [Finding("duty-filler", f"b{i}", "major", "e", "w", "a") for i in range(4)]
    assert readiness(soft, {"duty-filler"}) == 85
    assert readiness(soft * 10, set()) == 0


def test_render() -> None:
    """Counts, the truth line, no dashes, and setup as the next step when a fact is missing."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        e = rp.Entry("acme", "Acme", None, [], [], "", [rp.Bullet("acme-b1", "t", [])])
        common.write_json(
            d / "r.json", rp.Resume("t", [rp.Section("experience", "experience", "E", [e])])
        )
        gate = Finding(
            "truth-gate",
            "acme",
            "blocker",
            "1 unbacked claims: number 6h: no fact backs it",
            "w",
            "a",
        )
        nit = Finding("weak-opener", "acme-b1", "nit", "Took \u2014 opener", "w", "a")
        common.findings_out(d / "f.json", [nit, gate], {"paid_years": 2.0})
        opts: dict[str, str | None] = {
            "state": None,
            "settings": None,
            "posting": "acme-swe",
            "resume": str(d / "r.json"),
        }
        text, summary = build([d / "f.json"], opts)
        assert "1 blockers | 0 majors | 1 nits" in text and TRUTH_LINE in text
        assert "\u2014" not in text and "Acme, bullet 1" in text
        assert summary["verdict"] == "not ready" and summary["next"].startswith("/resume-setup")
        assert [f["rule_id"] for f in summary["findings"]] == ["truth-gate", "weak-opener"]


def self_test() -> int:
    tests = (test_verdict, test_readiness, test_render)
    for t in tests:
        t()
        print(f"ok {t.__name__}")
    print(f"self-test passed ({len(tests)} tests)")
    return 0


def main(argv: list[str]) -> int:
    if argv == ["--self-test"]:
        return self_test()
    opts: dict[str, str | None] = {
        k: None for k in ("state", "settings", "posting", "resume", "out")
    }
    paths: list[Path] = []
    i = 0
    while i < len(argv):  # bounded: each pass consumes one or two arguments
        a = argv[i]
        if a.startswith("--") and a[2:] in opts and i + 1 < len(argv):
            opts[a[2:]] = argv[i + 1]
            i += 2
        else:
            paths.append(Path(a))
            i += 1
    try:
        if not opts["resume"] or not opts["out"]:
            raise InputError("--resume and --out are required")
        text, summary = build(paths, opts)
    except InputError as e:
        print(f"report: {e}", file=sys.stderr)
        return 2
    out = Path(opts["out"])
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text(text, encoding="utf-8")
    common.write_json(out / "report.json", summary)
    print(f"report: {summary['verdict']}; {out / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
