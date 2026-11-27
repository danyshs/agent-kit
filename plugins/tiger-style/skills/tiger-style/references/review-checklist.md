# TigerStyle Review Checklist

Use in **review mode**. Walk this list against the target (a diff, a PR, a file, a
function), in whatever project and language it's invoked. Language-specific items are
labelled; translate them using the language's rules file, and skip one that has no
equivalent rather than forcing it. Examples come from a Python project. Cite
`file:line` and the specific TigerStyle rule for each finding. Note what's already
compliant — avoid an only-negatives report.

## Process

1. Identify scope. `git diff main...HEAD`, specific files, or the working tree.
2. **Locate the governing spec and run the Spec-conformance pass FIRST** (the P0
   section below). Find the ticket / PRD / design doc that defines what this change is
   supposed to do. When the work was asked for in conversation instead, the user's
   requests are the spec: quote each one. If there is neither, say so and ask — do not
   proceed as if there are no criteria to meet.
3. Walk the rest of the checklist, grouping findings by severity.
4. Output in the format at the end of SKILL.md. The Spec-conformance table is
   mandatory and comes first — a review without it is incomplete, not passing.
5. Also run the project's toolchain gates (formatter, linter, type/syntax checker,
   tests — see "Toolchain gates" in SKILL.md) and report their status. E.g. Python:
   `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`,
   `uv run pytest -q`; fish: `fish_indent --check`, `fish --no-execute`, `--test`.

## P0 — Spec conformance (fix before merge; this pass runs FIRST)

Why this is P0 and first: every other section asks "is this code well-built?" This one
asks "does this well-built code do what the problem statement said?" A clean, bounded,
fully-asserted function that violates a stated done-criterion is still a failed change.
Shipping a PR that misses its own problem-statement criteria is the specific failure
this pass exists to prevent — treat an UNMET criterion as a merge blocker on par with a
safety defect.

**Default is UNMET.** A criterion is MET only when you can cite the exact line of code
or the exact output that satisfies it. "Looks handled" is UNMET. "Probably fine" is
UNMET. Silence on a criterion is UNMET — never skip one.

- [ ] **Build the criteria list.** Enumerate EVERY done-criterion, acceptance clause,
      requirement, and edge-case contract in the governing spec (ticket, PRD §, design
      doc). One row per criterion. Quote the criterion verbatim so the match is
      checkable, not paraphrased.
- [ ] **Prove each one.** For each criterion, cite `file:line` of the code or the
      literal output line that satisfies it — or mark it **UNMET** with what's missing.
      Partial satisfaction is UNMET with a note.
- [ ] **Execute every worked example.** For each concrete example the spec gives,
      compute/run what the code actually produces and show it next to the spec's
      expected value. A
      mismatch is a P0 BREAK, even if every other section is clean. (These examples must
      also exist as tests — see P1 Testing.)
- [ ] **Interrogate the measurement behind a numeric criterion, not just the figure.**
      When a done-criterion is a count, rate, percentage, or threshold ("≥95%
      schema-valid", "p99 < 50 ms", "20/20 pass"), a printed number that clears the bar
      does NOT satisfy it until the measurement that produced it is sound. The letter
      can be met while the substance is not. Check, specifically:
      - **Effective vs nominal sample size.** Count the *distinct, independent* trials,
        not the loop count. A harness that cycles a fixed set of K inputs M times
        reports N = K×M calls but only K independent measurements — especially under a
        deterministic setting (`temperature: 0`, fixed seed) where repeats replay the
        first result. "20 calls" over 10 fixed prompts at temperature 0 is n≈10, not 20.
      - **Does the result support the stated bar?** A small clean run bounds the true
        rate far below intuition. 10/10 successes is only ~69% at a 95% lower
        confidence bound (Clopper-Pearson) — it cannot substantiate a "≥95%" claim; ~60
        clean independent trials would. State the bound, not just the point estimate.
      - **Confounds that inflate the number.** Fixed/cherry-picked inputs, retries or
        fallbacks masking first-attempt failures, a denominator that excludes the hard
        cases, warm-cache effects. Name any that apply.
      Mark the criterion UNMET (measurement too thin to support the bar) even when the
      raw figure clears it, and say what a sound measurement would take. This is the
      spec's own "are the numbers believable?" question — ask it mechanically.
- [ ] **Reconcile internal spec contradictions.** If two parts of the spec disagree
      (e.g. a worked example implies different thresholds than the prose), flag it as
      UNRESOLVED and name the two locations — do not silently pick one. Resolution is a
      decision for the author, not the reviewer.
- [ ] **Challenge the spec itself — criteria are not infallible, but verify before
      doubting.** A criterion can be wrong: an internally inconsistent number (when the
      spec's own worked example disagrees with the spec's own rule, the code following
      the rule is not the bug; flag the example), a stale
      external fact, or an awkwardly-worded requirement. But do NOT cry "spec is wrong" to excuse a
      code bug: before doubting the spec, independently verify the fact (search, run,
      check the real source). A name, version, or key that looks "off" is far more
      often a code typo than a spec error — confirm which it is. (Concretely: code
      reading `XDG_CACHE_DIR` while the spec says `XDG_CACHE_HOME` was the CODE's
      error, not the spec's, once the XDG Base Directory spec was checked.) When the
      spec is genuinely wrong, mark the criterion **SPEC-SUSPECT** with the conflict and
      the verification spelled out, and surface it for an author decision — never
      silently conform the code to a mistake, and never silently "fix" code to match a
      bad spec. Conforming to a wrong criterion is as much a failure as missing a right
      one.
- [ ] **Account for every open question in the design doc.** If the change has a
      dry-run / design doc with "to audit", "intent mismatch", "confirm", or "TODO"
      items, each must be resolved to a decision (with rationale) or an explicit tracked
      follow-up BEFORE merge. An unresolved audit question that ships is the exact creep
      this gate exists to stop — mark it UNMET.
- [ ] **Selection-policy sanity (logic, not style).** Where the code picks a
      max/min/first across resources or options (local disk vs network volume, free vs
      total, first-match priority), verify the policy matches physical/spec reality.
      First-match ≠ max. Ask: does a smaller pool wrongly shadow a larger usable one?
      Does one branch's unit/semantics (free) silently mix with another's (total)?

## P0 — PR-comment conformance (if a PR is active; runs with Spec conformance, FIRST)

Why this is P0: an unaddressed reviewer comment is an open loop that silently ships.
The failure this prevents is a PR merging with reviewer feedback neither fixed, nor
declined-with-reason, nor consciously deferred. Treat an unaddressed comment exactly
like an UNMET spec criterion — a merge blocker. We should never be in a situation where
a review comment has no disposition.

**Default is UNADDRESSED.** A comment is addressed only when you can cite the fix, the
drift that already satisfies it, or an explicit decline/defer with a reason. "Looks
handled" is UNADDRESSED. Silence on a comment is UNADDRESSED — never skip one.

- [ ] **Pull the live comment list.** If a PR is open for this branch, fetch every
      review comment (inline and top-level) from the host, e.g.
      `gh pr view <n> --json reviews` and
      `gh api repos/<owner>/<repo>/pulls/<n>/comments`. One row per comment. Include
      comments already answered — a prior reply does not exempt it from re-checking
      that the code still matches the reply.
- [ ] **Give every comment a disposition** against the CURRENT code, one of:
      - **FIXED** — cite `file:line` (and commit) of the change that resolves it.
      - **RESOLVED-BY-DRIFT** — the code already satisfies it for a reason other than a
        targeted fix; cite the `file:line` that does so. (A reviewer asking for X when
        X already exists.)
      - **DECLINED** — not applicable; state the reason and cite where that reasoning
        lives in the code/comment (e.g. a documenting comment).
      - **UNADDRESSED** — none of the above. This blocks.
      Build this as a table exactly like the Spec-conformance table (comment verbatim
      or summarized | disposition | evidence).
- [ ] **Act on the dispositions.** FIXED items that are not yet actually fixed get
      fixed (then tested, per the Testing gate). RESOLVED-BY-DRIFT and DECLINED get a
      reply. Nothing is left UNADDRESSED.
- [ ] **Post one consolidated reply and resolve the ticket.** By the end there must be
      a PR comment that responds to every review comment with its disposition, and the
      ticket is marked **Resolved**. Do not mark Resolved while any comment is
      UNADDRESSED.

## P0 — Safety violations (fix before merge)

- [ ] **Unbounded loops / queues / buffers.** Every iteration and growth has a fixed
      upper bound. An intentionally non-terminating loop asserts that intent.
- [ ] **A bound that does no work (ineffective repetition).** Separate from "is it
      bounded?": for every loop/retry/backoff/cache/batch that repeats, ask whether a
      later pass can produce a *different* outcome than the first. Something must feed
      forward between iterations — new input, the previous failure's error/output,
      a changed parameter, or backoff/elapsed-time state. A correctly-bounded retry
      that resends byte-identical input to a deterministic operation (same request,
      same seed, `temperature: 0`) is a defect: it spends the whole budget and can only
      ever reproduce the first result. Trace what actually varies across iterations; if
      nothing does, flag it — either feed something forward or cut the budget to one
      attempt with a stated reason. (This is the "a bound must do work" principle; a
      green test proving the loop *terminates* does not prove it *accomplishes*
      anything.)
- [ ] **Unbounded in-process growth.** Collections that grow without a cap in a
      long-lived or repeated path.
- [ ] **External I/O without a timeout.** Subprocesses, HTTP calls, file/network
      ops must pass an explicit timeout (Python: `subprocess.run(timeout=…)`). Where
      the language has none built in (fish), use the rules file's fallback or record
      why the risk is accepted.
- [ ] **Unhandled errors.** Bare `except:`, `except Exception: pass`, swallowed
      exceptions, ignored return values that signal failure.
- [ ] **Stated error contract not exhaustively honored (escape-analysis).** When code
      declares what it raises — a docstring saying "returns bytes or raises
      `FooError`", a typed-exception boundary, a "maps the X failure family to Y"
      claim — do not eyeball that the `except` clause "looks right." Enumerate every
      exception type the operations *inside* the boundary can actually raise and check
      each is either mapped to the contract's type or deliberately allowed through.
      Two failure shapes to hunt specifically, because both pass a glance:
      - **Catch too NARROW — a type escapes.** An exception that is NOT a subclass of
        anything in the `except` tuple leaks out raw and breaks the contract. Verify
        the hierarchy, don't assume: e.g. `http.client.IncompleteRead` is a subclass of
        `HTTPException`, **not** `OSError`, so `except (URLError, OSError)` never catches
        it. Confirm with `python -c "print(E.__mro__)"` before trusting a tuple.
      - **Catch too BROAD — information is silently swallowed.** A specific exception
        that is a *subclass* of a broader one in the tuple gets handled by the generic
        branch, discarding its payload. The canonical case: `urllib.error.HTTPError`
        IS-A `URLError`, so `except URLError` swallows the HTTP body — the user sees
        `HTTP Error 404` instead of the server's actual message. The specific subclass
        must be caught *before* its parent to read what it carries.
      For any `except (A, B, C)` guarding a stated contract, list the real raisable
      types, map each to a branch, and flag any that escape or are swallowed. Add a
      test per escaping/swallowed type.
- [ ] **Recursion where iteration is correct**, or unbounded recursion depth.
- [ ] **An assertion used to validate untrusted input** — bad input from outside
      needs an ordinary handled error, not an assert (Python: `assert` is stripped
      under `-O`, use a typed exception; fish: error message + `return 1`, not the
      assert helper's `return 2`).
- [ ] **`eval` / `exec` / untrusted dynamic import** in production code.
- [ ] **Compound boolean conditions hiding an unhandled case.** Split into nested
      `if/else` and check both branches.

## P1 — Assertions & correctness

- [ ] **Function arguments unvalidated.** Precondition assertions on non-trivial args
      (bounds, invariants the caller must uphold).
- [ ] **Return values / post-conditions unasserted**, especially at module boundaries.
- [ ] **Invariants not asserted** (loop invariants, state transitions, "sorted here").
- [ ] **Assertion density < 2/function average** across the diff.
- [ ] **Paired assertions missing** — a property asserted on only one of two paths.
- [ ] **Assertions that can hide each other** — two asserts that fire on the same bad
      state, with tests that don't check which message fired. Each assert needs a
      distinct message and a test that triggers it and checks that message.
- [ ] **Negative space unasserted** — only the happy path is checked.
- [ ] **Compound `assert a and b`** that should be split.
- [ ] **Mutable default argument** (Python: `def f(x=[])` / `={}`).
- [ ] **Safety-critical behavior relying on implicit defaults** instead of explicit
      keyword arguments at the call site.

## P1 — Function shape

- [ ] **Functions > 140 lines** (excluding decorators/docstring), or > 70 without a
      modularization/separation-of-concerns reason. Centralize control flow in the
      parent, extract pure leaves.
- [ ] **Too many parameters** / not inverse-hourglass shaped.
- [ ] **Leaf functions branching** — push `if`s up; leaves should be pure.
- [ ] **Control flow and state scattered across helpers** rather than owned by a parent.

## P1 — Types & errors (Python; for other languages, use the rules file's equivalent)

- [ ] **Missing type hints** on public functions, methods, module-level constants.
- [ ] **Untrusted input not validated at the boundary.**
- [ ] **Library/engine code calling `sys.exit` or printing-and-continuing** instead of
      raising a typed exception for the caller to handle.
- [ ] **Lost exception context** — `raise` without `from` where a cause exists.

## P1 — Boundary types and bypasses (every typed language; cite file:line)

- [ ] **Top type in an internal signature** (`Any`, `unknown`, `object`, `interface{}`,
      `dyn Any`, `void *`) outside a boundary parser.
- [ ] **Type evidence laundered:** widen-then-cast or a cast chain.
- [ ] **Checker bypass with no stated invariant** (cast, `!`, `type: ignore`, `allow`,
      `diagnostic disable`, `NOLINT`).
- [ ] **Representation probing past the boundary** (`isinstance`/`typeof` on the raw value of
      data that was already decoded; a `match` or `isinstance` on the domain type is fine).

Per-language checkers for the four findings above (cite the specific tool in the
finding; "review only" means no tool catches it, so flag it by reading the code):

- [ ] **TypeScript** — top type: `@typescript-eslint/no-explicit-any` (`any`;
      `unknown`/`object` are review only). Laundering: `no-unsafe-type-assertion`
      (a single narrowing cast; chains through `unknown`/`any` are review only).
      Bypass without a reason: `ban-ts-comment` on `@ts-ignore`/`@ts-expect-error`.
      Non-exhaustive dispatch: `switch-exhaustiveness-check` (switch only, not an
      `if`/`else` chain).
- [ ] **Go** — top type (`any`/`interface{}` in a signature) and cast-chain
      laundering: review only (`forbidigo` probed and cannot see a type in a
      signature; no linter targets the chain shape). Bypass without a reason:
      `nolintlint` (`require-specific` + `require-explanation`) on `//nolint`.
      Non-exhaustive dispatch: `exhaustive` (typed-const enums), `gochecksumtype`
      (sealed sum types), both with `default-signifies-exhaustive: false`.
- [ ] **C** — top type (general `void *` laundering): review only; `-Wcast-qual`
      covers only the narrower const-qualifier-loss case. Bypass without a reason
      (`NOLINT`): review only, no clang-tidy check requires one. Non-exhaustive
      dispatch: `-Wswitch-enum` (plain `-Wswitch` stays silent once a `default:`
      exists — that gap is the anti-slop pattern).
- [ ] **C++** — top type / laundering: the four named casts via
      `cppcoreguidelines-pro-type-*` (`cstyle-cast`, `reinterpret-cast`,
      `const-cast`, `static-cast-downcast`) plus `bugprone-casting-through-void`
      (void* laundering — this check is C++-only, silent on the identical `.c`
      pattern). Bypass without a reason (`NOLINT`): review only, same as C.
      Non-exhaustive dispatch: `-Wswitch-enum`, same gap as C.

## P2 — Naming

- [ ] **Abbreviations** (`src`/`dst`/`cfg`/`msg`) where full names are clearer.
- [ ] **Units leading instead of trailing** — `max_latency_ms` → `latency_ms_max`.
- [ ] **Mismatched character lengths** on related vars that would line up in math.
- [ ] **Overloaded terms** — one word meaning two things.
- [ ] **Participles where a noun reads better** in docs/conversation.
- [ ] **Single-use helper not prefixed** with its caller's name.
- [ ] **Callbacks not last** in the parameter list.
- [ ] **Two same-typed positional args** that could be swapped — use keyword args.

## P2 — Scope & aliasing

- [ ] **Variables declared too early / too broadly scoped.**
- [ ] **Dead variables** left after last use.
- [ ] **Aliased duplicates** of the same value — drift risk.
- [ ] **Check far from use (POCPOU).**

## P2 — Comments & commits

- [ ] **Comments that restate the code** — replace with a "why" or delete.
- [ ] **Missing rationale** on non-obvious decisions (thresholds, orderings).
- [ ] **Non-trivial test without a top-of-test "goal & methodology" comment.**
- [ ] **Commit messages that describe what, not why** (flag, don't block on style).

## P2 — Testing (this skill's rule, every project)

- [ ] **New feature lacks useful tests.** No feature is done / no PR opens until its new
      behavior is tested. This is a hard gate.
- [ ] **New tests not mutation-checked before push.** "Green" is not evidence a test
      bites. For each non-trivial branch the change adds, a deliberate mutation (flipped
      boundary, altered constant/threshold, changed branch guard or chosen string) must
      make a test fail; a surviving mutant is an untested line. Confirm manually per new
      branch at minimum, or systematically with the language's tool when scope warrants
      (Python: `mutmut`; fish: the mutation runner in `fish-rules.md`). Runs after
      the suite is green and before the push. (See Testing discipline in SKILL.md.)
      A pure double-check assertion that only fires when other code is already broken
      can't be killed from outside; leave it off the mutation list and say why.
- [ ] **Spec worked-examples are not tests.** Every concrete example in the governing
      spec (each "input → expected output" the PRD/ticket states) must exist as a test
      case asserting exactly that mapping. This is mandatory, not optional: it is the
      mechanical half of the P0 conformance pass, and the only thing that keeps a
      spec example from silently drifting out of conformance later. A spec example with
      no corresponding test is a P1 gap even if other tests exist.
- [ ] **Tests that look premature/scaffolding-shaped** — asserting against a feature
      that's clearly still changing, rather than verifying completed behavior and edge
      cases. (Flag; the rule is test-after on a stable contract.)
- [ ] **Edge cases uncovered** — valid, invalid, boundary, regression.

## P3 — Formatting & mechanics

- [ ] **Lines > 100 columns** (or the project's configured line length, e.g. a Python
      project's ruff setting — follow the project config and flag any mismatch).
- [ ] **Formatter not run** — the project's formatter in check mode (Python:
      `uv run ruff format --check .`; fish: `fish_indent --check`).
- [ ] **Linter / checker warnings** — zero-warning policy (Python: `ruff check`,
      `pyright`; fish: `fish --no-execute`).

## Performance (only for genuinely hot or repeated paths)

- [ ] **No back-of-envelope reasoning** for a path with meaningful N.
- [ ] **Per-event work that could be batched.**
- [ ] **Network / subprocess calls inside a loop** without a bound.

Don't over-apply performance rules to cold paths (a CLI's one-off run, a shell
function) — note when a rule doesn't earn its place there.

## Reporting

Use the template in SKILL.md. Omit any severity section with no findings. Always
include a short "Compliant" note on what's already TigerStyle. Don't pad.
