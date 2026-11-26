# /// script
# requires-python = ">=3.10"
# dependencies = ["strictyaml==1.6.2", "yamllint==1.38.0", "pypdf==7.1.1"]
# ///
"""The evidence file: fact blocks inside Markdown, their lint, and the checks against a resume.

Usage: evidence.py lint <file> [--hook]     yamllint and the schema, as <file>:<line>:<col>: <msg>
       evidence.py validate <file>          lint plus the lost-fact guard
       evidence.py todo <file>              draft facts and their open questions
       evidence.py check <resume.json> <evidence> <pdf> --out <findings.json>
       evidence.py delete <fact-id> <file>  remove one fact on purpose
       evidence.py public <file> --out <f>  confirmed, non-confidential facts only, for agent briefs
       evidence.py --self-test
A fact is a fenced block that opens with three backticks and "fact" and holds StrictYAML.
Exit 0 ok, 1 findings or problems, 2 bad input, 3 missing package.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # sibling module, not a package
from common import Finding, InputError, Problem

common.require(["strictyaml", "yamllint", "pypdf"])
import resume_parse as rp  # noqa: E402  (imports below sit after the package guard on purpose)
import strictyaml as sy  # noqa: E402
from yamllint import linter as yl_linter  # noqa: E402
from yamllint.config import YamlLintConfig  # noqa: E402

PLUGIN = Path(__file__).resolve().parent.parent
YAMLLINT_CONF = PLUGIN / "data" / "yamllint.yml"
FENCE_OPEN = "```fact"
FENCE_CLOSE = "```"
FIELD_LINE = re.compile(r"^(id|entry|is_measure|paid|shipped|confidential|status|source):")
YES_NO = sy.Enum(["yes", "no"])
SCHEMA = sy.Map(
    {
        "id": sy.Regex(r"[a-z0-9]+(-[a-z0-9]+)*"),
        "entry": sy.Str(),
        "text": sy.Str(),
        "is_measure": YES_NO,
        "paid": YES_NO,
        "shipped": YES_NO,
        sy.Optional("dates"): sy.Regex(r"\d{4}-\d{2} to (\d{4}-\d{2}|present)"),
        "source": sy.Str(),
        "confidential": YES_NO,
        "status": sy.Enum(["confirmed", "draft"]),
        sy.Optional("languages"): sy.Seq(sy.Str()),
        sy.Optional("terms"): sy.Seq(sy.Str()),
        sy.Optional("links"): sy.Seq(sy.Str()),
        sy.Optional("questions"): sy.Seq(sy.Str()),
    }
)
WORD_NUMBERS = {
    w: i + 1
    for i, w in enumerate(
        ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")
    )
}
SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "billion": 1e9}
APPROX_TOLERANCE = 0.10  # a number marked about or ~ matches a fact within 10%


@dataclass
class Fact:
    id: str
    entry: str
    text: str
    is_measure: bool
    paid: bool
    shipped: bool
    dates: str | None
    source: str
    confidential: bool
    status: str
    line: int  # the fence's line in the Markdown file
    languages: list[str] = field(default_factory=list[str])
    terms: list[str] = field(default_factory=list[str])
    links: list[str] = field(default_factory=list[str])
    questions: list[str] = field(default_factory=list[str])


@dataclass
class Number:
    value: float
    unit: str  # "", "%", "x" or "h"
    ordinal: bool
    approx: bool


def read_evidence(path: Path) -> str:
    try:
        size = path.stat().st_size
        if size > common.EVIDENCE_BYTES_MAX:
            raise InputError(
                f"{path}: {size} bytes, EVIDENCE_BYTES_MAX ({common.EVIDENCE_BYTES_MAX}) hit"
            )
        return path.read_text(encoding="utf-8")
    except OSError as e:
        raise InputError(f"{path}: {e}") from e


def blocks(md: str, name: str) -> tuple[list[tuple[int, str]], list[Problem]]:
    """(first YAML line number, YAML text) per fact block, plus fence and stray-field problems."""
    out: list[tuple[int, str]] = []
    problems: list[Problem] = []
    start: int | None = None
    body: list[str] = []
    for n, line in enumerate(md.split("\n"), start=1):
        s = line.strip()
        if start is None and s == FENCE_OPEN:
            start, body = n, []
        elif start is not None and s == FENCE_CLOSE:
            out.append((start + 1, "\n".join(body) + "\n"))
            start = None
        elif start is not None and s.startswith("```"):
            problems.append(Problem(name, n, 1, f"fence opened at line {start} never closed"))
            start = n if s == FENCE_OPEN else None
            body = []
        elif start is not None:
            body.append(line)
        elif FIELD_LINE.match(line):
            problems.append(
                Problem(name, n, 1, "fact field outside a fact block (lost opening fence?)")
            )
    if start is not None:
        problems.append(Problem(name, start, 1, "fact block never closed"))
    if len(out) > common.FACTS_MAX:
        raise InputError(f"{name}: {len(out)} facts, FACTS_MAX ({common.FACTS_MAX}) hit")
    return out, problems


def yamllint_problems(first: int, text: str, name: str) -> list[Problem]:
    conf = YamlLintConfig(file=str(YAMLLINT_CONF))
    return [
        Problem(name, first + p.line - 1, p.column, f"[{p.level}] {p.desc}")
        for p in yl_linter.run(text, conf)
    ]


def to_fact(first: int, text: str, name: str) -> tuple[Fact | None, list[Problem]]:
    try:
        d: dict[str, Any] = sy.load(text, SCHEMA).data
    except sy.YAMLError as e:
        mark = getattr(e, "problem_mark", None) or getattr(e, "context_mark", None)
        line, col = (int(mark.line) + 1, int(mark.column) + 1) if mark is not None else (1, 1)
        msg = " ".join(str(e).split())
        return None, [Problem(name, first + line - 1, col, msg[:300])]
    if len(d["text"]) > common.FACT_TEXT_CHARS_MAX:
        return None, [
            Problem(name, first, 1, f"text over FACT_TEXT_CHARS_MAX ({common.FACT_TEXT_CHARS_MAX})")
        ]
    yes = {k: d[k] == "yes" for k in ("is_measure", "paid", "shipped", "confidential")}
    f = Fact(
        d["id"],
        d["entry"],
        " ".join(d["text"].split()),
        yes["is_measure"],
        yes["paid"],
        yes["shipped"],
        d.get("dates"),
        d["source"],
        yes["confidential"],
        d["status"],
        first - 1,
        list(d.get("languages", [])),
        list(d.get("terms", [])),
        list(d.get("links", [])),
        list(d.get("questions", [])),
    )
    return f, []


def load_facts(path: Path) -> tuple[list[Fact], list[Problem]]:
    md = read_evidence(path)
    found, problems = blocks(md, path.name)
    facts: list[Fact] = []
    for first, text in found:
        problems += yamllint_problems(first, text, path.name)
        f, p = to_fact(first, text, path.name)
        problems += p
        if f is not None:
            facts.append(f)
    seen: dict[str, int] = {}
    for f in facts:
        if f.id in seen:
            problems.append(
                Problem(path.name, f.line, 1, f"duplicate fact id {f.id!r} (first at {seen[f.id]})")
            )
        seen[f.id] = f.line
    return facts, problems


NUM_RE = re.compile(
    r"(?<![A-Za-z0-9.])(~|about |approximately |\\textasciitilde\{\}\s*)?"
    r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)"
    r"(st|nd|rd|th)?\s?(%|x|×|h\b|hours?\b|k\b|K\b|M\b|B\b|thousand\b|million\b|billion\b)?"
    r"(?![A-Za-z0-9])",
)
WORD_NUM_RE = re.compile(r"\b(" + "|".join(WORD_NUMBERS) + r")\b", re.IGNORECASE)


def numbers(text: str) -> list[Number]:
    """Numbers a reader would check, with units; digits inside names (R1, v14, x86) are skipped."""
    out: list[Number] = []
    for m in NUM_RE.finditer(text):
        value = float(m.group(2).replace(",", ""))
        unit = (m.group(4) or "").lower()
        if unit in SCALE:
            value, unit = value * SCALE[unit], ""
        unit = {"×": "x", "hour": "h", "hours": "h"}.get(unit, unit)
        out.append(Number(value, unit, m.group(3) is not None, m.group(1) is not None))
    for m in WORD_NUM_RE.finditer(text):
        out.append(Number(float(WORD_NUMBERS[m.group(1).lower()]), "", False, False))
    return out


def same_number(claim: Number, fact: Number) -> bool:
    if claim.unit != fact.unit or claim.ordinal != fact.ordinal:
        return False
    if claim.approx or fact.approx:
        return abs(claim.value - fact.value) <= APPROX_TOLERANCE * max(abs(fact.value), 1e-9)
    return claim.value == fact.value


def norm_link(url: str) -> str:
    return re.sub(r"^(https?://)?(www\.)?", "", url.strip().lower()).rstrip("/")


def fact_dates(f: Fact) -> rp.DateRange | None:
    if f.dates is None:
        return None
    a, b = f.dates.split(" to ")
    start = (int(a[:4]), int(a[5:7]))
    return rp.DateRange(start, None if b == "present" else (int(b[:4]), int(b[5:7])))


def finding(rule: str, loc: str, sev: str, evidence: str, action: str) -> Finding:
    why = {
        "truth-gate": "a claim the evidence doesn't back can't be defended in an interview",
        "private-terms": "a confidential term on the page breaks a promise to an employer",
        "underclaim": "a measured result left off reads as a weaker candidate",
    }[rule]
    return Finding(rule, loc, sev, evidence, why, action)


def show_number(value: float) -> str:
    """20000000, not 2e+07: the friend compares it with the page."""
    return f"{value:,.0f}" if value == int(value) else f"{value:,.4f}".rstrip("0")


def claim_finding(loc: str, what: str, backed: list[Fact]) -> Finding | None:
    """None when a confirmed fact backs it; unconfirmed when only a draft does."""
    if any(f.status == "confirmed" for f in backed):
        return None
    if backed:
        ids = ", ".join(f.id for f in backed)
        return finding(
            "truth-gate",
            loc,
            "blocker",
            f"{what}: unconfirmed (only draft fact {ids})",
            f"confirm fact {ids} in the evidence file, or drop the claim",
        )
    return finding(
        "truth-gate",
        loc,
        "blocker",
        f"{what}: no fact backs it",
        "add a fact that backs it, or change the claim to match the evidence",
    )


def gate_entry(e: rp.Entry, facts: list[Fact]) -> list[Finding]:
    mine = [f for f in facts if f.entry == e.id]
    out: list[Finding | None] = []
    for b in e.bullets:
        for n in numbers(b.text):
            backed = [f for f in mine if any(same_number(n, x) for x in numbers(f.text))]
            out.append(claim_finding(b.id, f"number {show_number(n.value)}{n.unit}", backed))
        for url in b.links:
            backed = [f for f in mine if norm_link(url) in map(norm_link, f.links)]
            out.append(claim_finding(b.id, f"link {url}", backed))
    if e.dates is not None:
        out.append(claim_finding(e.id, "dates", [f for f in mine if fact_dates(f) == e.dates]))
    for tag in e.tags:
        backed = [f for f in mine if tag.lower() in {t.lower() for t in f.languages + f.terms}]
        out.append(claim_finding(e.id, f"tag {tag}", backed))
    for url in e.links:
        backed = [f for f in mine if norm_link(url) in map(norm_link, f.links)]
        out.append(claim_finding(e.id, f"link {url}", backed))
    return [f for f in out if f is not None]


def gate_skills(r: rp.Resume, facts: list[Fact]) -> list[Finding]:
    terms = [f for f in facts if f.languages or f.terms]
    out: list[Finding | None] = []
    for line in r.skills:
        for item in line.items:
            parts = [p.strip().lower() for p in item.split(" (", 1)[0].split("/")]
            backed = [f for f in terms if {t.lower() for t in f.languages + f.terms} & set(parts)]
            out.append(claim_finding("skills", f"skill {item}", backed))
    return [f for f in out if f is not None]


CLAIMS_SHOWN_MAX = 6  # per merged finding; the rest are counted


def merge_claims(loc: str, found: list[Finding]) -> Finding | None:
    """One finding per entry, so a thin evidence file reports, not overflows the cap."""
    if not found:
        return None
    assert all(f.rule_id == "truth-gate" for f in found)
    shown = [f"{f.location_id}: {f.evidence}" for f in found[:CLAIMS_SHOWN_MAX]]
    more = len(found) - len(shown)
    text = "; ".join(shown) + (f"; and {more} more" if more else "")
    return finding(
        "truth-gate",
        loc,
        "blocker",
        f"{len(found)} unbacked claims: {text}",
        "add or confirm the facts that back them, or change the claims",
    )


def truth_gate(r: rp.Resume, facts: list[Fact]) -> list[Finding]:
    merged = [merge_claims("skills", gate_skills(r, facts))]
    for s in r.sections:
        for e in s.entries:
            merged.append(merge_claims(e.id, gate_entry(e, facts)))
    return [f for f in merged if f is not None]


def underclaims(r: rp.Resume, facts: list[Fact]) -> list[Finding]:
    out: list[Finding] = []
    for e in (e for s in r.sections for e in s.entries):
        measured = [
            f
            for f in facts
            if f.entry == e.id and f.is_measure and f.status == "confirmed" and numbers(f.text)
        ]
        on_page = [n for b in e.bullets for n in numbers(b.text)]
        uncited = [
            f
            for f in measured
            if not any(same_number(c, x) for c in on_page for x in numbers(f.text))
        ]
        if not uncited:
            continue
        best = uncited[0]
        if len(uncited) == len(measured):
            out.append(
                finding(
                    "underclaim",
                    e.id,
                    "major",
                    f"no bullet uses a measured result; best {best.id}: {best.text[:80]}",
                    f"lead a bullet with fact {uncited[0].id}'s number",
                )
            )
            uncited = uncited[1:]
        if uncited:  # one nit per entry: a 1-page sheet can't carry every number
            ids = ", ".join(f.id for f in uncited)
            out.append(
                finding(
                    "underclaim",
                    e.id,
                    "nit",
                    f"{len(uncited)} more measured facts unused: {ids}",
                    "use one if a bullet has room; otherwise leave it",
                )
            )
    return out


def private_terms(pdf_text: str, facts: list[Fact]) -> list[Finding]:
    out: list[Finding] = []
    low = common.search_text(pdf_text)
    for f in (f for f in facts if f.confidential):
        for t in f.terms:
            needle = common.search_text(t).split(" \x00 ")[0]  # same folding as the page
            assert needle, f"fact {f.id}: empty private term"
            if re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])", low):
                out.append(
                    finding(
                        "private-terms",
                        "page",
                        "blocker",
                        f"confidential term {t!r} (fact {f.id}) is on the page",
                        f"remove {t!r} from the resume",
                    )
                )
    return out


CHECKS: dict[str, Callable[[rp.Resume, list[Fact], str], list[Finding]]] = {
    "underclaim": lambda r, facts, _text: underclaims(r, facts),
}
FIXED: dict[str, Callable[[rp.Resume, list[Fact], str], list[Finding]]] = {
    "truth-gate": lambda r, facts, _text: truth_gate(r, facts),
    "private-terms": lambda _r, facts, text: private_terms(text, facts),
}


def location_ids(r: rp.Resume) -> set[str]:
    ids = {"page", "skills"} | {s.id for s in r.sections}
    for e in (e for s in r.sections for e in s.entries):
        ids |= {e.id} | {b.id for b in e.bullets}
    return ids


def pdf_text(path: Path) -> str:
    from pypdf import PdfReader

    common.check_pdf_size(path)
    try:
        pages = PdfReader(path).pages
        if len(pages) > common.PDF_PAGES_MAX:
            raise InputError(
                f"{path}: {len(pages)} pages: PDF_PAGES_MAX ({common.PDF_PAGES_MAX}) hit"
            )
        return "\n".join(p.extract_text() or "" for p in pages)
    except InputError:
        raise
    except Exception as err:  # pypdf raises its own types on a corrupt file
        raise InputError(f"{path}: unreadable PDF: {err}") from err


def cmd_check(resume_json: Path, evidence: Path, pdf: Path, out: Path) -> int:
    r = rp.load_resume(resume_json)
    facts, problems = load_facts(evidence)
    if problems:
        raise InputError(f"{evidence}: {len(problems)} problems; run: evidence.py lint {evidence}")
    text = pdf_text(pdf)
    ids = location_ids(r)
    found: list[Finding] = []
    for name, fn in {**FIXED, **CHECKS}.items():
        got = fn(r, facts, text)
        if len(got) > common.FINDINGS_PER_CHECK_MAX:
            raise InputError(
                f"check {name} hit its cap ({len(got)} > {common.FINDINGS_PER_CHECK_MAX})"
            )
        bad = [f.location_id for f in got if f.location_id not in ids]
        assert not bad, f"{name} named unknown locations {bad}"
        print(f"{name}: {len(got)}")
        found += got
    print(f"{len(FIXED) + len(CHECKS)} checks ran on {len(facts)} facts")
    common.findings_out(out, found)
    return 1 if found else 0


def state_path(evidence: Path) -> Path:
    return evidence.parent / ".resumekit" / "evidence_state.json"


def print_problems(problems: list[Problem], stream: Any = sys.stdout) -> None:
    for p in problems:
        print(f"{p.file}:{p.line}:{p.column}: {p.message}", file=stream)


def cmd_lint(evidence: Path) -> int:
    facts, problems = load_facts(evidence)
    print_problems(problems)
    print(f"{len(facts)} fact blocks, {len(problems)} problems")
    return 1 if problems else 0


def cmd_validate(evidence: Path) -> int:
    facts, problems = load_facts(evidence)
    sp = state_path(evidence)
    before = set(common.read_json(sp).get("fact_ids", [])) if sp.exists() else set[str]()
    lost = sorted(before - {f.id for f in facts})
    problems += [
        Problem(
            evidence.name,
            1,
            1,
            f"fact {i!r} is gone; restore its block, or run: evidence.py delete {i} <file>",
        )
        for i in lost
    ]
    print_problems(problems)
    print(f"{len(facts)} fact blocks, {len(problems)} problems")
    if problems:
        return 1
    common.write_json(sp, {"fact_ids": sorted(f.id for f in facts)})
    return 0


def cmd_todo(evidence: Path) -> int:
    facts, problems = load_facts(evidence)
    if problems:
        print_problems(problems)
        return 1
    drafts = [f for f in facts if f.status == "draft"]
    for f in drafts:
        print(f"{evidence.name}:{f.line}: {f.id} (draft): {f.text[:100]}")
        print("\n".join(f"    ? {q}" for q in f.questions))
    print(f"{len(drafts)} draft facts")
    return 0


def cmd_delete(fact_id: str, evidence: Path) -> int:
    lines = read_evidence(evidence).split("\n")
    facts, problems = load_facts(evidence)
    if problems:
        raise InputError(f"{evidence}: fix its problems before deleting (evidence.py lint)")
    hit = [f for f in facts if f.id == fact_id]
    if not hit:
        raise InputError(f"no fact {fact_id!r} in {evidence}")
    start = hit[0].line - 1  # the opening fence, 0-based
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == FENCE_CLOSE)
    evidence.write_text("\n".join(lines[:start] + lines[end + 1 :]), encoding="utf-8")
    sp = state_path(evidence)
    if sp.exists():
        ids = [i for i in common.read_json(sp).get("fact_ids", []) if i != fact_id]
        common.write_json(sp, {"fact_ids": ids})
    print(f"deleted fact {fact_id} (lines {start + 1}-{end + 1})")
    return 0


def public_text(md: str, facts: list[Fact]) -> str:
    """Only the confirmed, non-confidential fact blocks, with no prose around them: an agent
    brief can't leak what the friend marked private, even from a note between facts."""
    lines = md.split("\n")
    keep = [f for f in facts if not f.confidential and f.status == "confirmed"]
    blocks: list[str] = []
    for f in keep:
        start = f.line - 1
        end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == FENCE_CLOSE)
        blocks.append("\n".join(lines[start : end + 1]))
    out = "\n\n".join(blocks) + "\n"
    found = common.search_text(out)
    for f in (f for f in facts if f.confidential):
        for t in f.terms:
            needle = common.search_text(t).split(" \x00 ")[0]
            if re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])", found):
                raise InputError(f"a public fact names {t!r}, a private term of fact {f.id}")
    again, problems = facts_of(out)
    assert not problems and [g.id for g in again] == [f.id for f in keep], "public copy changed"
    return out


def cmd_public(evidence: Path, out: Path) -> int:
    facts, problems = load_facts(evidence)
    if problems:
        raise InputError(f"{evidence}: {len(problems)} problems; run: evidence.py lint {evidence}")
    text = public_text(read_evidence(evidence), facts)
    out.write_text(text, encoding="utf-8")
    print(f"public: {text.count(FENCE_CLOSE)} facts to {out}")
    return 0


def cmd_hook() -> int:
    """PostToolUse: lint an edited file holding fact blocks; exit 2 shows the agent its problems."""
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0
    path = Path(str(event.get("tool_input", {}).get("file_path", "")))
    if (
        not path.is_file()
        or path.suffix != ".md"
        or FENCE_OPEN not in path.read_text(encoding="utf-8")
    ):
        return 0
    facts, problems = load_facts(path)
    if not problems:
        return 0
    print_problems(problems, sys.stderr)
    print(
        f"{len(problems)} problems in {path.name}'s fact blocks ({len(facts)} parsed)",
        file=sys.stderr,
    )
    return 2


def fact_block(fid: str, entry: str, text: str, status: str = "confirmed", extra: str = "") -> str:
    return (
        f"```fact\nid: {fid}\nentry: {entry}\ntext: >\n  {text}\nis_measure: yes\npaid: yes\n"
        f"shipped: yes\nsource: test\nconfidential: no\nstatus: {status}\n{extra}```\n"
    )


def facts_of(md: str) -> tuple[list[Fact], list[Problem]]:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "ev.md"
        p.write_text(md, encoding="utf-8")
        return load_facts(p)


def test_format() -> None:
    good = (
        "# Acme\n_role_\n"
        + fact_block("a-hours", "acme", "Saved analysts ~5 hours a day each.")
        + fact_block(
            "a-place", "acme", "Result: 2nd place overall", "draft", "questions:\n- which round?\n"
        )
    )
    facts, problems = facts_of(good)
    assert len(facts) == 2 and not problems, problems
    cases = {
        "norway": good.replace("paid: yes", "paid: true", 1),
        "missing field": good.replace("shipped: yes\n", "", 1),
        "duplicate key": good.replace(
            "status: confirmed\n", "status: confirmed\nstatus: draft\n", 1
        ),
        "colon in plain text": good.replace("text: >\n  Saved", "text: Saved: analysts", 1),
        "inline list": good.replace("questions:\n- which round?", "questions: [which round?]", 1),
        "lost fence": good.replace("```fact\nid: a-hours", "id: a-hours", 1),
        "trailing space": good.replace("paid: yes\n", "paid: yes \n", 1),
    }
    for name, md in cases.items():
        _, problems = facts_of(md)
        assert problems, f"accepted: {name}"
    _, problems = facts_of(cases["trailing space"])
    assert problems[0].line == 9 and "trailing" in problems[0].message, problems


def test_numbers() -> None:
    got = [
        (n.value, n.unit, n.ordinal, n.approx)
        for n in numbers("~59× faster, 10M rows, 1st of R1, x86, 40% and 4,047 tokens")
    ]
    assert got == [
        (59.0, "x", False, True),
        (1e7, "", False, False),
        (1.0, "", True, False),
        (40.0, "%", False, False),
        (4047.0, "", False, False),
    ], got
    assert same_number(numbers("10M")[0], numbers("10 million")[0])
    assert same_number(numbers("~59x")[0], numbers("62x")[0])
    assert not same_number(numbers("59x")[0], numbers("62x")[0])
    assert not same_number(numbers("1st")[0], numbers("1")[0])
    assert [n.value for n in numbers("three teams")] == [3.0]


def gate_case(bullet: str, facts_md: str) -> list[str]:
    facts, problems = facts_of(facts_md)
    assert not problems, problems
    e = rp.Entry("acme", "Acme", None, [], [], "", [rp.Bullet("acme-b1", bullet, [])])
    r = rp.Resume("t", [rp.Section("experience", "experience", "Experience", [e])])
    merged = truth_gate(r, facts)
    raw = gate_entry(e, facts)
    assert len(merged) == (1 if raw else 0), merged  # one finding per entry
    return [f.evidence for f in raw]


def test_private() -> None:
    """A private term must be caught however the PDF spells it: ligatures, a line break inside a
    two-word term, a word hyphenated at a line end. A longer word that contains it is not a hit."""
    block = fact_block("p1", "acme", "Client work.", extra="terms:\n  - Zafiro\n  - Globex  Labs\n")
    facts, problems = facts_of(block.replace("confidential: no", "confidential: yes"))
    assert not problems and facts[0].confidential, problems
    for page, hits in (
        ("worked at Za\ufb01ro", 1),
        ("built at Globex\nLabs", 1),
        ("at Zafi-\nro in 2023", 1),
        ("Zafirov and Globexer Labs", 0),
    ):
        got = private_terms(page, facts)
        assert len(got) == hits, (page, got)


def test_public() -> None:
    """The agent copy keeps confirmed public facts only, drops prose between blocks, and refuses
    a public fact that names a private term."""
    private = fact_block("p1", "acme", "Client work.", extra="terms:\n  - Zafiro\n").replace(
        "confidential: no", "confidential: yes"
    )
    md = (
        "# notes: Zafiro pays well\n\n"
        + fact_block("a1", "acme", "Cut build time 40%.")
        + "\nprose\n"
        + fact_block("a2", "acme", "Draft idea.", status="draft")
        + private
    )
    facts, problems = facts_of(md)
    assert not problems, problems
    out = public_text(md, facts)
    assert "a1" in out and "a2" not in out and "p1" not in out, out
    assert "notes" not in out and "prose" not in out and "zafiro" not in out.lower(), out
    leak = md.replace("Cut build time 40%.", "Cut Zafiro build time 40%.")
    try:
        public_text(leak, facts_of(leak)[0])
        raise AssertionError("a public fact naming a private term passed")
    except InputError:
        pass


def test_merge() -> None:
    """A thin evidence file must report every unbacked claim in one finding, not hit the cap."""
    nums = " ".join(f"{i + 1}0 items" for i in range(30))
    e = rp.Entry("acme", "Acme", None, [], [], "", [rp.Bullet("acme-b1", nums, [])])
    r = rp.Resume("t", [rp.Section("experience", "experience", "Experience", [e])])
    got = truth_gate(r, [])
    assert len(got) == 1 and got[0].location_id == "acme", got
    assert got[0].evidence.startswith("30 unbacked claims") and "and 24 more" in got[0].evidence


def test_gate() -> None:
    md = fact_block("a-hours", "acme", "Saved analysts ~5 hours a day each.") + fact_block(
        "a-place", "acme", "Placed 2nd overall", "draft"
    )
    assert gate_case("Saved ~5 hours a day each", md) == []
    assert gate_case("Saved ~6 hours a day each", md) == ["number 6h: no fact backs it"]
    assert gate_case("Placed 2nd overall", md) == [
        "number 2: unconfirmed (only draft fact a-place)"
    ]
    priv = fact_block(
        "a-sec", "acme", "Found a flaw in Projectx", extra="terms:\n- Projectx\n"
    ).replace("confidential: no", "confidential: yes")
    facts, _ = facts_of(priv)
    assert len(private_terms("Built Projectx tools", facts)) == 1 and not private_terms(
        "Built tools", facts
    )
    under = rp.Resume(
        "t",
        [
            rp.Section(
                "experience",
                "experience",
                "E",
                [
                    rp.Entry(
                        "acme", "Acme", None, [], [], "", [rp.Bullet("acme-b1", "Shipped it", [])]
                    )
                ],
            )
        ],
    )
    facts, _ = facts_of(md)
    got = [(f.severity, f.location_id) for f in underclaims(under, facts)]
    assert got == [("major", "acme")], got


def self_test() -> int:
    tests = (test_format, test_numbers, test_gate, test_merge, test_private, test_public)
    for t in tests:
        t()
        print(f"ok {t.__name__}")
    print(f"self-test passed ({len(tests)} tests)")
    return 0


def main(argv: list[str]) -> int:
    try:
        if argv == ["--self-test"]:
            return self_test()
        if argv[:2] == ["lint", "--hook"] or argv == ["--hook"]:
            return cmd_hook()
        cmds: dict[tuple[str, int], Callable[[], int]] = {
            ("lint", 2): lambda: cmd_lint(Path(argv[1])),
            ("validate", 2): lambda: cmd_validate(Path(argv[1])),
            ("todo", 2): lambda: cmd_todo(Path(argv[1])),
            ("delete", 3): lambda: cmd_delete(argv[1], Path(argv[2])),
        }
        if argv and (argv[0], len(argv)) in cmds:
            return cmds[(argv[0], len(argv))]()
        if len(argv) == 4 and argv[0] == "public" and argv[2] == "--out":
            return cmd_public(Path(argv[1]), Path(argv[3]))
        if len(argv) == 6 and argv[0] == "check" and argv[4] == "--out":
            return cmd_check(Path(argv[1]), Path(argv[2]), Path(argv[3]), Path(argv[5]))
        print(__doc__, file=sys.stderr)
        return 2
    except InputError as e:
        print(f"evidence: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
