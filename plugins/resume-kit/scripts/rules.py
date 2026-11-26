# /// script
# requires-python = ">=3.10"
# dependencies = ["strictyaml==1.6.2", "yamllint==1.38.0", "pypdf==7.1.1"]
# ///
"""The rules ledger: which ruling is active for each rule, and the flexible-mode edits.

Usage: rules.py --print [--settings <file>]     active rules, then every superseded ruling
       rules.py --check                         every sharp rule has a check and every check a rule
       rules.py --fuzzy [--settings <file>]     active fuzzy rules' text, for a scorer brief
       rules.py add|modify|remove <rule-id> --reason <text> [--text <t>] [--severity <s>]
                [--kind sharp|fuzzy] --settings <file>    flexible mode only
       rules.py --self-test
A rule's active ruling is its last by (version, after_audits, absorbed_seq); a friend's local
rulings carry version 1000 so they sort last. Exit 0 ok, 1 --check found a gap, 2 bad input.
"""

from __future__ import annotations

import importlib
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # sibling module, not a package
from common import InputError

PLUGIN = Path(__file__).resolve().parent.parent
SHIPPED = PLUGIN / "data" / "rules.json"
FIXED = ("truth-gate", "private-terms")  # code in evidence.py, outside the ledger
CHECK_MODULES = ("lint", "kw_check", "evidence")
KINDS = ("sharp", "fuzzy", "fit")


@dataclass
class Ruling:
    text: str
    version: int
    after_audits: bool
    absorbed_seq: int
    source: str
    params: dict[str, Any] | None = None
    severity: str | None = None  # set only by a friend's modify
    op: str = "rule"  # rule (shipped), add, modify, remove

    def key(self) -> tuple[int, bool, int]:
        return (self.version, self.after_audits, self.absorbed_seq)


@dataclass
class Rule:
    id: str
    kind: str
    check: str | None
    severity: str
    allows_lock: bool
    rulings: list[Ruling] = field(default_factory=list[Ruling])
    params: dict[str, Any] = field(
        default_factory=dict[str, Any]
    )  # the check's lists and thresholds

    def active(self) -> Ruling | None:
        """None when the rule's last ruling removes it."""
        last = sorted(self.rulings, key=Ruling.key)[-1]
        return None if last.op == "remove" else last

    def active_severity(self) -> str:
        changed = [r.severity for r in sorted(self.rulings, key=Ruling.key) if r.severity]
        return changed[-1] if changed else self.severity


def ruling_from(d: dict[str, Any], where: str) -> Ruling:
    try:
        r = Ruling(
            str(d["text"]),
            int(d["version"]),
            bool(d["after_audits"]),
            int(d["absorbed_seq"]),
            str(d["source"]),
            d.get("params"),
            d.get("severity"),
            str(d.get("op", "rule")),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise InputError(f"{where}: bad ruling {d!r}: {e}") from e
    if r.severity is not None and r.severity not in common.SEVERITIES:
        raise InputError(f"{where}: unknown severity {r.severity!r}")
    return r


def rule_from(d: dict[str, Any], where: str) -> Rule:
    try:
        rule = Rule(
            str(d["id"]), str(d["kind"]), d.get("check"), str(d["severity"]), bool(d["allows_lock"])
        )
    except (KeyError, TypeError) as e:
        raise InputError(f"{where}: bad rule {d!r}: {e}") from e
    if rule.kind not in KINDS or rule.severity not in common.SEVERITIES:
        raise InputError(f"{where}: rule {rule.id}: bad kind or severity")
    rule.rulings = [ruling_from(x, f"{where}:{rule.id}") for x in d.get("rulings", [])]
    params: Any = d.get("params") or {}
    if not isinstance(params, dict):
        raise InputError(f"{where}: rule {rule.id}: params must be an object")
    rule.params = cast(dict[str, Any], params)
    if not rule.rulings:
        raise InputError(f"{where}: rule {rule.id} has no rulings")
    return rule


def load(shipped: Path = SHIPPED, local: Path | None = None) -> dict[str, Rule]:
    """local is read only when the caller has checked the settings say flexible."""
    data = common.read_json(shipped, common.EVIDENCE_BYTES_MAX)
    rules: dict[str, Rule] = {}
    for d in data.get("rules", []):
        rule = rule_from(d, str(shipped))
        if rule.id in rules:
            raise InputError(f"{shipped}: duplicate rule {rule.id}")
        rules[rule.id] = rule
    if local is not None and local.exists():
        apply_local(rules, common.read_json(local, common.EVIDENCE_BYTES_MAX), str(local))
    if len(rules) > common.RULES_MAX:
        raise InputError(f"{len(rules)} rules: RULES_MAX ({common.RULES_MAX}) hit")
    return rules


def apply_local(rules: dict[str, Rule], data: dict[str, Any], where: str) -> None:
    for d in data.get("rulings", []):
        rid, op = str(d.get("rule_id", "")), str(d.get("op", ""))
        if rid in FIXED:
            raise InputError(f"{where}: {rid} is not a rule and can't be changed")
        r = ruling_from(d, where)
        if op == "add":
            if rid in rules:
                raise InputError(f"{where}: add {rid}: it already exists; use modify")
            # a friend's rule has no code behind it, so only the scorer can apply it
            rules[rid] = Rule(rid, "fuzzy", None, r.severity or "nit", False, [r])
        elif op in ("modify", "remove"):
            if rid not in rules:
                raise InputError(f"{where}: {op} {rid}: no such rule")
            rules[rid].rulings.append(r)
        else:
            raise InputError(f"{where}: unknown op {op!r}")


def settings_local(settings: Path | None) -> tuple[Path | None, common.Settings | None]:
    if settings is None:
        return None, None
    s = common.load_settings(settings)
    local = settings.parent / "rules.local.json"
    return (local if s.mode == "flexible" else None), s


def cmd_print(rules: dict[str, Rule]) -> int:
    superseded: list[str] = []
    for rule in rules.values():
        ordered = sorted(rule.rulings, key=Ruling.key)
        act = rule.active()
        state = "REMOVED" if act is None else rule.active_severity()
        text = ordered[-1].text if act is None else act.text
        print(f"{rule.id} [{rule.kind}, {state}] {text} ({ordered[-1].source})")
        superseded += [f"  {rule.id}: {r.text} ({r.source})" for r in ordered[:-1]]
    print(f"{len(rules)} rules; {len(superseded)} superseded rulings:")
    print("\n".join(superseded))
    return 0


def cmd_fuzzy(rules: dict[str, Rule]) -> int:
    for rule in rules.values():
        act = rule.active()
        if rule.kind == "fuzzy" and act is not None:
            print(f"- {rule.id} ({rule.active_severity()}): {act.text}")
    return 0


def check_gaps(rules: dict[str, Rule], checks: dict[str, set[str]]) -> list[str]:
    """checks: module name to its CHECKS keys."""
    gaps: list[str] = []
    for rule in rules.values():
        if rule.kind != "sharp":
            continue
        if rule.check is None or "." not in rule.check:
            gaps.append(f"sharp rule {rule.id} names no check")
            continue
        mod = rule.check.split(".", 1)[0]
        if rule.id not in checks.get(mod, set()):
            gaps.append(f"sharp rule {rule.id}: {mod}.CHECKS has no entry for it")
    for mod, keys in checks.items():
        gaps += [
            f"{mod}.CHECKS[{k!r}] names no sharp rule"
            for k in sorted(keys)
            if k not in rules or rules[k].kind != "sharp"
        ]
    return gaps


def pin_gaps() -> list[str]:
    req = PLUGIN / "skills" / "resume-setup" / "requirements.txt"
    pins = set(req.read_text(encoding="utf-8").split()) if req.exists() else set[str]()
    gaps = [] if req.exists() else [f"missing {req}"]
    for script in sorted((PLUGIN / "scripts").glob("*.py")):
        head = script.read_text(encoding="utf-8").split("# ///", 2)
        deps = re.findall(r'"([^"]+==[^"]+)"', head[1]) if len(head) > 2 else []
        gaps += [f"{script.name} pins {d}, requirements.txt doesn't" for d in deps if d not in pins]
    return gaps


def cmd_check(rules: dict[str, Rule]) -> int:
    checks: dict[str, set[str]] = {}
    for mod in CHECK_MODULES:
        try:
            checks[mod] = set(importlib.import_module(mod).CHECKS)
        except ImportError as e:
            print(f"rules --check: can't import {mod}: {e}", file=sys.stderr)
            return 1
    gaps = check_gaps(rules, checks) + pin_gaps()
    for g in gaps:
        print(g)
    n_checks = sum(len(k) for k in checks.values())
    print(f"{len(rules)} rules, {n_checks} checks, {len(gaps)} gaps")
    return 1 if gaps else 0


def cmd_edit(op: str, rid: str, opts: dict[str, str], settings: Path) -> int:
    s = common.load_settings(settings)
    if s.mode != "flexible":
        raise InputError("rules can change only in flexible mode; set mode to flexible first")
    if rid in FIXED:
        raise InputError(f"{rid} is not a rule and can't be changed")
    if not opts.get("reason"):
        raise InputError("--reason is required")
    local = settings.parent / "rules.local.json"
    empty: dict[str, Any] = {"rulings": []}
    data: dict[str, Any] = (
        common.read_json(local, common.EVIDENCE_BYTES_MAX) if local.exists() else empty
    )
    if not isinstance(data.get("rulings"), list):
        raise InputError(f"{local}: no rulings list")
    prior: list[dict[str, Any]] = data["rulings"]
    seq = len(prior) + 1
    text = opts.get("text") or (
        f"Removed by the friend: {opts['reason']}" if op == "remove" else ""
    )
    if not text:
        raise InputError(f"{op} needs --text")
    data["rulings"].append(
        {
            "rule_id": rid,
            "op": op,
            "reason": opts["reason"],
            "text": text,
            "severity": opts.get("severity"),
            "version": common.LOCAL_RULING_VERSION,
            "after_audits": True,
            "absorbed_seq": seq,
            "source": "friend (local)",
            "params": None,
        }
    )
    rules = load(SHIPPED if not opts.get("shipped") else Path(opts["shipped"]))
    apply_local(rules, data, str(local))  # refuse before writing
    common.write_json(local, data)
    print(f"{op} {rid}: written to {local}")
    return 0


def sample() -> dict[str, Any]:
    def ruling(text: str, v: int, after: bool, seq: int) -> dict[str, Any]:
        return {
            "text": text,
            "version": v,
            "after_audits": after,
            "absorbed_seq": seq,
            "source": "test",
            "params": None,
        }

    return {
        "rules": [
            {
                "id": "spill",
                "kind": "sharp",
                "check": "lint.spill",
                "severity": "major",
                "allows_lock": False,
                "rulings": [
                    ruling("rulebook", 14, True, 19),
                    ruling("audit v14", 14, False, 22),
                    ruling("calibration", 14, True, 18),
                ],
            },
            {
                "id": "duty-filler",
                "kind": "fuzzy",
                "check": None,
                "severity": "major",
                "allows_lock": False,
                "rulings": [ruling("no duties", 5, False, 16)],
            },
        ]
    }


def test_order() -> None:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "rules.json"
        common.write_json(p, sample())
        rules = load(p)
        act = rules["spill"].active()
        assert act is not None and act.text == "rulebook", act  # after_audits, then seq 19 > 18


def test_local() -> None:
    with tempfile.TemporaryDirectory() as d:
        p, s = Path(d) / "rules.json", Path(d) / "r.resumekit.json"
        common.write_json(p, sample())
        common.write_json(
            s, {"page_rule": 1, "mode": "opinionated", "evidence": "e.md", "runner": "uv"}
        )
        try:
            cmd_edit("modify", "spill", {"reason": "x", "text": "y", "shipped": str(p)}, s)
            raise AssertionError("opinionated mode accepted an edit")
        except InputError:
            pass
        common.write_json(
            s, {"page_rule": 1, "mode": "flexible", "evidence": "e.md", "runner": "uv"}
        )
        opts = {"reason": "my field", "text": "friend text", "severity": "nit", "shipped": str(p)}
        assert cmd_edit("modify", "spill", opts, s) == 0
        assert cmd_edit("remove", "duty-filler", {"reason": "n/a", "shipped": str(p)}, s) == 0
        assert cmd_edit("add", "my-rule", {"reason": "r", "text": "t", "shipped": str(p)}, s) == 0
        for rid in FIXED:
            try:
                cmd_edit("remove", rid, {"reason": "x", "shipped": str(p)}, s)
                raise AssertionError(f"{rid} accepted an edit")
            except InputError:
                pass
        local, _ = settings_local(s)
        rules = load(p, local)
        act = rules["spill"].active()
        assert (
            act is not None
            and act.text == "friend text"
            and rules["spill"].active_severity() == "nit"
        )
        assert rules["duty-filler"].active() is None
        assert rules["my-rule"].kind == "fuzzy"
        assert load(p, None)["spill"].active_severity() == "major", "local read without flexible"


def test_gaps() -> None:
    rules = {r["id"]: rule_from(r, "t") for r in sample()["rules"]}
    assert check_gaps(rules, {"lint": {"spill"}}) == []
    assert check_gaps(rules, {"lint": set()}) == [
        "sharp rule spill: lint.CHECKS has no entry for it"
    ]
    assert check_gaps(rules, {"lint": {"spill", "ghost"}}) == [
        "lint.CHECKS['ghost'] names no sharp rule"
    ]


def self_test() -> int:
    for t in (test_order, test_local, test_gaps):
        t()
        print(f"ok {t.__name__}")
    print("self-test passed (3 tests)")
    return 0


def parse_opts(argv: list[str]) -> dict[str, str]:
    opts: dict[str, str] = {}
    it = iter(argv)
    for a in it:
        if not a.startswith("--"):
            raise InputError(f"unexpected argument {a!r}")
        val = next(it, None)
        if val is None:
            raise InputError(f"{a} needs a value")
        opts[a[2:]] = val
    return opts


def main(argv: list[str]) -> int:
    try:
        if argv == ["--self-test"]:
            return self_test()
        if argv and argv[0] in ("add", "modify", "remove") and len(argv) >= 2:
            opts = parse_opts(argv[2:])
            if "settings" not in opts:
                raise InputError("--settings is required")
            return cmd_edit(argv[0], argv[1], opts, Path(opts["settings"]))
        if argv and argv[0] in ("--print", "--check", "--fuzzy"):
            opts = parse_opts(argv[1:])
            local, _ = settings_local(Path(opts["settings"]) if "settings" in opts else None)
            rules = load(Path(opts.get("rules", str(SHIPPED))), local)
            cmd = {"--print": cmd_print, "--check": cmd_check, "--fuzzy": cmd_fuzzy}[argv[0]]
            return cmd(rules)
        print(__doc__, file=sys.stderr)
        return 2
    except InputError as e:
        print(f"rules: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
