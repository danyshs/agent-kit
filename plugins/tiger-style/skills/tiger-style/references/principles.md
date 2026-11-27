# TigerStyle Principles

Distilled from [TIGER_STYLE.md](https://github.com/tigerbeetle/tigerbeetle/blob/main/docs/TIGER_STYLE.md),
organized by design goal. These rules are language-neutral; Zig-origin rules are
flagged where they need translating. For how they apply in a given language, see
`<language>-rules.md` (C, C++, fish, Go, Java, Lua, Python, Rust, TypeScript).
Examples from a Python project appear throughout; they illustrate, they don't limit scope.

## The philosophy

- **Style = design.** Design goals in priority order: safety, performance, developer
  experience. On a forced tradeoff, the higher-priority goal wins.
- **Simplicity is elegance, not concession.** It's the hardest revision, not the first
  attempt.
- **Zero technical debt.** Code, like steel, is cheaper to change while hot. A problem
  solved in production costs many times more than one solved in design. An untested
  completed feature is debt; so is a premature test written against a feature that is
  still changing shape.
- **Spend the mental energy upfront.** An hour or a day of design is worth weeks or
  months in production.
- **A numeric criterion is only met if its measurement is sound.** When a requirement
  is a number — a rate, count, percentage, latency, threshold — hitting the figure is
  necessary but not sufficient. Interrogate the measurement that produced it: how many
  *independent* samples actually went in (a fixed input set cycled under deterministic
  decoding has an effective n far below the call count), does the result statistically
  support the stated bar (a small clean run bounds the true rate well below the point
  estimate), and does anything inflate it (retries masking first-attempt failures,
  cherry-picked inputs, a denominator dropping the hard cases). A "20/20 at ≥95%" that
  is really 10 fixed prompts at `temperature: 0` meets the letter and fails the
  substance. Report the honest bound, not the flattering figure.
- **Solve bottom-up: earn certainty about the parts before reasoning about the whole.**
  When a problem is unclear — a bug with several plausible causes, an integration that
  "just doesn't work", a config that won't take effect — do not fire hypotheses at the
  whole system and ask the user (or production) to tell you which stuck. That is
  guessing with extra steps, and it burns trust and time. Instead, decompose the system
  into its smallest independently-checkable pieces and establish, with a direct test or
  observation, exactly what each one does. Pin each fact before building on it: read the
  value actually on disk, not the one you wrote; confirm the daemon reloaded, not that
  the file changed; prove a keybind reaches layer N before theorizing about layer N+1.
  State plainly which rungs you verified yourself and which are genuinely blocked (e.g.
  a sandbox that cannot drive the GUI) and must be handed to the user — and when you
  hand one off, hand off one specific observation to make, not a menu of maybes.
- **The verified parts are a skill list; let an unbiased sub-agent plan the fight.**
  Think of each small piece whose behavior you have *confirmed* as a skill in an RPG
  character sheet: a known, bounded capability with a known effect. The overall problem
  is the boss. Once the skill list is real (not assumed), the composition step — which
  skills, in what order, beat the boss — is a planning problem, and it is often best
  given to a sub-agent that has the piece-level facts but none of the sunk-cost bias of
  whoever has been staring at the bug. Brief that sub-agent with the *verified*
  capabilities and constraints, not your running theory, so its plan is driven by what
  the parts actually do rather than by the hypothesis you are already attached to. A
  plan assembled from confirmed primitives is checkable; a plan assembled from assumed
  ones is another guess.

---

## Safety

Built on NASA's *Power of Ten — Rules for Developing Safety-Critical Code*.

### Control flow

- **Only simple, explicit control flow.** No recursion where iteration works — keeps
  bounded executions bounded. If you must recurse, the input must be finite/acyclic by
  construction or carry an explicit depth/size limit; document the rationale and add
  boundary tests.
- **Minimal abstractions.** Every abstraction risks leaking and has a cost.
- **Put a limit on everything.** Every loop and queue needs a fixed upper bound. Where
  a loop genuinely cannot terminate (an event loop), assert that intent.
- **A bound must do work, not just terminate.** Bounding an operation is necessary but
  not sufficient — the question is not only "does this stop?" but "does each iteration
  change the inputs enough that it can produce a different outcome?" A loop, retry,
  backoff, cache, or batch that is correctly bounded yet repeats identical work on
  every pass is still a defect: it burns the full budget (latency, calls, memory) and
  can only ever return the first pass's result. The classic case is a retry that
  resends a byte-identical request to a deterministic operation (e.g. same prompt,
  same seed, `temperature: 0`): the budget is spent but the odds never improve. For
  any bounded-and-repeated operation, verify that something feeds forward between
  iterations (new input, error context, changed parameter, elapsed-time/backoff state)
  so a later pass can succeed where an earlier one failed; if nothing changes, either
  make it change or drop the budget to one attempt and say why.
- **Don't react to external events directly.** Run at your own pace — batch instead of
  context-switching per event. Easier to bound work-per-period.

### Assertions

Assertions detect *programmer* errors. Operating errors are expected and handled;
assertion failures are unexpected, and the only correct response is to crash.
Assertions downgrade catastrophic correctness bugs into liveness bugs.

- **Assert arguments, return values, pre/post-conditions, invariants.** Minimum **two
  assertions per function on average**.
- **Pair assertions.** For a given property, assert it on at least two code paths —
  e.g. validate right before a write and immediately after the matching read.
- **Assert positive and negative space.** Assert what you expect AND what you don't.
  Interesting bugs live at the valid/invalid boundary.
- **Split compound assertions.** `assert a; assert b` beats `assert a and b` — clearer,
  and the failure is precise.
- **Use single-line `if` to assert implications.** `if a: assert b`.
- **Assertions aren't a substitute for understanding.** Build a precise mental model
  first, then encode it.
- Every language needs the same split: an assertion for the program's own invariants,
  an ordinary handled error for bad input from outside. Python: `assert` (stripped
  under `-O`) vs typed exceptions. fish: an assert helper returning 2 vs an error
  message returning 1. See the language's rules file.
- **Give each assertion a distinct message, and have tests check it.** Two assertions
  can fire on the same bad state; if a test only checks "something failed", one
  assertion silently stands in for the other and removing it goes unnoticed.

### Resource bounds (translation of "no dynamic allocation after init")

- TigerBeetle statically allocates all memory at startup. In languages that allocate
  dynamically (Python, shells, most scripting languages) the translation is:
  bound the growth of in-process collections, use pre-sized or capped structures, cap
  queue depth / batch size / retries, and set explicit timeouts on all external I/O.
  The point survives the language change: reason about worst-case resource use upfront.

### Scope & variables

- **Smallest possible scope.** Minimize the variables in scope at any point.
- **Don't introduce variables before needed; don't leave them after.**
- **POCPOU** (place-of-check to place-of-use) — compute and check close to use.
- **Don't duplicate variables or take aliases** — reduces the chance state drifts.

### Functions

- **Target 70 lines, hard limit 140** (excluding decorators/docstring). Prefer
  functions that fit on a screen; up to 140 is allowed in service of better
  modularization and separation of concerns. Past 140, split.
- **Inverse-hourglass shape.** Few parameters, simple return type, meaty body.
- **Centralize control flow.** "Push `if`s up and `for`s down." One function owns the
  branching; leaves are pure computation.
- **Centralize state manipulation.** Parent holds state in locals; helpers compute what
  should change; parent applies it.
- **Simpler return types reduce dimensionality.** Prefer `None` / a value over
  `Optional` over a union; this propagates through call chains.
- **Don't type states the live code never reaches.** Fewer optional parameters and fields:
  a type that allows a combination no caller produces makes every reader handle it. Make the
  input required, or split the type.

### Errors

- **Handle every error.** 92% of catastrophic failures in distributed systems come
  from mishandled non-fatal errors.
- **Honor a stated error contract exhaustively.** When a function declares what it
  raises ("returns bytes or raises `FooError`", a typed-exception boundary, a "maps
  the X failure family to Y" claim), that contract is only as good as the exception
  hierarchy underneath it. Enumerate every type the inner operations can raise and
  verify each is mapped or deliberately allowed — do not trust that an `except` tuple
  "looks complete." Two silent failure shapes: a type that is **not** a subclass of
  anything caught escapes raw and breaks the contract (`http.client.IncompleteRead`
  is a `HTTPException`, not an `OSError`); and a **subclass** caught by a broader
  parent in the same tuple has its payload swallowed (`HTTPError` IS-A `URLError`, so
  catching `URLError` loses the server's error body — catch the specific subclass
  first). Verify the real MRO (`python -c "print(E.__mro__)"`); don't assume it.
- **Explicit control flow for errors too.** No silent swallowing. (Python: no bare
  `except:`, catch specific types, preserve context.)

### Types and boundaries

Every typed language has a way to tell the checker something untrue. These four rules keep
the checker's picture honest. They apply to Python (`Any`), TypeScript (`unknown`, `object`,
`any`), Go (`any`, `interface{}`), Java (`Object`), Rust (`dyn Any`) and C/C++ (`void *`).
C's `void *` laundering has a checker only in C++ mode (`bugprone-casting-through-void`
fires on a `.cpp` file, not the identical `.c` one — verified, see `c-rules.md` §9); plain C
is review only for the general case, with `-Wcast-qual` covering the narrower
const-qualifier-loss case in both languages. Zig has `anytype`,
`*anyopaque` and `@ptrCast`, with no checker for them: review only. Lua and fish get no
top-type rule: fish has no types, and for Lua (`any` in lua-language-server annotations) this
doc claims no tool check, so it is review only. The bypass rule below still applies to Lua's
`---@diagnostic disable`.

Checker coverage for the top-type rule specifically (which languages have one, verified
on the real toolchain — see each `<language>-rules.md` §1/§9 for fixtures): Run in Python
(`ruff` `ANN401`), TypeScript (`no-explicit-any` plus the `no-unsafe-*` family), and C++'s
`void *` case (`bugprone-casting-through-void`, which does not fire on the identical
pattern compiled as C). Review only in Go (`any`/`interface{}` in a signature —
`forbidigo` was probed and cannot see a type used in a signature, only call/selector
expressions) and plain C (`void *`, general case — `-Wcast-qual` catches only the
narrower const-qualifier-loss half). No rust-rules.md section targets `dyn Any`
specifically, so Rust stays unverified here, same as Java's `Object`.

- **Top types belong at the boundary.** A top type is allowed where untrusted input enters:
  a parser, a deserializer, an error's cause. Internal parameters, return types, aliases
  and dictionary values name the domain type. Renaming a top type with an alias changes
  nothing.
- **Never launder type evidence.** Don't widen a value the checker already knows and cast it
  back. Don't chain casts through a wide type. Each one deletes a proof and then fakes one.
- **Decode once at the boundary, then branch on the domain value.** Untrusted input becomes
  a named type in one place. Past that place, don't re-probe the same data's representation
  (`isinstance`, `typeof` or a type switch on its raw form). Dispatching on the parsed domain
  type is fine: `isinstance` or `match` on your own classes, a sealed-type switch, a type
  switch over a sum. Not covered by this rule: invariant assertions and type-guard
  functions, which check the program's own claim (see Assertions).
- **Every checker bypass states its invariant.** A cast, a non-null `!`, `# type: ignore`,
  `---@diagnostic disable`, `eslint-disable`, `#[allow(...)]`, an `unsafe` block: one line
  beside it says why the checker is wrong here. A rule code alone is not a reason. (Rust's
  `// SAFETY:` and `expect("invariant: ...")` and Lua's one-line suppression reason are this
  rule in two languages.)

### Conditionals

- **Split compound conditions** into nested `if/else` so both branches are explicit.
- **State invariants positively.** `if index < length` reads easier than the negation.
- **Consider whether every `if` needs an `else`** to cover positive and negative space.

### Calls

- **Pass options explicitly at the call site.** Don't rely on library defaults — they
  can change. (Python: prefer keyword arguments for safety-critical behavior.)

### Toolchain

- **All warnings on, strictest setting, from day one.** Every language in the repo has a
  linter whose committed config errors on every warning (detail: SKILL.md "Toolchain gates").
  Use the project's formatter,
  linter, checker and tests (e.g. a Python project: `ruff` + `pyright` clean, `pytest` green;
  fish: `fish_indent --check`, `fish --no-execute`, the script's `--test`). See the
  language's rules file.

### Motivation

- **Always say why.** Rationale shares the decision criteria so reviewers can evaluate
  edge cases.

---

## Performance

> "The lack of back-of-the-envelope performance sketches is the root of all evil."

- **Think about performance from the outset** — the big wins come in design, exactly
  when you can't measure yet.
- **Back-of-envelope sketches.** Four resources (network, disk, memory, CPU) × two
  dimensions (bandwidth, latency).
- **Optimize slowest resources first** (network → disk → memory → CPU), weighted by
  frequency. A cache miss called many times can cost more than a single `fsync`.
- **Amortize costs by batching.** Let the CPU be a sprinter — predictable, large chunks
  of work rather than zig-zagging per event.
- **Be explicit; minimize magic.** Extract hot loops into standalone functions with
  primitive arguments so redundant work is visible.

Scale this to the project. For a small local tool (a Python CLI, a shell
function) the day-to-day relevance is: avoid unbounded work per invocation, don't
shell out or hit the network in a loop without a bound, and keep startup cheap. Don't
over-engineer for throughput the project doesn't need.

---

## Developer experience

### Naming

- **Get nouns and verbs right.** Names capture what a thing is or does.
- **`snake_case`** for functions, variables, files.
- **No abbreviations** (full-length CLI flags like `--force`, not single letters).
- **Units last, descending significance.** `latency_ms_max`, not `max_latency_ms` —
  groups related variables and lines up with `latency_ms_min`.
- **Infuse names with meaning.** A name should tell the reader what role the value
  plays, not just its type.
- **Same character count for related names.** `source`/`target` beats `src`/`dest` so
  derived names (`source_offset`/`target_offset`) line up.
- **Prefix helpers with the caller's name** when a helper is called from exactly one
  function (`read_sector` → `read_sector_callback`).
- **Callbacks go last** in the parameter list.
- **Order for readability.** Files are read top-down; put important things near the top.
- **Don't overload names.** One term shouldn't mean two things in two contexts.
- **Named arguments when positions are mixable.** Two same-typed args should be keyword
  arguments so the call site is unambiguous.

### Comments & commits

- **Descriptive commit messages** — they're read, PR descriptions aren't in `git blame`.
- **Say why, not what.** Code alone isn't documentation.
- **Say how for tests.** A top-of-test comment states the goal and methodology so a
  reader can skim.
- **Comments are prose.** Space after `#`, capital letter, full stop.

### Avoiding stale state

- **Don't alias or duplicate variables.** Reduces desync risk.
- **Shrink scope; calculate close to use.** Avoid POCPOU.
- **Keep critical sections synchronous.** Don't suspend (`await`) in the middle of
  assertion-heavy logic, so preconditions hold for the section's lifetime.

### Off-by-one

- **`index`, `count`, and `size` are distinct ideas.** `index → count`: add 1
  (0-based vs 1-based). `count → size`: multiply by unit. This is why units-in-names
  matters.
- **Show intent in division.** Make rounding explicit (`//`, `math.ceil`, a named
  helper, or a comment) rather than a bare `/` whose rounding is ambiguous.

### Style by the numbers

- **4-space indentation.**
- **Hard 100-column line limit.**
- **Run the formatter** — the project's own (Python: `ruff format`; fish:
  `fish_indent`).

### Dependencies

- **Each dependency has a real cost** — supply chain, safety, install time. Justify
  every one. TigerBeetle's "zero dependencies" is extreme for most domains; the
  practical rule is: add a dependency only when it clearly earns its place, and pin it.

### Tooling

- **Small, standardized toolbox** — one tool known well beats many known poorly. Use
  the project's established tools (e.g. Python: `uv`, `ruff`, `pyright`, `pytest`);
  don't add a second tool for a job one already does. In a shell, that includes not
  switching shells: write fish in fish rather than reaching for bash.

---

## Zig-only rules dropped or translated

Skip these literally; each language's mapping (where any) is in its rules file:
`zig fmt`, `@divExact`/`@divFloor`, `@prefetch` options, `usize`/`u32` sizing,
`*const` for 16+ byte args, in-place init via out-pointer, struct field/type/method
ordering idiom, the `options: struct` named-argument pattern, CamelCase struct files,
`comptime` constant assertions, `defer`-grouped cleanup (Python uses `with`).
