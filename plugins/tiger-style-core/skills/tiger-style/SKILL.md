---
name: tiger-style
description: TigerBeetle's TigerStyle coding discipline (safety, performance, developer experience) for writing or reviewing code in any language; rules files cover Python, fish, Lua, Rust, Java, Zig, C, C++, Go and TypeScript. Load it when the user says "tiger style" or "tigerstyle", and also when they name one of its rules without the style's name, since those rules are this skill's own, namely assertion density, paired assertions, bounded loops, a limit on everything, NASA Power of Ten (Power of 10), units in names (suffixes like _ms, _bytes), the 70-line function limit, no recursion. Not for animals, CSS styles or generic code review.
---

# TigerStyle

A Claude Code skill that brings TigerBeetle's TigerStyle coding discipline to wherever it's
invoked: any project, any language. Design goals, in priority order: **safety**, then
**performance**, then **developer experience**. When a tradeoff is forced, the
higher-priority goal wins.

The principles are language-neutral. Language specifics live in one rules file per
language, each its own `tiger-style-<language>` skill. Python projects and fish shell scripts
are where it has been used so far, so their examples appear throughout; they are
examples, not the scope.

Fused from two sources:
- The `tiger-style` skill from fotoetienne/agent-essentials (MIT) — advisory,
  invoke-on-demand, two modes. This is the backbone.
- The Python coding contract from robertguss/programming_tiger_style — its
  Python-specific rules, with `mypy` swapped for `pyright`. Its mandatory
  test-first TDD cycle is deliberately NOT adopted (see Testing discipline).

Canonical source: https://github.com/tigerbeetle/tigerbeetle/blob/main/docs/TIGER_STYLE.md

When invoked, follow the protocol below. Everything from here through the `---`
divider is the protocol to execute. The section after the divider is documentation
for humans maintaining this skill.

---

## First invocation: load the references

The TL;DR below is a shortlist, not the full guide. On first invocation in a session,
read the reference files you'll need before responding:

- **Always read** `references/principles.md` — the full distilled rules with rationale.
- **Load the language skill for the target's language** if it is installed:
  `tiger-style-python`, `-typescript`, `-rust`, `-go`, `-c`, `-cpp`, `-java`, `-lua` or `-fish`.
  Each is its own plugin, so only the languages the user picked are there.
  - Zig → `references/upstream/TIGER_STYLE.md`, TigerBeetle's original guide,
    which is written for Zig. Use it as the Zig rules directly.
  - No language skill for the language → apply `principles.md` directly, translate each
    rule into that language's idioms yourself, and use the project's own toolchain (see
    Toolchain gates). Say in your reply that no rules are loaded for the language.
- **Find the project's conventions** before applying the rules: its formatter and
  linter config, its test runner, its line length, and its existing comment style.
  Where the project has a settled convention, it wins over a TigerStyle default;
  flag the difference instead of fighting it.
- **If reviewing** (the user said review / audit / check, or pointed at a diff or
  file), also read `references/review-checklist.md`.

Exception: if the user asks a single narrow question the TL;DR answers directly
(e.g. "what's the function line limit?"), answer from the TL;DR without loading
references. When in doubt, read. In later turns of the same session the references
are already in context — don't re-read unless you need a section you skipped.

## Two modes

### Reference mode — authoring code (default)

The user is writing new code or asks "how should I write this in TigerStyle?". Apply
the rules from `principles.md` and the language's rules file proactively as you write. Cite
the specific rule when the reason is non-obvious.

### Review mode — auditing changes

The user says "tiger style review" or asks you to review changes. Walk
`references/review-checklist.md` against `git diff` (or the files in scope) and report
violations with `file:line` citations grouped by severity, using the output format at
the end of this file.

**Default to reference mode.** Switch to review mode when the user says "review",
"audit", "check", or points at a diff or PR.

## When to apply — scale to the stakes

The full discipline costs real effort, so match it to what a bug would cost.

| Full TigerStyle | Lighter TigerStyle |
|---|---|
| A bug could corrupt money, stored data, isolation, or system state | Small tools, scripts, glue, UI |
| Determinism and reasoning locally about the code matter more than convenience | A plain typed error covers every failure well |
| The design can still be simplified before it's built | It's a small patch inside an established pattern |

Lighter still means: asserts on real invariants, bounded loops, errors handled, the
project's gates passing, and tests for new behavior. What it relaxes: allocation
rules, dispatch and pointer restrictions, assertion-density targets, and building a
full test harness where a few direct checks would do. Say which level you applied and
why. When the user asks for full TigerStyle on low-stakes code (e.g. to build a role
model), do it, and say that it's a deliberate choice.

## Proof

Every piece of TigerStyle work comes with its proof, stated in the reply or review:
name each bound (the constant or the limit on each loop and buffer), point at the
paired assertions for each invariant, and name the smallest test that fails if that
invariant is broken — the mutation gate below is how you know that test really
fails.

## Testing discipline (this skill's rule — read carefully)

This applies in every project and overrides any TDD guidance in the source material:

- **No feature is "done" — no PR opened, no change handed over as finished — until its
  new behavior has useful tests.** Testing is a hard gate at that boundary. Every new
  feature ships tested. If the project has no test setup (e.g. a single shell script),
  the tests live with the code; `tiger-style-fish` shows the self-test pattern.
- **Do NOT write tests first.** Do not author a failing test and build toward it. A
  test written against a half-defined feature churns and breaks every time the
  feature's shape evolves while it's being built — that brittleness is itself debt.
- **Test after the contract is stable.** Finish the feature's behavior, then write
  meaningful tests that verify real behavior and edge cases (valid, invalid,
  boundary, regression). A useful test on a completed feature — not a scaffolding
  artifact.
- This serves "zero technical debt" (an untested feature is debt) while rejecting
  premature tests (which are also debt). It is test-after-well, not test-first.

Do not auto-add tests mid-feature. Add them once the feature is complete, before it
is considered done.

- **Mutation-test new logic before pushing (hard pre-push gate).** "Tests pass" proves
  nothing on its own — a test only has value if it FAILS when the code it guards is
  broken. Once the code looks good to go and the suite is green, before pushing, verify
  the new/changed tests actually bite: inject a deliberate wrong value or flipped
  condition into each non-trivial branch the change introduced (a boundary `>=`→`>`, a
  constant, a threshold, a chosen enum/string, a branch guard) and confirm a test turns
  red; then revert. A mutation that survives (all tests still green) is an untested
  line masquerading as covered — write or strengthen a test until it is killed. Prefer
  the language's systematic tool when scope warrants it (Python:
  `uv run --with mutmut mutmut run`; fish: the built-in mutation runner pattern in
  `tiger-style-fish`) and drive surviving mutants to zero; a quick manual mutation pass on
  each new branch is the minimum. This gate runs AFTER the suite is green and BEFORE the
  push — line/branch coverage is not a substitute for it.

- **Spec worked-examples are mandatory test cases.** When the governing spec states a
  concrete "input → expected output" (a spec line like "given A, the result is B"), that mapping
  must become a test asserting exactly it, as part of the feature's "done" tests. This
  is the mechanical half of the spec-conformance gate: prose criteria are checked by judgment
  in review mode, but any example crisp enough to compute must be pinned by a test so
  it cannot drift out of conformance unnoticed later.

## Core principles (TL;DR)

1. **Zero technical debt.** Do it right the first time; code is cheaper to change while
   hot. An untested completed feature is debt — so is a premature test.
2. **Conform to the problem statement.** Code that is beautifully built but violates a
   stated done-criterion is still a failed change. In review mode this is the first,
   non-skippable pass (P0 Spec conformance): every criterion defaults to UNMET until a
   `file:line` or output line proves it, and every worked example in the spec is run
   against the code and pinned by a test. Shipping a PR that misses its own criteria is
   the failure this gate exists to prevent.
3. **Assertions are force multipliers.** Average at least two per function. Assert
   arguments, return values, pre/post-conditions, and invariants — positive *and*
   negative space. Assertions are for the program's own invariants; bad input from
   outside gets an ordinary handled error (Python: `assert` vs typed exceptions; fish:
   an assert helper returning 2 vs an error message returning 1). The language's rules
   file says how. In tiny functions (e.g. shell helpers) the density average matters
   less than this: every invariant has an assert, and every assert is shown to fire.
4. **Put a limit on everything.** Every loop, queue, buffer, retry, batch, and payload
   has a fixed upper bound. External I/O has an explicit timeout. Fail fast.
5. **Bound resource growth.** No unbounded in-process collections; use pre-sized or
   capped structures. (The translation of "no dynamic allocation after init" for
   languages that allocate dynamically.)
6. **Function length: target 70 lines, hard limit 140** (excluding decorators and
   docstring). Prefer ≤70; up to 140 is allowed in service of better modularization
   and separation of concerns. Push `if`s up and `for`s down — parent owns control
   flow, leaves are pure.
7. **Hard 100-column line limit.** No exceptions.
8. **Explicit, simple control flow.** No recursion where iteration works; bound it when
   you must recurse. Split compound conditions into nested `if/else`.
9. **Handle every error.** Nothing swallowed, every failure status checked where it
   happens, context preserved (Python: no bare `except:`; shells: no errexit to lean
   on, so `or return` at each step).
10. **Name things well.** Units last, descending significance: `latency_ms_max`, not
   `max_latency_ms`. `snake_case`. No abbreviations. Match lengths of related names
   (`source`/`target`, not `src`/`dst`).
11. **Say why.** Comments explain rationale, not what the code does. Tests get a
    top-of-test comment stating goal and methodology. Where the project's established
    comment style asks for more (e.g. a fish project that explains every flag),
    the project's style wins.
12. **Back-of-envelope first.** Reason about performance at design time across the four
    resources (network, disk, memory, CPU) × two dimensions (bandwidth, latency).
    Optimize the slowest first, weighted by frequency.
13. **Batch to amortize.** Run at your own pace; don't context-switch per external
    event.
14. **Shrink scope.** Declare variables at the smallest possible scope; compute close
    to use (avoid place-of-check to place-of-use gaps).
15. **Debug bottom-up; plan with an unbiased sub-agent.** On an unclear problem, don't
    fire hypotheses at the whole system — decompose it and earn certainty about each
    small part with a direct test (read the value on disk, confirm the daemon reloaded,
    prove the keystroke reaches the layer). The verified parts are a skill list; the
    boss is the problem; composing skills into a plan is best handed to a sub-agent
    briefed on the confirmed capabilities, not on your running theory. A plan built
    from confirmed primitives is checkable; one built from assumptions is a guess.
16. **Keep the checker honest.** Top types only at the input boundary. Never launder type
    evidence (widen then cast, cast chains). Decode untrusted input once, then branch on
    the domain value. Every checker bypass states its invariant beside it. Detail:
    `references/principles.md` "Types and boundaries".

Full detail: `references/principles.md`. Language specifics: the
`tiger-style-<language>` skill.

## Toolchain gates

TigerStyle wants zero warnings at the strictest setting, from day one. Whatever the
project, that means four kinds of check pass clean before a change is done: formatter,
linter, type or syntax checker, tests. Use the project's own tools and config; if it
has none for one of the four, say so rather than inventing a setup. Examples:

- **Python:** `uv run ruff format --check .`, `uv run ruff check .`,
  `uv run pyright`, `uv run pytest -q`. (The source contract says `mypy`; the Python rules use
  `pyright`.)
- **fish:** `fish_indent --check`, `fish --no-execute`, and the script's own
  `--test` (see `tiger-style-fish`).

## Review-mode output format

```
## TigerStyle Review: <scope>

### P0 — Spec conformance  (MANDATORY — always present, always first)
Governing spec: <ticket / PRD § / design doc / the user's requests in this
conversation, quoted / or "NONE FOUND — asked author">

| # | Criterion (verbatim) | Status | Evidence (file:line or output) |
|---|----------------------|--------|--------------------------------|
| 1 | <criterion, verbatim> | UNMET | <file:line: what the code does instead> |
| 2 | worked example: <"given A, the result is B", verbatim> | BREAK | <file:line: what the code produces for A, against B> |
| 3 | <criterion, verbatim> | MET | <file:line or output line that satisfies it> |

Verdict: <N MET / M UNMET / K BREAK>. Any UNMET or BREAK blocks merge.
(Default every criterion to UNMET until evidence is cited. Never omit a criterion.)

### P0 — PR-comment conformance  (present whenever a PR is active; next to Spec)
PR: <#n / url, or "no active PR for this branch">

| # | Review comment (verbatim/summary) | Disposition | Evidence (file:line / commit / reason) |
|---|-----------------------------------|-------------|----------------------------------------|
| 1 | <review comment, verbatim or summary> | FIXED | <file:line>; <commit> |
| 2 | <review comment, verbatim or summary> | DECLINED | <reason> |

Verdict: every comment has a disposition. Any UNADDRESSED blocks merge. A single
consolidated reply must post all dispositions and mark the ticket Resolved.

### P0 — Safety
- path/to/file.py:L42 — Unbounded loop over `items` (no fixed upper bound). Rule: "put a limit on everything". Fix: <specific suggestion>.

### P1 — Assertions / function shape
- path/to/file.py:L15 — `process()` has no precondition assertions on its 3 arguments. Rule: "assert all function arguments".
- path/to/file.py:L108 — Function is 180 lines. Rule: "target 70, hard limit 140". Suggest extracting <helper>.

### P2 — Naming / scope / comments
- path/to/file.py:L7 — `max_latency_ms` should be `latency_ms_max` (units last, descending significance).

### P3 — Formatting
- path/to/file.py:L80 — Line exceeds 100 columns.

### Compliant
- <brief note on what's already TigerStyle-compliant>
```

The Spec-conformance table is never optional and never empty. When the work was asked
for in conversation rather than a ticket or doc, the user's requests are the spec:
quote each one as a criterion. Only if there is no written spec and no such request,
say so and ask the author rather than omitting it. A review that skips it is
incomplete, not a pass. Cite the specific rule for each code-shape finding. Omit any
severity section (P0 Safety through P3) with no findings. Don't pad with non-violations.

## When NOT to use

- The user asks about general code quality without invoking TigerStyle → review
  normally; don't force TigerStyle rules.
- A rule conflicts with a framework or language idiom (e.g. Typer's callback/decorator
  patterns in a Python CLI, which require abstractions and defaults TigerStyle
  discourages) — flag the tradeoff, don't demand a rewrite that fights the framework.
- Tests whose descriptive names or setup exceed limits — the strict limits target
  production code.

---

## Maintainer notes (not part of the protocol)

This skill applies wherever it's invoked. It loads when the user says "tiger style" or
names one of its rules (see the description above), not for generic code review.

Adding a language: write a `tiger-style-<language>` skill, verify every claim on the
real toolchain, and add it to the language list under "First invocation". Write each
rule as a **rule contract**, as `tiger-style-rust` does:
- **Default** — the pattern to write.
- **Banned** — the pattern not to write.
- **Why** — the reason, so edge cases can be judged.
- **Exception** — when breaking it is allowed, and what must accompany that.
- **Proof** — what a reviewer looks at to confirm it's followed.
- **Enforced by** — the lint, compiler flag or test that catches a violation, or
  "review" when no tool can. Say what the tool misses (a check that sees only the top
  level of a type, a suppression rule that wants a code but no reason).
A rule that names no proof and no enforcement can't be applied the same way twice.
(`tiger-style-fish` predates this format; convert it when it's next edited.)

The "When to apply", "Proof" and rule-contract ideas were adapted, in our own words,
from the "TigerStyle (Dedalus)" page of Kevin Liu's wiki
(https://wiki.kevinliu.studio/wiki/style/tiger-style), read 2026-10-19.

Provenance and licensing:
- Backbone + structure: fotoetienne/agent-essentials `tiger-style` skill (MIT).
- Python rules: robertguss/programming_tiger_style Python coding contract.
- Both derive from TigerBeetle's TIGER_STYLE.md.
- fish rules: written in-session, checked against fish 4.9.3 and the fish docs.
- Lua rules: each behaviour checked on Neovim v0.13.0-dev-1952, stylua 2.6.0 and
  lua-language-server 3.19.2.
- Types-and-boundaries principles: ideas from dmmulroy/anti-slop (MIT, commit
  13909c9e07590f5a85d93c62712259fe14a883f7, read 2026-11-14), restated in our own words;
  no code copied here. Not carried: its whitespace rule (the project formatter owns
  that), its ban on the word "shape", its Effect-TS rules, its JS-mechanics rules. Where
  it conflicts with tiger-style, tiger-style wins.

Deliberate deviations from the sources:
- `mypy` → `pyright` in the Python rules, to match the Python project toolchain.
- The contract's mandatory test-first Red→Green→Refactor cycle is NOT adopted. This
  skill's rule is test-after, before the change counts as done (see Testing
  discipline). If a future project wants strict TDD, that is a separate decision,
  not this skill's default.
- Zig-only rules from the source are translated per language or dropped (see the
  rules files).
- Invoke with `/tiger-style` or by asking for tiger style.
