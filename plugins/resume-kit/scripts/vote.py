# /// script
# requires-python = ">=3.10"
# dependencies = ["strictyaml==1.6.2", "yamllint==1.38.0", "pypdf==7.1.1"]
# ///
"""Turns several agent runs into one answer: scorer tallies and paired judge votes.

Usage: vote.py tally --keep N --resume <resume.json> [--locks <locks.json>] --out <file>
                     <run.json>...
       vote.py judge-prep <old.json> <new.json> <old.pdf> <new.pdf> --seed N --out <dir>
       vote.py paired <verdict.json>...      each next to the map.json judge-prep wrote
       vote.py --self-test
tally keeps a finding raised by at least N runs, keyed by (rule ID, location ID); its severity
comes from the active rule, never the scorer. Exit 0 ok, 2 bad input or invalid agent output.
"""

from __future__ import annotations

import random
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # sibling module, not a package
from common import Finding, InputError

common.require(["strictyaml", "yamllint", "pypdf"])
import resume_parse as rp  # noqa: E402  (imports below sit after the package guard on purpose)

import rules  # noqa: E402

SCHEMAS = rules.PLUGIN / "data" / "schemas"
RUN_COUNTS = (common.VOTE_RUNS, common.EXPRESS_SCORES_EACH)  # a loop round, an Express variant
NOTES_MAX = common.VOTE_RUNS * 12  # every run's notes fit, so merging never drops one


def load_run(path: Path, schema: str) -> dict[str, Any]:
    """An agent's JSON, checked; a failure names the file so the session re-launches that run."""
    data = common.read_json(path, common.AGENT_JSON_BYTES_MAX)
    errors = common.schema_errors(data, common.read_json(SCHEMAS / f"{schema}.json"))
    if errors:
        raise InputError(f"{schema} output invalid: {path}: {'; '.join(errors[:5])}")
    return data


def location_ids(r: rp.Resume) -> set[str]:
    ids = {"timeline", "skills", "page"} | {s.id for s in r.sections}
    for s in r.sections:
        for e in s.entries:
            ids.add(e.id)
            ids |= {b.id for b in e.bullets}
    return ids


def run_findings(
    data: dict[str, Any], path: Path, known: set[str], fuzzy: set[str]
) -> set[tuple[str, str]]:
    """One vote per (rule, location) per run, however often a run repeats it."""
    keys: set[tuple[str, str]] = set()
    for f in data["findings"]:
        if f["rule_id"] not in fuzzy:
            raise InputError(
                f"scorer output invalid: {path}: {f['rule_id']} is not an active fuzzy rule"
            )
        if f["location_id"] not in known:
            raise InputError(f"scorer output invalid: {path}: unknown location {f['location_id']}")
        keys.add((f["rule_id"], f["location_id"]))
    return keys


def locked_keys(path: Path | None) -> set[tuple[str, str]]:
    if path is None or not path.exists():
        return set()
    data = common.read_json(path)
    try:
        return {(d["rule_id"], d["location_id"]) for d in data["locks"]}
    except (KeyError, TypeError) as e:
        raise InputError(f"{path}: not a locks.json: {e}") from e


def tally(
    runs: list[dict[str, Any]],
    paths: list[Path],
    keep: int,
    r: rp.Resume,
    active: dict[str, rules.Rule],
    locks: set[tuple[str, str]],
) -> tuple[list[Finding], dict[str, Any]]:
    if len(runs) not in RUN_COUNTS:
        raise InputError(f"tally needs {' or '.join(map(str, RUN_COUNTS))} runs, got {len(runs)}")
    if not 1 <= keep <= len(runs):
        raise InputError(f"--keep {keep}: must be 1 to {len(runs)}")
    fuzzy = {k for k, v in active.items() if v.kind == "fuzzy" and v.active() is not None}
    known = location_ids(r)
    votes: Counter[tuple[str, str]] = Counter()
    first: dict[tuple[str, str], dict[str, Any]] = {}
    for data, path in zip(runs, paths):
        for key in run_findings(data, path, known, fuzzy):
            votes[key] += 1
        for f in data["findings"]:
            first.setdefault((f["rule_id"], f["location_id"]), f)
    kept: list[Finding] = []
    for key, n in sorted(votes.items()):
        if n < keep or key in locks:
            continue
        f, rule = first[key], active[key[0]]
        sev = rule.active_severity()
        if f["severity"] != sev:
            print(
                f"vote: {key[0]} at {key[1]}: scorer said {f['severity']}, rule says {sev}",
                file=sys.stderr,
            )
        kept.append(
            Finding(
                key[0],
                key[1],
                sev,
                f"{f['evidence']} ({n} of {len(runs)} runs)",
                f["why"],
                f["action"],
            )
        )
    return kept, merged_fit(runs)


def merged_fit(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """The majority level, with the notes of the first run that gave it: runs paraphrase the
    same warning, so merging their notes would repeat each one up to five times."""
    levels = Counter(d["fit"]["level"] for d in runs)
    level = levels.most_common(1)[0][0]
    notes: list[str] = next(d["fit"]["notes"] for d in runs if d["fit"]["level"] == level)
    assert len(notes) <= NOTES_MAX, len(notes)
    return {"level": level, "notes": notes, "level_votes": dict(levels)}


def plain_text(r: rp.Resume) -> str:
    """What a screener reads: no location IDs, so the judge can't tell old from new by them."""
    out: list[str] = []
    for s in r.sections:
        out.append(f"## {s.title}")
        if s.kind == "skills":
            out += [f"{k.label}: {', '.join(k.items)}" for k in r.skills]
        for e in s.entries:
            out.append(e.heading or e.title)
            out += [f"- {b.text}" for b in e.bullets]
        out.append("")
    return "\n".join(out)


def judge_prep(old: Path, new: Path, old_pdf: Path, new_pdf: Path, seed: int, out: Path) -> str:
    """Old and new go to A and B at random, recorded, so a side preference can't pass as a win."""
    for p in (old_pdf, new_pdf):
        common.check_pdf_size(p)
    sides = ["old", "new"]
    random.Random(seed).shuffle(sides)
    files = {"old": (rp.load_resume(old), old_pdf), "new": (rp.load_resume(new), new_pdf)}
    out.mkdir(parents=True, exist_ok=True)
    for label, side in zip("AB", sides):
        r, pdf = files[side]
        (out / f"{label}.txt").write_text(plain_text(r), encoding="utf-8")
        shutil.copyfile(pdf, out / f"{label}.pdf")
    common.write_json(out / "map.json", {"A": sides[0], "B": sides[1], "seed": seed})
    return sides[0]


def paired(paths: list[Path]) -> str:
    """drop or more after 5 verdicts, keep or reject after 10 (design doc, Contracts)."""
    if len(paths) not in (common.PAIRED_RUNS_FIRST, common.PAIRED_RUNS_MAX):
        raise InputError(
            f"paired needs {common.PAIRED_RUNS_FIRST} or {common.PAIRED_RUNS_MAX} verdicts,"
            f" got {len(paths)}"
        )
    if len({p.resolve().parent for p in paths}) != len(paths):
        raise InputError("each verdict needs its own folder with its own map.json")
    wins_new = 0
    for p in paths:
        side = common.read_json(p.parent / "map.json")
        prefers = load_run(p, "judge")["prefers"]
        if side.get(prefers) not in ("old", "new"):
            raise InputError(f"{p.parent / 'map.json'}: no side for {prefers}")
        wins_new += side[prefers] == "new"
    if len(paths) == common.PAIRED_RUNS_FIRST:
        return "drop" if wins_new <= common.PAIRED_DROP_AT_MOST else "more"
    return "keep" if wins_new >= common.PAIRED_KEEP_AT_LEAST else "reject"


def opt(argv: list[str], name: str) -> str | None:
    if name not in argv:
        return None
    i = argv.index(name)
    if i + 1 >= len(argv):
        raise InputError(f"{name} needs a value")
    value = argv[i + 1]
    del argv[i : i + 2]
    return value


def need(value: str | None, name: str) -> str:
    if value is None:
        raise InputError(f"{name} is required")
    return value


def cmd_tally(argv: list[str]) -> int:
    keep = int(need(opt(argv, "--keep"), "--keep"))
    r = rp.load_resume(Path(need(opt(argv, "--resume"), "--resume")))
    locks_arg = opt(argv, "--locks")
    out = Path(need(opt(argv, "--out"), "--out"))
    paths = [Path(a) for a in argv]
    runs = [load_run(p, "scorer") for p in paths]
    kept, fit = tally(
        runs, paths, keep, r, rules.load(), locked_keys(Path(locks_arg) if locks_arg else None)
    )
    common.findings_out(out, kept, {"fit": fit, "runs": len(runs), "keep": keep})
    print(f"vote: {len(kept)} findings kept by {keep} of {len(runs)} runs")
    return 0


def sample_resume() -> rp.Resume:
    b = [rp.Bullet("acme-b1", "Cut load time", []), rp.Bullet("acme-b2", "Wrote docs", [])]
    e = rp.Entry("acme", "Acme", None, [], [], "Acme, Engineer", b)
    return rp.Resume("t.tex", [rp.Section("experience", "experience", "Experience", [e])])


def scorer_run(*keys: tuple[str, str, str]) -> dict[str, Any]:
    fs = [
        {
            "rule_id": k,
            "location_id": loc,
            "severity": sev,
            "evidence": "e",
            "why": "w",
            "action": "a",
        }
        for k, loc, sev in keys
    ]
    return {"findings": fs, "fit": {"level": "at", "notes": []}}


def test_tally() -> None:
    """3 of 5 keeps a finding, 2 of 5 doesn't; the rule's severity wins; locks and bad runs."""
    r, active = sample_resume(), rules.load()
    duty, gray = ("duty-filler", "acme-b2", "nit"), ("gray-zone-count", "acme-b1", "nit")
    runs = [
        scorer_run(duty, gray),
        scorer_run(duty, gray),
        scorer_run(duty),
        scorer_run(),
        scorer_run(),
    ]
    paths = [Path(f"run{i}.json") for i in range(5)]
    kept, fit = tally(runs, paths, 3, r, active, set())
    assert [(f.rule_id, f.severity) for f in kept] == [("duty-filler", "major")], kept
    assert fit["level"] == "at"
    kept, _ = tally(runs, paths, 2, r, active, {("duty-filler", "acme-b2")})
    assert [f.rule_id for f in kept] == ["gray-zone-count"], kept
    for bad in (("duty-filler", "nowhere", "nit"), ("pages", "acme-b1", "nit")):
        try:
            tally([scorer_run(bad)] * 5, paths, 3, r, active, set())
            raise AssertionError(f"accepted {bad}")
        except InputError as e:
            assert "scorer output invalid" in str(e)
    for n in (4, 5):
        tally(runs[:n], paths[:n], 3, r, active, set())
    try:
        tally(runs[:3], paths[:3], 3, r, active, set())
        raise AssertionError("accepted 3 runs")
    except InputError:
        pass


def test_paired_and_prep() -> None:
    """The drop, more, keep and reject boundaries; the A/B map is random but repeatable."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        common.write_json(d / "r.json", sample_resume())
        (d / "r.pdf").write_bytes(b"%PDF-1.4 stub")
        firsts = {
            judge_prep(d / "r.json", d / "r.json", d / "r.pdf", d / "r.pdf", s, d / f"p{s}")
            for s in range(8)
        }
        assert firsts == {"old", "new"}, firsts
        assert (
            judge_prep(d / "r.json", d / "r.json", d / "r.pdf", d / "r.pdf", 3, d / "again")
            == common.read_json(d / "p3" / "map.json")["A"]
        )
        assert "acme-b1" not in (d / "p0" / "A.txt").read_text(encoding="utf-8")
        for n, wins, want in ((5, 2, "drop"), (5, 3, "more"), (10, 8, "keep"), (10, 7, "reject")):
            paths: list[Path] = []
            for i in range(n):
                run = d / f"v{n}-{wins}-{i}"
                run.mkdir()
                common.write_json(run / "map.json", {"A": "new", "B": "old", "seed": i})
                common.write_json(
                    run / "verdict.json", {"prefers": "A" if i < wins else "B", "why": "w"}
                )
                paths.append(run / "verdict.json")
            assert paired(paths) == want, (n, wins, paired(paths))


def test_schema() -> None:
    """A scorer may not hand out a blocker, add keys, or drop the fit."""
    schema = common.read_json(SCHEMAS / "scorer.json")
    good = scorer_run(("duty-filler", "acme-b1", "major"))
    assert common.schema_errors(good, schema) == []
    bad = scorer_run(("duty-filler", "acme-b1", "blocker"))
    bad["extra"] = 1
    del bad["fit"]
    errs = common.schema_errors(bad, schema)
    assert len(errs) == 3, errs


def self_test() -> int:
    tests = (test_tally, test_paired_and_prep, test_schema)
    for t in tests:
        t()
        print(f"ok {t.__name__}")
    print(f"self-test passed ({len(tests)} tests)")
    return 0


def main(argv: list[str]) -> int:
    if argv == ["--self-test"]:
        return self_test()
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    cmd, rest = argv[0], argv[1:]
    try:
        if cmd == "tally":
            return cmd_tally(rest)
        if cmd == "judge-prep":
            seed = int(need(opt(rest, "--seed"), "--seed"))
            out = Path(need(opt(rest, "--out"), "--out"))
            if len(rest) != 4:
                raise InputError("judge-prep needs <old.json> <new.json> <old.pdf> <new.pdf>")
            judge_prep(*(Path(a) for a in rest), seed=seed, out=out)
            print(f"judge-prep: A.txt, B.txt, A.pdf, B.pdf and map.json in {out}")
            return 0
        if cmd == "paired":
            print(paired([Path(a) for a in rest]))
            return 0
        raise InputError(f"unknown command {cmd}")
    except (InputError, ValueError) as e:
        print(f"vote: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
