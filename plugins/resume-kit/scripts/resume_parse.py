# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Reads a marked resume .tex into resume.json: sections, entries, bullets, skills, with stable IDs.

Usage: resume_parse.py <tex> --out <resume.json>
       resume_parse.py ids <tex>  fill missing slugs and bullet IDs in place (keeps a .bak)
       resume_parse.py --self-test
Markers (tex/resumekit.sty): \rkSection{kind}{call}, \rkEntry{slug}{call}, \rkBullet[id]{call},
\rkSkillLine{label}{call}. The parser reads only the markers, never a template macro by name.
Exit 0 ok, 2 bad input, 3 missing package.
"""

from __future__ import annotations

import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # sibling module, not a package
from common import InputError

MONTHS = {
    m: i + 1
    for i, m in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    )
}
CDOT = "\x01"  # stands in for $\cdot$ while splitting lists
WAIVABLE = {
    "skills": "skills-backed",
    "tags": "tags-in-languages",
    "skills-backed": "skills-backed",
    "tags-in-languages": "tags-in-languages",
}
SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
SLUG_CHARS_MAX = 24
MARKER_RE = re.compile(r"\\rk(Section|Entry|Bullet|SkillLine)(?![A-Za-z])")
WAIVER_RE = re.compile(r"lint-ok:\s*(\S+)\s+(.+?)\s*=\s*(.+?)\s*$")


@dataclass
class DateRange:
    start: tuple[int, int]
    end: tuple[int, int] | None  # None: present


@dataclass
class Bullet:
    id: str
    text: str
    links: list[str]


@dataclass
class Entry:
    id: str
    title: str
    dates: DateRange | None
    tags: list[str]
    links: list[str]
    bullets: list[Bullet] = field(default_factory=list[Bullet])


@dataclass
class SkillLine:
    label: str
    items: list[str]


@dataclass
class Section:
    kind: str
    id: str
    title: str
    entries: list[Entry] = field(default_factory=list[Entry])


@dataclass
class Waiver:
    check: str
    item: str
    reason: str


@dataclass
class Resume:
    path: str
    sections: list[Section] = field(default_factory=list[Section])
    skills: list[SkillLine] = field(default_factory=list[SkillLine])
    waivers: list[Waiver] = field(default_factory=list[Waiver])


def read_group(text: str, i: int, open_ch: str = "{") -> tuple[str, int]:
    """Reads one balanced group at text[i] (after spaces); returns its inside and its end."""
    close_ch = {"{": "}", "[": "]"}[open_ch]
    while i < len(text) and text[i].isspace():
        i += 1
    if i >= len(text) or text[i] != open_ch:
        raise InputError(f"expected {open_ch} at offset {i}: {text[i : i + 40]!r}")
    depth, j = 0, i
    while j < len(text):
        if text[j] == "\\":
            j += 2
            continue
        depth += {open_ch: 1, close_ch: -1}.get(text[j], 0)
        if depth == 0:
            return text[i + 1 : j], j + 1
        j += 1
    raise InputError(f"unbalanced {open_ch} from offset {i}")


def read_args(text: str, i: int, n: int) -> tuple[list[str], int]:
    args: list[str] = []
    for _ in range(n):
        arg, i = read_group(text, i)
        args.append(arg)
    return args, i


def call_args(call: str) -> list[str]:
    """The brace arguments of the first macro call in a marker's argument."""
    m = re.search(r"\\[A-Za-z]+\*?", call)
    if m is None:
        return [call]
    args: list[str] = []
    i = m.end()
    while True:
        j = i
        while j < len(call) and call[j].isspace():
            j += 1
        if j >= len(call) or call[j] != "{":
            return args or [call]
        arg, i = read_group(call, j)
        args.append(arg)


def blank_comments(text: str) -> tuple[str, list[Waiver]]:
    """Replaces % comments with spaces (offsets kept); collects lint-ok waivers."""
    out: list[str] = []
    waivers: list[Waiver] = []
    for line in text.split("\n"):
        m = re.search(r"(?<!\\)%", line)
        if m is None:
            out.append(line)
            continue
        w = WAIVER_RE.search(line[m.end() :])
        if w:
            if w.group(1) not in WAIVABLE:
                raise InputError(f"waiver for unknown check {w.group(1)!r}: {line.strip()}")
            waivers.append(Waiver(WAIVABLE[w.group(1)], w.group(2), w.group(3)))
        out.append(line[: m.start()] + " " * (len(line) - m.start()))
    return "\n".join(out), waivers


def to_plain(latex: str) -> str:
    """LaTeX fragment to the text a reader sees, near enough for matching."""
    t = latex.replace(r"\textasciitilde{}", "\x02").replace("$\\sim$", "\x02")
    t = re.sub(r"\\href\{[^{}]*\}", "", t)
    t = re.sub(r"\\(?:color|textcolor)\{[^{}]*\}", "", t)
    for a, b in (
        (r"\&", "&"),
        (r"\%", "%"),
        (r"\#", "#"),
        (r"\$", "$"),
        ("$\\times$", "×"),
        ("$\\cdot$", CDOT),
        ("$-$", "-"),
        ("---", "—"),
        ("--", "–"),
        (r"\,", " "),
        (r"\\", " "),
        ("~", " "),
    ):
        t = t.replace(a, b)
    t = re.sub(r"\\[a-zA-Z]+\*?", " ", t)
    t = t.replace("{", "").replace("}", "").replace("$", "").replace("\x02", "~")
    return re.sub(r"\s+", " ", t).strip()


def parse_date(text: str) -> DateRange:
    """Raises InputError on any form that isn't a month-year, a range, or year-year."""
    t = to_plain(text)
    one = r"([A-Z][a-z]{2})[a-z]*\.?\s+(\d{4})"
    m = re.fullmatch(one, t)
    if m and m.group(1) in MONTHS:
        ym = (int(m.group(2)), MONTHS[m.group(1)])
        return DateRange(ym, ym)
    m = re.fullmatch(one + r"\s*[–—-]+\s*(?:Present|" + one + ")", t)
    if m and m.group(1) in MONTHS:
        start = (int(m.group(2)), MONTHS[m.group(1)])
        if m.group(3) is None:
            return DateRange(start, None)
        if m.group(3) not in MONTHS:
            raise InputError(f"unknown month in {text!r}")
        return DateRange(start, (int(m.group(4)), MONTHS[m.group(3)]))
    m = re.fullmatch(r"(\d{4})\s*[–—-]+\s*(\d{4})", t)
    if m:
        return DateRange((int(m.group(1)), 1), (int(m.group(2)), 12))
    raise InputError(f"not a date: {text!r}")


def split_list(latex: str) -> list[str]:
    """Splits a comma- or cdot-separated list at parenthesis depth 0; returns plain items."""
    items: list[str] = []
    depth, cur = 0, ""
    for ch in to_plain(latex):
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if ch in ("," + CDOT) and depth == 0:
            items.append(cur)
            cur = ""
        else:
            cur += ch
    items.append(cur)
    return [i.strip() for i in items if i.strip()]


def links_in(latex: str) -> list[str]:
    return re.findall(r"\\href\{([^{}]*)\}", latex)


def entry_fields(kind: str, call: str) -> tuple[str, DateRange | None, list[str]]:
    """Title, dates and heading tags from an entry's heading call. Tags: the last argument when it's
    a list (1-page layout), plus the part after --- in Projects and Research (where Experience puts
    a location)."""
    args = call_args(call)
    head = args[0].split("---", 1)
    title = to_plain(head[0]).strip(" —")
    dates: DateRange | None = None
    tags: list[str] = []
    if len(head) == 2 and kind in ("projects", "research"):
        tags += split_list(head[1])
    for a in args[1:]:
        if dates is None:
            try:
                dates = parse_date(a)
                continue
            except InputError:
                pass
        if "---" in a and kind in ("projects", "research"):
            tags += split_list(a.rsplit("---", 1)[1])
    last = args[-1] if len(args) > 1 else ""
    if last and "---" not in last and dates is not None and to_plain(last) != title:
        try:
            parse_date(last)
        except InputError:
            tags += split_list(last)
    return title, dates, tags


def new_section(r: Resume, kind: str, call: str) -> Section:
    if kind not in common.SECTION_KINDS:
        raise InputError(f"unknown section kind {kind!r}; one of {', '.join(common.SECTION_KINDS)}")
    same = [s for s in r.sections if s.kind == kind]
    if same and kind != "other":
        raise InputError(f"two {kind} sections; only 'other' may repeat")
    sid = f"other-{len(same) + 1}" if kind == "other" else kind
    return Section(kind, sid, to_plain(call))


def add_bullet(entry: Entry | None, bid: str, call: str, ids: set[str]) -> None:
    if entry is None:
        raise InputError(f"bullet before any entry: {to_plain(call)[:60]!r}")
    if not bid:
        raise InputError(f"bullet with no ID in {entry.id!r}; run: resume_parse.py ids <tex>")
    if not re.fullmatch(re.escape(entry.id) + r"-b\d+", bid):
        raise InputError(f"bullet ID {bid!r} doesn't belong to entry {entry.id!r}")
    if bid in ids:
        raise InputError(f"duplicate bullet ID {bid!r}")
    ids.add(bid)
    entry.bullets.append(Bullet(bid, to_plain(call), links_in(call)))
    if len(entry.bullets) > common.BULLETS_PER_ENTRY_MAX:
        raise InputError(f"{entry.id}: over BULLETS_PER_ENTRY_MAX ({common.BULLETS_PER_ENTRY_MAX})")


def parse_text(raw: str, path: str = "<text>") -> Resume:
    text, waivers = blank_comments(raw)
    r = Resume(path, waivers=waivers)
    section: Section | None = None
    entry: Entry | None = None
    ids: set[str] = set()
    for m in MARKER_RE.finditer(text):
        kind, i = m.group(1), m.end()
        bid = ""
        if kind == "Bullet" and text[i:].lstrip().startswith("["):
            bid, i = read_group(text, i, "[")
        args, _ = read_args(text, i, 1 if kind == "Bullet" else 2)
        if kind == "Section":
            section, entry = new_section(r, args[0].strip(), args[1]), None
            r.sections.append(section)
        elif section is None:
            raise InputError(f"\\rk{kind} outside any \\rkSection")
        elif kind == "SkillLine":
            if section.kind != "skills":
                raise InputError(f"\\rkSkillLine outside the skills section ({section.id})")
            items = re.sub(r"^\s*\\textbf\{[^{}]*\}", "", args[1])
            r.skills.append(SkillLine(to_plain(args[0]).rstrip(":"), split_list(items)))
        elif kind == "Entry":
            slug = args[0].strip()
            if not SLUG_RE.fullmatch(slug) or len(slug) > SLUG_CHARS_MAX:
                raise InputError(f"bad entry slug {slug!r}; run: resume_parse.py ids <tex>")
            if any(e.id == slug for s in r.sections for e in s.entries):
                raise InputError(f"duplicate entry slug {slug!r}")
            title, dates, tags = entry_fields(section.kind, args[1])
            entry = Entry(slug, title, dates, tags, links_in(args[1]))
            section.entries.append(entry)
        else:
            add_bullet(entry, bid.strip(), args[0], ids)
    check_shape(r)
    return r


def check_shape(r: Resume) -> None:
    if not r.sections:
        raise InputError(f"{r.path}: no \\rkSection markers; run /resume-setup to mark the resume")
    for s in r.sections:
        if not s.entries and s.kind != "skills":
            raise InputError(f"section {s.id!r} has no entries")
    n_entries = sum(len(s.entries) for s in r.sections)
    for count, cap, what in (
        (len(r.sections), common.SECTIONS_MAX, "SECTIONS_MAX"),
        (n_entries, common.ENTRIES_MAX, "ENTRIES_MAX"),
        (len(r.skills), common.SKILL_LINES_MAX, "SKILL_LINES_MAX"),
    ):
        if count > cap:
            raise InputError(f"{r.path}: {count} over {what} ({cap})")


def parse_tex(path: Path) -> Resume:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise InputError(f"{path}: {e}") from e
    return parse_text(raw, str(path))


def slugify(title: str, taken: set[str]) -> str:
    base = (
        re.sub(r"[^a-z0-9]+", "-", to_plain(title).lower()).strip("-")[:SLUG_CHARS_MAX].strip("-")
        or "entry"
    )
    slug, n = base, 2
    while slug in taken:
        suffix = f"-{n}"
        slug = base[: SLUG_CHARS_MAX - len(suffix)].strip("-") + suffix
        n += 1
    return slug


def marker_spans(text: str) -> list[tuple[str, int, int, str, str]]:
    """(kind, marker end, first arg end, current id or "", first call arg) for every marker."""
    spans: list[tuple[str, int, int, str, str]] = []
    for m in MARKER_RE.finditer(text):
        kind, i = m.group(1), m.end()
        cur = ""
        if kind == "Bullet":
            if text[i:].lstrip().startswith("["):
                cur, i = read_group(text, i, "[")
            args, _ = read_args(text, i, 1)
        else:
            cur, j = read_group(text, i)
            args, _ = read_args(text, j, 1)
        spans.append((kind, m.end(), i, cur.strip(), args[0]))
    return spans


def fill_ids(raw: str) -> tuple[str, int]:
    """Returns the text with missing slugs and bullet IDs filled, and how many were filled."""
    text, _ = blank_comments(raw)
    spans = marker_spans(text)
    slugs = {cur for kind, _, _, cur, _ in spans if kind == "Entry" and cur}
    used = {cur for kind, _, _, cur, _ in spans if kind == "Bullet" and cur}
    edits: list[tuple[int, int, str]] = []  # start, end, replacement
    slug = ""
    for kind, start, after, cur, call in spans:
        if kind == "Entry":
            slug = cur
            if not cur:
                slug = slugify(call_args(call)[0], slugs)
                slugs.add(slug)
                g0 = text.index("{", start)
                edits.append((g0, g0 + 2, "{" + slug + "}"))
        elif kind == "Bullet" and not cur and slug:
            k = 1
            while f"{slug}-b{k}" in used:
                k += 1
            used.add(f"{slug}-b{k}")
            edits.append((start, after, f"[{slug}-b{k}]"))
    for s, e, rep in sorted(edits, reverse=True):
        raw = raw[:s] + rep + raw[e:]
    return raw, len(edits)


def cmd_ids(path: Path) -> int:
    raw = path.read_text(encoding="utf-8")
    new, n = fill_ids(raw)
    if n:
        shutil.copyfile(path, path.with_suffix(path.suffix + ".bak"))
        path.write_text(new, encoding="utf-8")
    parse_text(new, str(path))  # the result must parse
    print(f"{n} IDs filled in {path}")
    return 0


def summary(r: Resume) -> str:
    n_e = sum(len(s.entries) for s in r.sections)
    n_b = sum(len(e.bullets) for s in r.sections for e in s.entries)
    lines = [
        f"{len(r.sections)} sections, {n_e} entries, {n_b} bullets, {len(r.skills)} skill lines"
    ]
    for s in r.sections:
        lines.append(f"  {s.id}: {s.title}")
        lines += [f"    {e.id}: {e.title} ({len(e.bullets)} bullets)" for e in s.entries]
    return "\n".join(lines)


GOOD = r"""
\rkSection{skills}{\section{Technical Skills}}
\rkSkillLine{Languages}{\textbf{Languages:}{ Python, C, Rust}}
\rkSection{experience}{\section{Experience}}
\rkEntry{acme}{\resumeSubheading{Acme}{Aug 2026 -- Present}{Engineer --- Toronto}{Python, Go}}
\rkBullet[acme-b1]{\resumeItem{Cut build time 40\% by caching, \href{https://x.y/1}{link}.}}
% \rkBullet[acme-b9]{\resumeItem{commented out}}
\rkBullet[acme-b2]{\resumeItem{Shipped it.}} % lint-ok: skills Rust = toy
\rkSection{projects}{\section{Projects}}
\rkEntry{tool}{\resumeSubheading{\href{https://g.h/t}{Tool}}{Jan 2025}{CLI --- Rust, fish}{}}
\rkBullet[tool-b1]{\resumeItem{Built it.}}
\rkSection{other}{\section{Awards}}
\rkEntry{award}{\resumeSubheading{Award}{2019 -- 2020}{}{}}
\rkSection{other}{\section{Volunteering}}
\rkEntry{vol}{\resumeSubheading{Vol}{Mar 2024 -- Apr 2024}{}{}}
"""

EA = r"\rkSection{experience}{E}\rkEntry{a}{\x{A}}"  # a section with one entry
BAD = {
    "zero markers": r"\section{Experience}",
    "entry outside section": r"\rkEntry{a}{\x{A}{Jan 2025}}",
    "bullet before entry": r"\rkSection{experience}{\section{E}}\rkBullet[a-b1]{\i{x}}",
    "duplicate slug": EA + r"\rkEntry{a}{\x{B}}",
    "duplicate bullet id": EA + r"\rkBullet[a-b1]{x}\rkBullet[a-b1]{y}",
    "missing bullet id": EA + r"\rkBullet{x}",
    "unknown bullet id": EA + r"\rkBullet[b-b1]{x}",
    "section with no entries": r"\rkSection{experience}{E}" + EA.replace("experience", "projects"),
    "two experience sections": EA + r"\rkSection{experience}{F}",
    "unknown kind": r"\rkSection{hobbies}{H}",
    "bad slug": r"\rkSection{experience}{E}\rkEntry{Acme Co}{\x{A}}",
    "skill line outside skills": r"\rkSection{experience}{E}\rkSkillLine{L}{x}\rkEntry{a}{\x{A}}",
    "unknown waiver": "% lint-ok: spill x = y\n\\rkSection{experience}{E}\\rkEntry{a}{\\x{A}}",
    "over a cap": EA + "".join(f"\\rkBullet[a-b{k}]{{x}}" for k in range(1, 14)),
}


def test_good() -> None:
    r = parse_text(GOOD)
    assert [s.id for s in r.sections] == [
        "skills",
        "experience",
        "projects",
        "other-1",
        "other-2",
    ], r.sections
    acme = r.sections[1].entries[0]
    assert acme.dates == DateRange((2026, 8), None) and acme.tags == ["Python", "Go"], acme
    assert [b.id for b in acme.bullets] == ["acme-b1", "acme-b2"], "commented bullet must not count"
    assert acme.bullets[0].links == ["https://x.y/1"] and "40%" in acme.bullets[0].text
    tool = r.sections[2].entries[0]
    assert (
        tool.title == "Tool" and tool.tags == ["Rust", "fish"] and tool.links == ["https://g.h/t"]
    ), tool
    assert r.skills == [SkillLine("Languages", ["Python", "C", "Rust"])], r.skills
    assert r.waivers == [Waiver("skills-backed", "Rust", "toy")], r.waivers
    assert r.sections[3].entries[0].dates == DateRange((2019, 1), (2020, 12))


def test_bad() -> None:
    for name, text in BAD.items():
        try:
            parse_text(text)
        except InputError:
            continue
        raise AssertionError(f"accepted bad input: {name}")


def test_ids() -> None:
    raw = (
        r"\rkSection{experience}{E}\rkEntry{}{\x{Acme Co.}}\rkBullet[acme-co-b1]{a}\rkBullet{b}"
        "\n% \\rkBullet{commented}\n"
        r"\rkEntry{}{\x{Acme Co.}}\rkBullet[]{c}"
    )
    new, n = fill_ids(raw)
    assert n == 4, (n, new)
    r = parse_text(new)
    got = [(e.id, [b.id for b in e.bullets]) for e in r.sections[0].entries]
    assert got == [("acme-co", ["acme-co-b1", "acme-co-b2"]), ("acme-co-2", ["acme-co-2-b1"])], got
    assert "% \\rkBullet{commented}" in new
    assert fill_ids(new) == (new, 0), "ids must be idempotent"


def self_test() -> int:
    for t in (test_good, test_bad, test_ids):
        t()
        print(f"ok {t.__name__}")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "r.tex"
        p.write_text(GOOD, encoding="utf-8")
        assert main([str(p), "--out", str(Path(d) / "r.json")]) == 0
        assert common.read_json(Path(d) / "r.json")["sections"][1]["entries"][0]["id"] == "acme"
    print(f"self-test passed (3 tests, {len(BAD)} bad inputs)")
    return 0


def main(argv: list[str]) -> int:
    try:
        if argv == ["--self-test"]:
            return self_test()
        if len(argv) == 2 and argv[0] == "ids":
            return cmd_ids(Path(argv[1]))
        if len(argv) == 3 and argv[1] == "--out":
            r = parse_tex(Path(argv[0]))
            common.write_json(Path(argv[2]), r)
            print(summary(r))
            return 0
        print(__doc__, file=sys.stderr)
        return 2
    except InputError as e:
        print(f"resume_parse: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
