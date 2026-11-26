# /// script
# requires-python = ">=3.10"
# dependencies = ["strictyaml==1.6.2", "yamllint==1.38.0", "pypdf==7.1.1"]
# ///
"""The sharp page checks: each one is keyed by its rule ID and reads its lists from the rule.

Usage: lint.py <resume.json> <pdf> --settings <.resumekit.json> --out <findings.json>
       lint.py --self-test
Exit 0 no findings, 1 findings, 2 bad input or a cap hit, 3 missing package.
"""

from __future__ import annotations

import datetime
import re
import sys
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # sibling module, not a package
from common import Finding, InputError

common.require(["strictyaml", "yamllint", "pypdf"])
import evidence  # noqa: E402  (imports below sit after the package guard on purpose)
import resume_parse as rp  # noqa: E402
from pypdf import PdfReader  # noqa: E402

import rules  # noqa: E402

YM = tuple[int, int]
DATED_KINDS = ("experience", "education", "research", "projects", "other")  # every dated section
SOFT_ORDER_KINDS = ("research", "education", "other")  # projects go by relevance (RB 7.2)


@dataclass
class Pdf:
    path: str
    lines: list[list[str]]  # per page, its non-empty text lines
    ys: list[list[float]] = field(default_factory=list[list[float]])  # per page, text baselines
    fonts: list[float] = field(default_factory=list[float])  # page 1 sizes, one per character


@dataclass
class Ctx:
    resume: rp.Resume
    pdf: Pdf
    settings: common.Settings
    paid_years: float
    now: YM


Check = Callable[[Ctx, dict[str, Any], str], list[Finding]]


def nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def visitor(ys: list[float], fonts: list[float] | None) -> Callable[..., None]:
    """Collects text baselines, and on page 1 the font size of every character."""

    def visit(text: str, cm: list[float], tm: list[float], _f: Any, size: float) -> None:
        if not text.strip():
            return
        ys.append(tm[4] * cm[1] + tm[5] * cm[3] + cm[5])
        if fonts is not None:
            scale = abs(tm[3] * cm[3]) or 1.0
            fonts.extend([round(size * scale, 1)] * len(text.strip()))

    return visit


def read_pdf(path: Path) -> Pdf:
    common.check_pdf_size(path)
    try:
        reader = PdfReader(path)
        pages = reader.pages
        if len(pages) > common.PDF_PAGES_MAX:
            raise InputError(
                f"{path}: {len(pages)} pages: PDF_PAGES_MAX ({common.PDF_PAGES_MAX}) hit"
            )
        pdf = Pdf(str(path), [])
        for i, page in enumerate(pages):
            ys: list[float] = []
            visit = visitor(ys, pdf.fonts if i == 0 else None)

            raw = page.extract_text(visitor_text=visit)
            pdf.lines.append([ln.strip() for ln in nfkc(raw).splitlines() if ln.strip()])
            pdf.ys.append(ys)
    except InputError:
        raise
    except Exception as err:  # pypdf raises its own types on a corrupt file
        raise InputError(f"{path}: unreadable PDF: {err}") from err
    return pdf


def months(a: YM, b: YM) -> int:
    """Months from a to b, counting both ends."""
    return (b[0] - a[0]) * 12 + b[1] - a[1] + 1


def add_months(a: YM, n: int) -> YM:
    k = a[0] * 12 + a[1] - 1 + n
    return (k // 12, k % 12 + 1)


def entries(r: rp.Resume, kinds: tuple[str, ...]) -> list[rp.Entry]:
    return [e for s in r.sections if s.kind in kinds for e in s.entries]


def finding(rule: str, loc: str, sev: str, ev: str, why: str, action: str) -> Finding:
    return Finding(rule, loc, sev, ev, why, action)


def waiver(r: rp.Resume, check: str, item: str) -> rp.Waiver | None:
    return next((w for w in r.waivers if w.check == check and w.item == item), None)


def need(params: dict[str, Any], key: str) -> Any:
    if key not in params:
        raise InputError(f"rule params lack {key!r}: data/rules.json is out of date")
    return params[key]


# -- page checks


def pages(ctx: Ctx, _p: dict[str, Any], sev: str) -> list[Finding]:
    n, rule = len(ctx.pdf.lines), ctx.settings.page_rule
    if (rule == 1 and n != 1) or (rule == 2 and n > 2):
        why = "the page rule in the settings is the reader's limit; anything past it is not read"
        return [finding("pages", "page", sev, f"{n} pages, page rule {rule}", why, "cut to fit")]
    return []


def page_length(ctx: Ctx, p: dict[str, Any], sev: str) -> list[Finding]:
    n = len(ctx.pdf.lines)
    under = need(p, "paid_years_under")
    if n == 2 and ctx.paid_years < under:
        ev = f"2 pages on {ctx.paid_years:.1f} paid software years (under {under})"
        why = "under about 5 years of paid work, one page is the hard ceiling"
        return [finding("page-length", "page", sev, ev, why, "cut to one page")]
    if n >= 3 and not ctx.settings.academic:
        why = "three or more pages is outside industry norms unless the role is academic"
        return [finding("page-length", "page", sev, f"{n} pages", why, "cut to two pages")]
    return []


def fill_ratio(pdf: Pdf) -> float:
    top, bottom = max(pdf.ys[0]), min(pdf.ys[0])
    assert top > bottom, "page 1 has one text line"
    return (top - min(pdf.ys[1])) / (top - bottom)


def page2_fill(ctx: Ctx, p: dict[str, Any], _sev: str) -> list[Finding]:
    if len(ctx.pdf.lines) != 2 or not ctx.pdf.ys[1]:
        return []
    pct = round(fill_ratio(ctx.pdf) * 100)
    if pct < need(p, "major_under_pct"):
        sev = "major"
    elif pct < need(p, "fine_from_pct") and ctx.paid_years >= need(p, "years_min"):
        sev = "nit"
    else:
        return []
    why = "a thin second page reads as failed compression"
    return [
        finding(
            "page2-fill",
            "page",
            sev,
            f"page 2 is {pct}% full",
            why,
            "fill page 2 with strong work or cut to one page",
        )
    ]


def spill(ctx: Ctx, p: dict[str, Any], sev: str) -> list[Finding]:
    """Multi-line bullets whose last line holds a word or two."""
    glyphs = tuple(need(p, "bullet_glyphs"))
    words_max = need(p, "words_max")
    titles = [e.title for e in entries(ctx.resume, common.SECTION_KINDS) if e.title]
    heads = {s.title.lower() for s in ctx.resume.sections}
    bullets: list[list[str]] = []
    for lines in ctx.pdf.lines:
        cur: list[str] | None = None  # a page break ends a bullet
        for ln in lines:
            if ln.startswith(glyphs):
                cur = [ln]
                bullets.append(cur)
            elif cur is not None and ln.lower() not in heads and not ln.startswith(tuple(titles)):
                cur.append(ln)
            else:
                cur = None
    why = "a word or two alone on a line wastes a line and looks unedited"
    return [
        finding(
            "spill",
            "page",
            sev,
            f"{b[0][:60]!r} ends on {b[-1]!r}",
            why,
            "shorten it to one line, or say more to fill the second",
        )
        for b in bullets
        if len(b) > 1 and len(b[-1].split()) <= words_max
    ]


def links(ctx: Ctx, p: dict[str, Any], sev: str) -> list[Finding]:
    out: list[Finding] = []
    text = " ".join(ln for page in ctx.pdf.lines for ln in page).lower()
    for host in need(p, "contact_hosts"):
        if host not in text:
            why = "a technical reader checks the code first; the contact block must link it"
            out.append(
                finding(
                    "links",
                    "header",
                    sev,
                    f"no {host} link in the text",
                    why,
                    f"add the {host} link to the contact block",
                )
            )
    for e in entries(ctx.resume, tuple(need(p, "linked_kinds"))):
        if e.links or any(b.links for b in e.bullets):
            continue
        w = waiver(ctx.resume, "links", e.id)
        why = "a flagship repo, paper or PR is the proof; the reader wants the specific link"
        ev = f"{e.title!r} has no link" + (f" (waived: {w.reason})" if w else "")
        out.append(
            finding(
                "links",
                e.id,
                need(p, "pending_severity") if w else sev,
                ev,
                why,
                "link the repo, paper or PR itself",
            )
        )
    return out


# -- timeline checks


def covered(r: rp.Resume, now: YM) -> set[YM]:
    got: set[YM] = set()
    for e in entries(r, DATED_KINDS):
        if e.dates is None:
            continue
        end = e.dates.end or now
        n = months(e.dates.start, end)
        if n > common.ENTRIES_MAX * 120:
            raise InputError(f"{e.id}: a {n}-month date range")
        got |= {add_months(e.dates.start, k) for k in range(max(n, 0))}
    return got


def gaps(r: rp.Resume, now: YM, window: int) -> list[tuple[YM, int, bool]]:
    """(first blank month, length, trailing to now) for each gap after the first dated month."""
    have = covered(r, now)
    span = [add_months(now, -k) for k in range(window - 1, -1, -1)]
    first = next((i for i, m in enumerate(span) if m in have), None)
    out: list[tuple[YM, int, bool]] = []
    run_start: int | None = None
    for i in range(0 if first is None else first, len(span)):
        if span[i] not in have and run_start is None:
            run_start = i
        if span[i] in have and run_start is not None:
            out.append((span[run_start], i - run_start, False))
            run_start = None
    if run_start is not None:
        out.append((span[run_start], len(span) - run_start, True))
    return out


def gap_severity(n: int, trailing: bool, early: bool, p: dict[str, Any]) -> str:
    if n > need(p, "blocker_over") or (trailing and n > need(p, "trailing_blocker_over")):
        return "blocker"
    if n > need(p, "nit_through"):
        return "major"
    if n > need(p, "none_through"):
        return "nit" if early else "none"
    return "none"


def blank_months(ctx: Ctx, p: dict[str, Any], _sev: str) -> list[Finding]:
    early = ctx.paid_years < need(p, "mid_level_years")
    out: list[Finding] = []
    found = gaps(ctx.resume, ctx.now, need(p, "window_months"))
    for k, (start, n, trailing) in enumerate(found):
        sev = gap_severity(n, trailing, early, p)
        later_quiet = all(m <= need(p, "none_through") for _, m, _ in found[k + 1 :])
        if (
            sev == "major"
            and not trailing
            and n <= need(p, "historical_nit_through")
            and later_quiet
        ):
            sev = "nit"  # an old gap followed by continuous work
        if sev == "none":
            continue
        ev = f"{n} blank months from {start[0]}-{start[1]:02d}" + (" to now" if trailing else "")
        why = "a blank stretch on the timeline is read before any bullet"
        out.append(
            finding(
                "blank-months",
                "timeline",
                sev,
                ev,
                why,
                "add the dated work that covers it, if there is any",
            )
        )
    return out


def short_stints(ctx: Ctx, p: dict[str, Any], sev: str) -> list[Finding]:
    out: list[Finding] = []
    labels = [x.lower() for x in need(p, "labels")]
    for e in entries(ctx.resume, ("experience",)):
        if e.dates is None:
            continue
        n = months(e.dates.start, e.dates.end or ctx.now)
        head = (e.heading or e.title).lower()
        if n < need(p, "months_under") and not any(x in head for x in labels):
            why = "an unlabelled short role reads like a failed probation"
            ev = f"{e.title!r} lasted {n} months with no contract, term or volunteer label"
            out.append(
                finding(
                    "short-stint-label",
                    e.id,
                    sev,
                    ev,
                    why,
                    "label it (contract, internship, volunteer)",
                )
            )
    return out


def order_breaks(r: rp.Resume, kinds: tuple[str, ...]) -> list[tuple[rp.Entry, rp.Entry]]:
    out: list[tuple[rp.Entry, rp.Entry]] = []
    for s in r.sections:
        if s.kind not in kinds:
            continue
        dated = [e for e in s.entries if e.dates is not None]
        for prev, cur in zip(dated, dated[1:]):
            assert prev.dates is not None and cur.dates is not None
            later = cur.dates.start > prev.dates.start
            tie = (
                cur.dates.start == prev.dates.start
                and cur.dates.end is None
                and prev.dates.end is not None
            )
            if later or tie:
                out.append((prev, cur))
    return out


def exp_order(ctx: Ctx, _p: dict[str, Any], sev: str) -> list[Finding]:
    why = "the reader expects the latest employer first"
    return [
        finding(
            "exp-order",
            cur.id,
            sev,
            f"{cur.title!r} should come before {prev.title!r}",
            why,
            "sort by start date, newest first",
        )
        for prev, cur in order_breaks(ctx.resume, ("experience",))
    ]


def order_soft(ctx: Ctx, _p: dict[str, Any], sev: str) -> list[Finding]:
    why = "newest first is the default; relevance may override it for a posting"
    return [
        finding(
            "section-order-soft",
            cur.id,
            sev,
            f"{cur.title!r} starts after {prev.title!r}",
            why,
            "keep it if relevance put it there",
        )
        for prev, cur in order_breaks(ctx.resume, SOFT_ORDER_KINDS)
    ]


# -- skills checks


def words(text: str) -> list[str]:
    """Lowercase word tokens with a trailing s dropped on 4+ letter words ("reviews" = "review")."""
    toks = re.findall(r"[a-z0-9+#]+", nfkc(text).lower())
    return [t[:-1] if len(t) >= 4 and t.endswith("s") else t for t in toks]


def contains(hay: list[str], needle: list[str]) -> bool:
    n = len(needle)
    return n > 0 and any(hay[i : i + n] == needle for i in range(len(hay) - n + 1))


def skills_backed(ctx: Ctx, _p: dict[str, Any], sev: str) -> list[Finding]:
    es = entries(ctx.resume, common.SECTION_KINDS)
    backing = [words(b.text) for e in es for b in e.bullets]  # a bullet backs it, not a tag
    out: list[Finding] = []
    for line in ctx.resume.skills:
        if line.label.strip().lower().startswith("language"):
            continue  # heading tags back languages: tags-in-languages checks them
        for item in line.items:
            parts = item.split(" (", 1)[0].split("/")
            backed = any(contains(bw, words(x)) for x in parts for bw in backing)
            w = waiver(ctx.resume, "skills-backed", item)
            if backed and w:
                why = "a waiver that no longer applies hides the next real problem"
                out.append(
                    finding(
                        "skills-backed",
                        "skills",
                        sev,
                        f"stale waiver: a bullet backs {item!r} now",
                        why,
                        "delete the waiver",
                    )
                )
            elif not backed and not w:
                why = "a skill no bullet shows is a claim the reader can't check"
                out.append(
                    finding(
                        "skills-backed",
                        "skills",
                        sev,
                        f"no bullet backs {item!r}",
                        why,
                        "cut it, back it, or waive it with a reason",
                    )
                )
    return out


def has_lang(text: str, lang: str) -> bool:
    return (
        re.search(r"(?<![A-Za-z0-9+#])" + re.escape(lang) + r"(?![A-Za-z0-9+#])", text) is not None
    )


def tags_in_languages(ctx: Ctx, p: dict[str, Any], sev: str) -> list[Finding]:
    listed = [
        i for line in ctx.resume.skills if "language" in line.label.lower() for i in line.items
    ]
    out: list[Finding] = []
    for e in entries(ctx.resume, common.SECTION_KINDS):
        for lang in need(p, "languages"):
            if not any(has_lang(t, lang) for t in e.tags):
                continue
            on_line = any(has_lang(i, lang) for i in listed)
            w = waiver(ctx.resume, "tags-in-languages", lang)
            if on_line and w:
                out.append(
                    finding(
                        "tags-in-languages",
                        e.id,
                        sev,
                        f"stale waiver: {lang!r} is on the Languages line now",
                        "a stale waiver hides the next problem",
                        "delete the waiver",
                    )
                )
            elif not on_line and not w:
                why = "a reader scanning the Languages line won't find a language the headings use"
                out.append(
                    finding(
                        "tags-in-languages",
                        e.id,
                        sev,
                        f"{e.title!r} tags {lang!r}, which the Languages line doesn't list",
                        why,
                        "add it to the Languages line, or waive it",
                    )
                )
    return out


# -- text checks


def about_form(ctx: Ctx, _p: dict[str, Any], sev: str) -> list[Finding]:
    n = sum(ln.count("∼") for page in ctx.pdf.lines for ln in page)
    why = "one form for approximate values; the math sim glyph reads as a different symbol"
    return (
        [
            finding(
                "about-form",
                "page",
                sev,
                f"{n} math ~ glyphs in the text",
                why,
                "write a plain ~ (\\textasciitilde{})",
            )
        ]
        if n
        else []
    )


def text_order(ctx: Ctx, p: dict[str, Any], sev: str) -> list[Finding]:
    standard = {h.lower() for h in need(p, "headings")}
    flat = [ln.lower() for page in ctx.pdf.lines for ln in page]
    out: list[Finding] = []
    last = -1
    for s in ctx.resume.sections:
        title = s.title.lower()
        if s.kind != "other" and title not in standard:
            why = "screening parsers split a resume by its standard headings"
            out.append(
                finding(
                    "text-order",
                    s.id,
                    sev,
                    f"heading {s.title!r} is not a standard name",
                    why,
                    f"use one of {sorted(need(p, 'headings'))}",
                )
            )
        at = next((i for i, ln in enumerate(flat) if ln == title), None)
        if at is None or at < last:
            why = "the extracted text must read in one column, in order"
            ev = f"heading {s.title!r} " + (
                "is missing from the extracted text"
                if at is None
                else "is out of order in the extracted text"
            )
            out.append(
                finding(
                    "text-order", s.id, sev, ev, why, "fix the layout so the text reads in order"
                )
            )
        last = max(last, at if at is not None else last)
    return out


def body_font(ctx: Ctx, _p: dict[str, Any], sev: str) -> list[Finding]:
    want = ctx.settings.body_font_pt
    if want is None or not ctx.pdf.fonts:
        return []
    got = Counter(ctx.pdf.fonts).most_common(1)[0][0]
    if got >= want - 0.05:
        return []
    why = "a shrunk body font is the first thing a reviewer spots"
    return [
        finding(
            "body-font",
            "page",
            sev,
            f"body text is {got}pt, the template's is {want}pt",
            why,
            "cut words, not the font size",
        )
    ]


def all_bullets(r: rp.Resume) -> list[rp.Bullet]:
    return [b for e in entries(r, common.SECTION_KINDS) for b in e.bullets]


def weak_opener(ctx: Ctx, p: dict[str, Any], sev: str) -> list[Finding]:
    weak = sorted(need(p, "weak"), key=len, reverse=True)
    out: list[Finding] = []
    for b in all_bullets(ctx.resume):
        hit = next((w for w in weak if re.match(re.escape(w) + r"\b", b.text.strip())), None)
        if hit:
            why = "the first word is the outcome slot; a casual verb spends it"
            out.append(
                finding(
                    "weak-opener",
                    b.id,
                    sev,
                    f"opens with {hit!r}: {b.text[:60]!r}",
                    why,
                    "open with the outcome verb (Cut, Shipped, Unblocked, ...)",
                )
            )
    return out


def slop_counts(ctx: Ctx, p: dict[str, Any], sev: str) -> list[Finding]:
    pats = [re.compile(x, re.IGNORECASE) for x in need(p, "patterns")]
    out: list[Finding] = []
    for b in all_bullets(ctx.resume):
        m = next((m for m in (pat.search(b.text) for pat in pats) if m), None)
        if m:
            why = "line, test and checklist counts read as generated filler"
            out.append(
                finding(
                    "slop-counts",
                    b.id,
                    sev,
                    f"{m.group(0)!r} in {b.text[:60]!r}",
                    why,
                    "cut the count; say what the work did",
                )
            )
    return out


CHECKS: dict[str, Check] = {
    "pages": pages,
    "page-length": page_length,
    "page2-fill": page2_fill,
    "spill": spill,
    "links": links,
    "blank-months": blank_months,
    "short-stint-label": short_stints,
    "exp-order": exp_order,
    "section-order-soft": order_soft,
    "skills-backed": skills_backed,
    "tags-in-languages": tags_in_languages,
    "about-form": about_form,
    "text-order": text_order,
    "body-font": body_font,
    "weak-opener": weak_opener,
    "slop-counts": slop_counts,
}


# -- run


def paid_years(r: rp.Resume, facts: list[evidence.Fact], now: YM) -> float:
    paid = {f.entry for f in facts if f.paid and f.status == "confirmed"}
    got: set[YM] = set()
    for e in entries(r, ("experience",)):
        if e.id in paid and e.dates is not None:
            n = months(e.dates.start, e.dates.end or now)
            got |= {add_months(e.dates.start, k) for k in range(max(n, 0))}
    return len(got) / 12


def rule_params(rule: rules.Rule) -> dict[str, Any]:
    """The rule's shipped params, then each flexible-mode ruling's, in ruling order."""
    out = dict(rule.params)
    for ruling in sorted(rule.rulings, key=rules.Ruling.key):
        if ruling.op != "rule":  # a shipped ruling's params are its record, not the check's input
            out.update(ruling.params or {})
    return out


def run_checks(ctx: Ctx, table: dict[str, rules.Rule]) -> list[Finding]:
    found: list[Finding] = []
    for rid, fn in CHECKS.items():
        rule = table.get(rid)
        if rule is None or rule.active() is None:
            continue  # removed in flexible mode, or not in this ledger
        got = [
            f for f in fn(ctx, rule_params(rule), rule.active_severity()) if f.severity != "none"
        ]
        if len(got) > common.FINDINGS_PER_CHECK_MAX:
            raise InputError(
                f"check {rid}: FINDINGS_PER_CHECK_MAX ({common.FINDINGS_PER_CHECK_MAX}) hit"
            )
        print(f"  {rid}: {len(got)}")
        found += got
    return found


def today() -> YM:
    d = datetime.date.today()
    return (d.year, d.month)


def run(resume_json: Path, pdf_path: Path, settings_path: Path, out: Path) -> int:
    local, settings = rules.settings_local(settings_path)
    assert settings is not None, "settings_local returns settings when given a path"
    table = rules.load(local=local)
    r = rp.load_resume(resume_json)
    ev_path = settings_path.parent / settings.evidence
    facts, problems = evidence.load_facts(ev_path) if ev_path.exists() else ([], [])
    if problems:
        raise InputError(f"{ev_path}: {len(problems)} problems; run: evidence.py lint {ev_path}")
    now = today()
    ctx = Ctx(r, read_pdf(pdf_path), settings, paid_years(r, facts, now), now)
    n_bullets = len(all_bullets(r))
    n_items = sum(len(s.items) for s in r.skills)
    print(f"lint: {len(ctx.pdf.lines)} pages, {n_bullets} bullets, {n_items} skills items")
    if n_bullets == 0:
        raise InputError(f"{resume_json}: 0 bullets parsed: nothing to check")
    found = run_checks(ctx, table)
    common.findings_out(out, found, {"paid_years": round(ctx.paid_years, 2)})
    print(f"lint: {len(found)} findings")
    return 1 if found else 0


# -- self-test (each rule: one pass, one fail)

PARAMS: dict[str, dict[str, Any]] = {
    "page-length": {"paid_years_under": 5},
    "page2-fill": {"major_under_pct": 40, "fine_from_pct": 55, "years_min": 5},
    "spill": {"bullet_glyphs": ["•"], "words_max": 2},
    "links": {
        "contact_hosts": ["github.com"],
        "linked_kinds": ["projects", "research"],
        "pending_severity": "nit",
    },
    "blank-months": {
        "window_months": 60,
        "none_through": 3,
        "nit_through": 6,
        "blocker_over": 12,
        "trailing_blocker_over": 9,
        "historical_nit_through": 9,
        "mid_level_years": 5,
    },
    "short-stint-label": {
        "months_under": 6,
        "labels": ["contract", "intern", "co-op", "volunteer", "term", "part-time"],
    },
    "tags-in-languages": {"languages": ["Python", "Rust", "C++"]},
    "text-order": {
        "headings": ["Technical Skills", "Experience", "Research", "Projects", "Education"]
    },
    "weak-opener": {"weak": ["Took", "Got", "Ran", "Worked on"]},
    "slop-counts": {
        "patterns": [
            r"\b\d[\d,.]*k?\s+lines\b",
            r"\b\d+\s+(?:unit\s+|mutation\s+)?tests\b",
            r"CI-verified",
        ]
    },
}
NOW: YM = (2026, 11)


def entry(
    eid: str, start: YM, end: YM | None, bullets: tuple[str, ...] = (), **kw: Any
) -> rp.Entry:
    bs = [rp.Bullet(f"{eid}-b{i + 1}", t, []) for i, t in enumerate(bullets)]
    return rp.Entry(
        eid,
        kw.get("title", eid),
        rp.DateRange(start, end),
        kw.get("tags", []),
        kw.get("links", []),
        kw.get("heading", ""),
        bs,
    )


def ctx_of(
    sections: list[rp.Section], lines: list[list[str]] | None = None, years: float = 2.0, **kw: Any
) -> Ctx:
    r = rp.Resume("t.tex", sections, kw.get("skills", []), kw.get("waivers", []))
    pdf = Pdf(
        "t.pdf", lines or [["Experience"]], kw.get("ys", [[700.0, 100.0]]), kw.get("fonts", [10.0])
    )
    s = common.Settings(
        kw.get("page_rule", 1), "opinionated", "ev.md", "uv", body_font_pt=kw.get("font")
    )
    return Ctx(r, pdf, s, years, NOW)


def sev_of(rid: str, ctx: Ctx, sev: str = "major", **over: Any) -> list[str]:
    params = {**PARAMS.get(rid, {}), **over}
    return [f.severity for f in CHECKS[rid](ctx, params, sev) if f.severity != "none"]


def exp(*es: rp.Entry) -> list[rp.Section]:
    return [rp.Section("experience", "experience", "Experience", list(es))]


def test_pages() -> None:
    one = ctx_of(exp(), [["a"]])
    two = ctx_of(exp(), [["a"], ["b"]])
    assert sev_of("pages", one, "blocker") == [] and sev_of("pages", two, "blocker") == ["blocker"]
    assert sev_of("page-length", two) == ["major"]
    assert sev_of("page-length", ctx_of(exp(), [["a"], ["b"]], years=6)) == []
    assert sev_of("page-length", ctx_of(exp(), [["a"], ["b"], ["c"]], years=9)) == ["major"]


def test_fill() -> None:
    def fill(pct: int, years: float) -> list[str]:
        low = 700.0 - 6.0 * pct  # page 1 spans 700 down to 100
        return sev_of(
            "page2-fill", ctx_of(exp(), [["a"], ["b"]], years, ys=[[700.0, 100.0], [700.0, low]])
        )

    assert (
        fill(39, 6) == ["major"]
        and fill(40, 6) == ["nit"]
        and fill(55, 6) == []
        and fill(60, 6) == []
    )
    assert fill(40, 3) == []  # page-length already charges a 2-page under 5 years


def test_blank() -> None:
    def gap(n: int, trailing: bool = False) -> list[str]:
        a = entry("a", (2022, 1), (2025, 1))
        b_start = add_months((2025, 1), n + 1)
        es = [a] if trailing else [entry("b", b_start, None), a]
        if trailing:
            es[0] = entry("a", (2022, 1), add_months(NOW, -n))
        return sev_of("blank-months", ctx_of(exp(*es)), historical_nit_through=0)  # bands alone

    assert gap(3) == [] and gap(4) == ["nit"] and gap(6) == ["nit"] and gap(7) == ["major"]
    assert gap(12) == ["major"] and gap(13) == ["blocker"]
    old = entry("x", (2022, 1), (2023, 1))
    cont = entry("y", add_months((2023, 1), 9), None)  # 8 blank months, then work to now
    assert sev_of("blank-months", ctx_of(exp(cont, old))) == ["nit"]
    mid = entry("m", add_months((2023, 1), 9), (2024, 6))
    late = entry("z", add_months((2024, 6), 6), None)  # a 5-month gap after it
    assert sev_of("blank-months", ctx_of(exp(late, mid, old))) == ["major", "nit"]
    assert gap(10, trailing=True) == ["blocker"] and gap(2, trailing=True) == []


def test_layout() -> None:
    ok = [
        [
            "Experience",
            "• Cut build time from 9 to 2 minutes by caching the toolchain",
            "and the",
            "deps",
        ]
    ]
    bad = [["Experience", "• Cut build time from 9 to 2 minutes by caching", "the toolchain"]]
    assert sev_of("spill", ctx_of(exp(), ok)) == ["major"] and sev_of(
        "spill", ctx_of(exp(), bad)
    ) == ["major"]
    assert sev_of("spill", ctx_of(exp(), [["Experience", "• one line only"]])) == []
    proj = [rp.Section("projects", "projects", "Projects", [entry("p", (2025, 1), None)])]
    assert sev_of("links", ctx_of(proj, [["github.com/me"]])) == ["major"]
    waived = ctx_of(proj, [["github.com/me"]], waivers=[rp.Waiver("links", "p", "pending")])
    assert sev_of("links", waived) == ["nit"]
    assert sev_of("links", ctx_of(exp(), [["no host"]])) == ["major"]
    secs = [
        rp.Section("experience", "e", "Experience", []),
        rp.Section("projects", "p", "Projects", []),
    ]
    assert sev_of("text-order", ctx_of(secs, [["Experience", "Projects"]])) == []
    assert sev_of("text-order", ctx_of(secs, [["Projects", "Experience"]])) == ["major"]
    assert sev_of(
        "text-order", ctx_of([rp.Section("projects", "p", "Stuff", [])], [["Stuff"]])
    ) == ["major"]
    assert sev_of("about-form", ctx_of(exp(), [["∼59x faster"]]), "nit") == ["nit"]
    assert sev_of("about-form", ctx_of(exp(), [["~59x faster"]]), "nit") == []
    assert sev_of("body-font", ctx_of(exp(), fonts=[9.0, 9.0], font=10.0)) == ["major"]
    assert sev_of("body-font", ctx_of(exp(), fonts=[10.0], font=10.0)) == []


def test_order() -> None:
    old, new = entry("old", (2023, 1), (2024, 1)), entry("new", (2025, 1), None)
    assert sev_of("exp-order", ctx_of(exp(new, old))) == [] and sev_of(
        "exp-order", ctx_of(exp(old, new))
    ) == ["major"]
    done, ongoing = entry("done", (2025, 1), (2025, 9)), entry("on", (2025, 1), None)
    assert sev_of("exp-order", ctx_of(exp(done, ongoing))) == ["major"]
    res = [rp.Section("research", "r", "Research", [old, new])]
    assert sev_of("section-order-soft", ctx_of(res), "nit") == ["nit"]
    short = entry("s", (2026, 6), (2026, 8), title="Acme")
    labelled = entry("l", (2026, 6), (2026, 8), heading="Acme, contract")
    assert sev_of("short-stint-label", ctx_of(exp(short))) == ["major"]
    assert sev_of("short-stint-label", ctx_of(exp(labelled))) == []


def test_skills() -> None:
    e = entry("a", (2025, 1), None, ("Shipped a Rust service with code reviews",), tags=["Python"])
    line = [
        rp.SkillLine("Tools", ["Rust", "Go", "Code review"]),
        rp.SkillLine("Languages", ["Rust", "Kotlin"]),
    ]
    assert sev_of("skills-backed", ctx_of(exp(e), skills=line)) == [
        "major"
    ]  # Go; Kotlin's line is exempt
    stale = ctx_of(
        exp(e),
        skills=line,
        waivers=[rp.Waiver("skills-backed", "Rust", "x"), rp.Waiver("skills-backed", "Go", "y")],
    )
    assert sev_of("skills-backed", stale) == ["major"]  # the Rust waiver is stale
    assert sev_of("tags-in-languages", ctx_of(exp(e), skills=line)) == ["major"]  # Python
    waived = ctx_of(exp(e), skills=line, waivers=[rp.Waiver("tags-in-languages", "Python", "z")])
    assert sev_of("tags-in-languages", waived) == []


def test_bullets() -> None:
    e = entry(
        "a",
        (2025, 1),
        None,
        (
            "Took over the build",
            "Cut CI time 40% by caching",
            "Wrote 57k lines",
            "Added 371 unit tests",
        ),
    )
    assert sev_of("weak-opener", ctx_of(exp(e)), "nit") == ["nit"]
    assert sev_of("slop-counts", ctx_of(exp(e))) == ["major", "major"]


def test_cap() -> None:
    many = tuple(f"Took step {i}" for i in range(common.FINDINGS_PER_CHECK_MAX + 1))
    rule = rules.Rule(
        "weak-opener",
        "sharp",
        "lint.weak_opener",
        "nit",
        False,
        [rules.Ruling("t", 14, True, 1, "test")],
        PARAMS["weak-opener"],
    )
    try:
        run_checks(ctx_of(exp(entry("a", (2025, 1), None, many))), {"weak-opener": rule})
        raise AssertionError("cap not hit")
    except InputError as e:
        assert "hit" in str(e)


TESTS = (
    test_pages,
    test_fill,
    test_blank,
    test_layout,
    test_order,
    test_skills,
    test_bullets,
    test_cap,
)


def self_test() -> int:
    for t in TESTS:
        t()
        print(f"ok {t.__name__}")
    print(f"self-test passed ({len(TESTS)} tests)")
    return 0


def main(argv: list[str]) -> int:
    try:
        if argv == ["--self-test"]:
            return self_test()
        if len(argv) == 6 and argv[2] == "--settings" and argv[4] == "--out":
            return run(Path(argv[0]), Path(argv[1]), Path(argv[3]), Path(argv[5]))
        print(__doc__, file=sys.stderr)
        return 2
    except InputError as e:
        print(f"lint: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
