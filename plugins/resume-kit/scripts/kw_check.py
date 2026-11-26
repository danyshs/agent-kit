# /// script
# requires-python = ">=3.10"
# dependencies = ["strictyaml==1.6.2", "yamllint==1.38.0", "pypdf==7.1.1"]
# ///
"""Every posting keyword the evidence backs is on the page, or has a reason not to be.

Usage: kw_check.py <keywords.txt> <evidence.md> <pdf> --out <findings.json>
       kw_check.py --self-test
Keywords file, one line per keyword the posting names:
  Python @"Minimum qualifications"          backed: must be on the page
  git = Git | GitHub @"Preferred"           same; the posting's spelling first, then variants
  Go @"Requirements" #or:lang               an OR-group: one backed member on the page is enough
  Docker ! team work only                   backed, left off on purpose; the reason is required
  - Jira                                    no evidence: a gap, listed for the record
  # comment
A missing backed keyword costs by its heading's tier (data/headings.json): required a blocker,
preferred a major, a soft-preferred OR-group sibling or a responsibilities-only keyword a nit,
unknown a major. Exit 0 none, 1 findings, 2 bad input, 3 missing package.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # sibling module, not a package
from common import Finding, FitWarning, InputError

common.require(["strictyaml", "yamllint", "pypdf"])
import evidence  # noqa: E402  (after the package guard on purpose)

PLUGIN = Path(__file__).resolve().parent.parent
PLURAL_MIN_CHARS = 3  # "C" + "s" is "CS", a different word
TIER_SEVERITY = {  # user, 2026-11-25: lowest cost last
    "required": "blocker",
    "preferred": "major",
    "responsibilities": "nit",
    "unknown": "major",
}
LINE_RE = re.compile(
    r'^(?P<head>[^@#!]+?)\s*(?:@"(?P<heading>[^"]+)")?\s*(?:#or:(?P<group>[\w-]+))?\s*$'
)


@dataclass
class Keyword:
    spellings: list[str]
    tier: str
    group: str | None
    form: str  # "claim", "omit" or "gap"
    reason: str = ""


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).lower()


def has_word(text: str, word: str) -> bool:
    """Whole word in squashed text; a symbol-ending keyword ("C++") needs no boundary after it."""
    w = squash(word).strip()
    assert w, "empty keyword"
    left = r"(?<![a-z0-9])" if w[0].isalnum() else ""
    plural = r"(?:e?s)?" if len(w) >= PLURAL_MIN_CHARS else ""
    right = plural + r"(?![a-z0-9+#])" if w[-1].isalnum() else ""
    return re.search(left + re.escape(w) + right, text) is not None


def tier_of(heading: str | None, tiers: dict[str, str]) -> str:
    return "unknown" if heading is None else tiers.get(heading.strip().lower(), "unknown")


def parse_line(raw: str, tiers: dict[str, str]) -> Keyword | None:
    line = raw.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("- "):
        return Keyword([line[2:].split("@", 1)[0].strip()], "unknown", None, "gap")
    main, bang, reason = line.partition("!")
    if bang and not reason.strip():
        raise InputError(f"a left-off keyword needs a reason: {raw!r}")
    m = LINE_RE.match(main)
    if m is None:
        raise InputError(f"can't read keyword line: {raw!r}")
    word, _, alts = m.group("head").partition("=")
    spellings = [word.strip()] + [a.strip() for a in alts.split("|") if alts]
    if not all(spellings):
        raise InputError(f"empty spelling in: {raw!r}")
    return Keyword(
        spellings,
        tier_of(m.group("heading"), tiers),
        m.group("group"),
        "omit" if bang else "claim",
        reason.strip(),
    )


def parse_keywords(path: Path) -> list[Keyword]:
    tiers: dict[str, str] = json.loads(
        (PLUGIN / "data" / "headings.json").read_text(encoding="utf-8")
    )
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        raise InputError(f"{path}: {e}") from e
    kws = [k for k in (parse_line(raw, tiers) for raw in lines) if k is not None]
    names = [k.spellings[0].lower() for k in kws]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise InputError(f"keyword listed twice: {dupes}")
    if len(kws) > common.KEYWORDS_MAX:
        raise InputError(f"{len(kws)} keywords: KEYWORDS_MAX ({common.KEYWORDS_MAX}) hit")
    return kws


def kw_finding(severity: str, evidence_text: str, action: str) -> Finding:
    why = "a screener, often a keyword filter, reads for the posting's words before anything else"
    return Finding("keywords", "page", severity, evidence_text, why, action)


def strength(word: str, facts: list[evidence.Fact]) -> tuple[int, int, int]:
    """Paid facts, then shipped facts, then any facts that mention the word."""
    hits = [f for f in facts if has_word(squash(" ".join([f.text, *f.terms, *f.languages])), word)]
    return (sum(f.paid for f in hits), sum(f.shipped for f in hits), len(hits))


def check_single(k: Keyword, page: str, backed: str) -> list[Finding]:
    on_page = any(has_word(page, s) for s in k.spellings)
    word = k.spellings[0]
    if k.form == "claim" and not on_page:
        sev = TIER_SEVERITY[k.tier]
        return [
            kw_finding(
                sev,
                f"{word!r} ({k.tier}) is not on the page",
                f"work {word!r} into a bullet or the skills",
            )
        ]
    if k.form == "omit" and on_page:
        return [
            kw_finding(
                "nit",
                f"{word!r} is marked left off but is on the page",
                "update the keywords file",
            )
        ]
    if k.form == "gap" and has_word(backed, word):
        return [
            kw_finding(
                "major",
                f"{word!r} is listed as a gap but the evidence mentions it",
                "claim it, or mark it left off with a reason",
            )
        ]
    return []


def check_group(members: list[Keyword], page: str, facts: list[evidence.Fact]) -> list[Finding]:
    claims = [k for k in members if k.form == "claim"]
    if not claims:
        return []
    shown = [k for k in claims if any(has_word(page, s) for s in k.spellings)]
    primary = max(claims, key=lambda k: strength(k.spellings[0], facts))
    best = primary.spellings[0]
    if not shown:
        sev = TIER_SEVERITY[primary.tier]
        return [
            kw_finding(
                sev,
                f"no member of OR-group {primary.group} is on the page; best: {best!r}",
                f"work {primary.spellings[0]!r} onto the page",
            )
        ]
    # one shown member clears the group; a backed sibling left off is soft-preferred (user)
    return [
        kw_finding(
            "nit",
            f"{k.spellings[0]!r} (OR-group {k.group}) is backed but not on the page",
            "optional: one member already clears the group",
        )
        for k in claims
        if k not in shown
    ]


def keywords(
    kws: list[Keyword], page: str, facts: list[evidence.Fact]
) -> tuple[list[Finding], list[FitWarning]]:
    confirmed = [f for f in facts if f.status == "confirmed"]
    backed = squash(" ".join(" ".join([f.text, *f.terms, *f.languages]) for f in confirmed))
    found: list[Finding] = []
    warnings: list[FitWarning] = []
    groups: dict[str, list[Keyword]] = {}
    for k in kws:
        if k.group is not None:
            groups.setdefault(k.group, []).append(k)
            if k.form == "gap":
                warnings.append(FitWarning(k.spellings[0], f"OR-group {k.group}: no evidence"))
            continue
        found += check_single(k, page, backed)
        if k.form == "claim" and not any(has_word(backed, s) for s in k.spellings):
            warnings.append(
                FitWarning(k.spellings[0], "claimed, but no confirmed fact mentions it")
            )
        if k.form == "gap":
            warnings.append(
                FitWarning(k.spellings[0], "the posting names it and there's no evidence")
            )
    for members in groups.values():
        found += check_group(members, page, confirmed)
    return found, warnings


CHECKS = {"keywords": keywords}


def run(kw_file: Path, ev_file: Path, pdf: Path, out: Path) -> int:
    kws = parse_keywords(kw_file)
    facts, problems = evidence.load_facts(ev_file)
    if problems:
        raise InputError(f"{ev_file}: {len(problems)} problems; run: evidence.py lint {ev_file}")
    page = squash(evidence.pdf_text(pdf))
    found, warnings = keywords(kws, page, facts)
    if len(found) > common.FINDINGS_PER_CHECK_MAX:
        raise InputError(
            f"check keywords hit its cap ({len(found)} > {common.FINDINGS_PER_CHECK_MAX})"
        )
    claims = [k for k in kws if k.form == "claim"]
    print(f"keywords: {len(claims)} claimed, {len(found)} findings, {len(warnings)} fit warnings")
    for f in found:
        print(f"  {f.severity}: {f.evidence}")
    common.findings_out(out, found, {"fit_warnings": [asdict(w) for w in warnings]})
    return 1 if found else 0


def self_test() -> int:
    tiers = {"minimum qualifications": "required", "preferred qualifications": "preferred"}
    k = parse_line('git = Git | GitHub @"Preferred qualifications" #or:vcs', tiers)
    assert (
        k is not None
        and k.spellings == ["git", "Git", "GitHub"]
        and k.tier == "preferred"
        and k.group == "vcs"
    ), k
    odd = parse_line('Rust @"Odd heading"', tiers)
    assert odd is not None and odd.tier == "unknown"
    for bad in ("Docker !", "= x"):
        try:
            parse_line(bad, tiers)
            raise AssertionError(f"accepted {bad!r}")
        except InputError:
            pass
    assert (
        has_word("python and c++", "C++")
        and not has_word("ci and cs", "C")
        and has_word("code reviews", "code review")
    )
    page = squash("Built in Python and Go")
    facts_md = evidence.fact_block("a", "acme", "Shipped Rust and Go services, used Docker.")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "ev.md"
        p.write_text(facts_md, encoding="utf-8")
        facts, _ = evidence.load_facts(p)
    kws = [
        parse_line(x, tiers)
        for x in (
            'Python @"Minimum qualifications"',
            'Java @"Preferred qualifications"',
            'Scala @"Minimum qualifications"',
            "Kafka",
            "Rust #or:sys",
            "Go #or:sys",
            "- Docker",
            "Go2 ! not mine",
        )
    ]
    found, warns = keywords([x for x in kws if x is not None], page, facts)
    got = sorted((f.severity, f.evidence.split(" ")[0]) for f in found)
    assert got == [
        ("blocker", "'Scala'"),
        ("major", "'Docker'"),
        ("major", "'Java'"),
        ("major", "'Kafka'"),
        ("nit", "'Rust'"),
    ], got
    assert {w.keyword for w in warns} >= {"Python", "Java", "Kafka"}, warns
    print("self-test passed")
    return 0


def main(argv: list[str]) -> int:
    try:
        if argv == ["--self-test"]:
            return self_test()
        if len(argv) == 5 and argv[3] == "--out":
            return run(Path(argv[0]), Path(argv[1]), Path(argv[2]), Path(argv[4]))
        print(__doc__, file=sys.stderr)
        return 2
    except InputError as e:
        print(f"kw_check: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
