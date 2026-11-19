---
name: tiger-style-c
description: tiger-style rules for C. Load together with the tiger-style skill whenever writing or reviewing C code under tiger style.
---

# C Rules

How TigerStyle and NASA's Power of 10 map onto C — the language Holzmann wrote Power
of 10 for, so this file is the most literal of the rules files. Every behaviour and
check named here was run on Apple clang 21.0.0 (clang-2100.4.1.1, `/usr/bin/clang`,
Xcode's default toolchain) and Homebrew LLVM 23.1.5 (`clang`, `clang-tidy`, Homebrew
formula `llvm` 23.1.5_1), cross-checked where noted. `cppcheck` is not installed on
this machine and is not cited.

Each rule follows the rule contract in tiger-style-core: Default, Banned, Why, Exception, Proof, Enforced by.

Scale to the stakes (SKILL.md "When to apply"): the full set is for code where a bug
corrupts data or state. For glue and small tools, keep the asserts and bounds, relax
the allocation and dispatch rules.

## 0. Project setup (copy this first)

`.clang-tidy` at the project root:

```yaml
Checks: >
  bugprone-*,
  cert-*,
  clang-analyzer-*,
  misc-*,
  readability-*,
  google-readability-casting
CheckOptions:
  readability-function-size.LineThreshold: '70'
  bugprone-assert-side-effect.CheckFunctionCalls: 'true'
  bugprone-unused-return-value.CheckedFunctions: '::my_project_function_a;::my_project_function_b'
```

Compiler flags (add to the build's base `CFLAGS`):

```
-Wall -Wextra -Wconversion -Wcast-qual -Wvla -Wswitch-enum -Wbad-function-cast
```

Verified: `bugprone-assert-side-effect.CheckFunctionCalls` defaults to `false` — the
option must be set explicitly or an `assert()` wrapping a mutating call warns nothing
(seen to trip both ways). `-Wswitch-enum` catches an unhandled enum value
even when a `default:` exists; plain `-Wswitch` does not — that gap is exactly the
anti-slop pattern rule 11.2 below targets.

## 1. Simple control flow — Power of 10 rule 1

- **Default:** loops and early `return`; `switch` with every enumerator written out.
- **Banned:** recursion (direct or mutual).
- **Why:** an acyclic call graph bounds stack use and execution time.
- **Exception:** recursion over input finite by construction, with an explicit depth
  limit asserted and a test at the limit.
- **Proof:** point at the loop that replaced the recursion, or the depth assert.
- **Enforced by:** `misc-no-recursion` (Run — fires on direct self-recursion with the
  full call chain in the note; also catches mutual recursion through two functions,
  not run here). Misses: a function pointer cycle (callback A calls B calls A) is
  invisible to it — review for that shape.

## 2. Bounded loops — Power of 10 rule 2

- **Default:** `for (i = 0; i < COUNT_MAX; i += 1)`, or a counter asserted against a
  max: `attempts += 1; assert(attempts <= ATTEMPTS_MAX);`.
- **Banned:** a loop whose condition variable is never updated in the body.
- **Why:** runaway loops become a located crash instead of a hang.
- **Exception:** an event loop that must never end: assert that intent, don't just
  omit the bound.
- **Proof:** name the bound constant for each loop.
- **Enforced by:** `bugprone-infinite-loop` (Run — fires when none of a loop's
  condition variables are updated inside it). Misses: a loop that IS bounded but
  whose bound is absurdly large, or one bounded only through an aliased pointer the
  check can't trace — review for those.

## 3. No allocation after initialisation — Power of 10 rule 3

- **Default:** fixed-size arrays (`int buffer[COUNT_MAX];`) or one `malloc` at
  start-up, sized once.
- **Banned:** `malloc`/`realloc` in a hot or safety-critical path; unbounded stack
  allocation (`int buffer[count];` — a VLA sized by a runtime value, no cap).
- **Why:** allocation is unpredictable in time and can fail; a VLA sized by untrusted
  input can blow the stack with no guard page warning on most platforms.
- **Exception:** start-up, CLI argument handling, tests, glue code.
- **Proof:** name where each buffer is sized and the constant that sizes it.
- **Enforced by:** `-Wvla` (Run — "variable length array used"; cross-checked on
  Apple clang and Homebrew clang, identical) for the stack-overflow-shaped half of
  this rule. Heap allocation count/size with no cap: **review only** — no compiler
  flag or clang-tidy check found for it (same gap as `tiger-style-rust` rule 3).

## 4. Short functions — Power of 10 rule 4

- **Default:** 70 lines (TigerStyle's limit). Push branching into the parent, loops
  into pure helpers.
- **Banned:** functions over 140 lines.
- **Proof / Enforced by:** `readability-function-size` with `LineThreshold: '70'`
  (Run — an 83-line function reported "exceeds recommended size/complexity
  thresholds", note "83 lines ... (threshold 70)"; a 4-line function: clean). Misses:
  it counts lines/statements/branches, not cognitive load — a short function doing
  five unrelated things still passes; pair with `readability-function-cognitive-
  complexity` (cited, not run) or review.

## 5. Assertions — Power of 10 rule 5

- **Default:** `assert()` from `<assert.h>` on every argument, return value, and
  invariant; compute the value first, assert the pure boolean on the already-computed
  result.
- **Banned:** a function call with a side effect inside the `assert()` expression
  itself — in a release build compiled with `NDEBUG`, `assert()` is a no-op and the
  call (and its side effect) vanishes with it.
- **Why:** TigerStyle: "the only correct way to handle corrupt code is to crash" — but
  only if the assert actually ran. An assert whose condition both does the work and
  checks it silently stops doing the work once `NDEBUG` strips it.
- **Exception:** none — move the call above the `assert` and store the result.
- **Proof:** every assert has a literal `const char *` message (C doesn't give
  `assert()` one directly; use `assert(x && "message")`), and a test that forces the
  condition false and checks the program aborts (or, for a handled error instead of
  an assert, checks the returned error code).
- **Enforced by:** `bugprone-assert-side-effect` with `CheckFunctionCalls: true`
  (Run — the default has this option `false` and misses the exact case this rule
  bans; set it explicitly, see section 0). Misses: a side effect through a macro or
  an overloaded operator it doesn't recognise — review for those.

## 6. Smallest scope — Power of 10 rule 6

- **Default:** declare each variable at first use, inside the narrowest block
  (C99's `for (int i = 0; ...)` scoping, not a function-top block of declarations).
- **Banned:** `static` function-local variables used as hidden mutable state; a
  block of declarations at the top of a long function, far from their use.
- **Why:** a variable visible past where it's needed is a variable a later edit can
  misuse. **Exception:** a loop-carried accumulator declared once above the loop on
  purpose, with a comment saying it's carried. **Proof:** point at the declaration
  and show no code between it and first use.
- **Enforced by:** review only — no clang-tidy check flags scope width in C.

## 7. Check every return value and parameter — Power of 10 rule 7

- **Default:** check the return value of every function that can fail; assert
  parameter validity at the top of the function.
- **Banned:** `remove(path);`, `fprintf(...);` or any call whose failure matters,
  used as a bare statement with the result thrown away.
- **Why:** an unchecked return is a failure the caller never reacts to; it just
  keeps running on bad state. **Exception:** the C idiom for "I mean to discard
  this" is an explicit `(void)expr;` cast, with a comment saying why the result
  can't matter.
- **Proof:** point at the `if` (or `(void)`) immediately after the call.
- **Enforced by:**
  - `cert-err33-c` (Run — "the value returned by this function should not be
    disregarded" on a discarded `remove()` call; clean once checked). Covers a fixed
    list of C/POSIX standard-library functions (`fopen`, `fclose`, `remove`,
    `fread`, `scanf`, …). Misses: your own project's functions — it has no
    configuration list, unlike the next check.
  - `bugprone-unused-return-value` with `CheckedFunctions` naming your own functions
    (Run — configured for a project function `my_alloc`, fired on the discarded
    call, clean once the result was stored). Misses: anything not named in the
    option; the default list is nearly all C++ standard library, so for C you must
    list your own functions explicitly (section 0).
  - `__attribute__((warn_unused_result))` plus `-Wall`/`-Wextra` (Run — "ignoring
    return value of function declared with 'warn_unused_result' attribute"
    `[-Wunused-result]`) for functions you declare yourself. Misses: nothing it's
    told about, but you must add the attribute to every such declaration — it isn't
    inferred.

## 8. Limit the preprocessor — Power of 10 rule 8

- **Default:** functions and `static inline` over macros; at most a couple of
  `#ifdef` platform switches.
- **Banned:** a macro that hides control flow (`return`, a loop, a `goto`) so a
  caller can't see it from the call site.
- **Why:** each conditional-compilation branch doubles what must be tested.
- **Exception:** a small, fixed set of platform `#ifdef`s (e.g. POSIX vs Win32
  path separators), each with a comment naming the platforms. **Proof:** point at
  the function/`static inline` that replaced the macro, or the `#ifdef` comment.
- **Enforced by:** review only. `bugprone-macro-parentheses` exists and is useful
  hygiene (an unparenthesized macro argument/body) but it is a different, narrower
  concern than control-flow-hiding; cited, not run, since it doesn't check this rule.

## 9. Restrict pointers — Power of 10 rule 9

- **Default:** one level of dereference; pass structs by pointer only where the
  callee needs to see the caller's copy, otherwise by value or `const *`.
- **Banned:** laundering a typed pointer through `void *` and back to an unrelated
  type; casting away `const` to mutate through what was declared read-only; function
  pointers that hide which function actually runs.
- **Why:** each of these deletes type information the compiler already had and
  fakes it back — exactly the "never launder type evidence" rule in `principles.md`
  "Types and boundaries", applied to C's only generic-typing tool.
- **Exception:** a genuine type-erasure boundary (`qsort`'s comparator, a callback
  table) — name the real type beside the `void *` in a comment at the one place it's
  cast back.
- **Proof:** point at the real type the `void *` always holds, and the single place
  it's cast back to that type.
- **Enforced by:** `-Wcast-qual` (Run — "cast from 'const int *' to 'int *' drops
  const qualifier"; cross-checked on Apple and Homebrew clang) catches the
  const-laundering half. The general `void *` → unrelated-type laundering has **no
  check in C**: `bugprone-casting-through-void` exists but only fires when compiled
  as C++ (confirmed — the same cast pattern produced 0 warnings as `.c`, 1 warning
  as `.cpp`; its own suggested fix is `reinterpret_cast`, a C++ keyword). For plain C
  this half is **review only**.

## 10. All warnings, every day — Power of 10 rule 10

- **Default:** the flags in section 0, in the build from day one, warnings treated
  as errors in CI (`-Werror`).
- **Exception:** a single, commented suppression on the smallest scope, never a
  blanket disable.
- Verified individually: `-Wconversion` ("implicit conversion loses
  integer precision: 'int' to 'unsigned char'" `[-Wimplicit-int-conversion]`),
  `-Wbad-function-cast` ("cast from function call of type 'int' to non-matching type
  'void *'", plus `-Wint-to-void-pointer-cast` on the same line), `-Wvla`,
  `-Wcast-qual`, `-Wswitch-enum` (see rule 11.2).

## 11. C-specific extras

### 11.1 NOLINT without a stated reason

- **Default:** every `// NOLINT(check-name)` or `// NOLINTNEXTLINE` carries a reason
  on the same line: `// NOLINT(cert-err33-c): return value checked by the caller`.
- **Banned:** a bare `// NOLINT` with no check name and no reason.
- **Why:** a suppression with no reason can't be told apart from "I didn't look"
  two years later.
- **Exception:** none — a suppression worth keeping is worth the one line.
- **Proof:** point at the reason text following the `NOLINT`/`NOLINTNEXTLINE`.
- **Enforced by:** review only — searched `clang-tidy --help` and the full check
  listing (`--list-checks --checks='*'`, 604 checks) for anything that requires a
  reason on a `NOLINT` comment; none exists in this clang-tidy. (Go's `nolintlint`
  and Rust's `undocumented_unsafe_blocks` do this for their languages; C has
  nothing equivalent.)

### 11.2 Switch over an enum: no catch-all `default`

- **Default:** a `switch` over your own enum names every enumerator explicitly, with
  no `default:` label, so adding a new enumerator later breaks the build.
- **Banned:** `default:` on a switch over an enum you own, as a stand-in for the
  cases you didn't write.
- **Why:** plain `-Wswitch` is satisfied the moment a `default:` exists, even if
  enumerators are unhandled — a `default:` silently absorbs every future value
  nobody wrote a case for. This is the C shape of Rust's `wildcard_enum_match_arm`.
- **Exception:** a `switch` over an enum from a library you don't control, where new
  values may appear between releases outside your control — keep the `default:` and
  say so in a comment (`// default: library enum may grow; see <link>`).
- **Proof:** point at either the exhaustive case list, or the comment justifying the
  default.
- **Enforced by:** `-Wswitch-enum` (Run — "enumeration values 'STATUS_RETRY' and
  'STATUS_FAILED' not explicitly handled in switch"; plain `-Wswitch` on the
  identical file stayed silent, confirming the gap this rule closes; cross-checked
  on Apple clang 21.0.0 and Homebrew clang 23.1.5, identical). Misses: a `switch`
  over an `int` masquerading as an enum (no `enum` type at all) — review for that.
  `bugprone-switch-missing-default-case` was considered and rejected: it demands a
  `default:` on every switch, which directly conflicts with this rule's intent.

### 11.3 Explicitly sized integers at boundaries

- **Default:** `<stdint.h>` fixed-width types (`int32_t`, `uint8_t`) at any boundary
  crossing a narrower range than `int`, with an explicit range check before the
  narrowing cast.
- **Banned:** an implicit narrowing conversion with no check (`unsigned char x =
  some_int * 2;`).
- **Why:** an implicit narrowing silently drops high bits — a boundary crossing a
  narrower range should say so with a checked cast, not the compiler's default
  promotion. **Exception:** a narrowing the value's own range already proves safe
  (e.g. just masked with `& 0xFF`), with a comment at the cast saying why.
- **Proof:** point at the range check immediately before the cast.
- **Enforced by:** `-Wconversion` (Run — fires via `-Wimplicit-int-conversion` on
  exactly this shape; clean once the value is widened, range-checked, then cast
  explicitly). Misses: a narrowing inside a macro or generated code the flag
  doesn't expand into — review for those.

## 12. Testing

- Every assert has a test that forces the condition false and checks the crash (or,
  for a handled error, checks the returned code) — same as `tiger-style-rust` §12,
  translated: C has no `#[should_panic]`, so fork-and-check the child's signal, or
  test the handled-error path directly where the invariant is an operating error
  rather than a true assertion.
- Mutation testing: no mutation tool was run for C on this machine; a manual
  mutation pass (flip a boundary, change a constant) on each new branch is the
  minimum per SKILL.md's testing discipline.

## 13. Gates

```fish
set -x PATH /opt/homebrew/opt/llvm/bin $PATH   # clang-tidy isn't on PATH by default
clang -std=c11 -Wall -Wextra -Wconversion -Wcast-qual -Wvla -Wswitch-enum -Wbad-function-cast -c src/*.c
clang-tidy src/*.c -- -std=c11
```

## Sources

- Gerard J. Holzmann, "The Power of 10: Rules for Developing Safety-Critical Code",
  NASA/JPL, IEEE Computer, 2006: https://spinroot.com/gerard/pdf/P10.pdf — C is the
  language this was written for.
- `clang-tidy` 23.1.5 check documentation: https://clang.llvm.org/extra/clang-tidy/checks/list.html
- TigerBeetle, `TIGER_STYLE.md` (copy in `upstream/`).
