---
name: tiger-style-cpp
description: tiger-style rules for C++. Load together with the tiger-style skill whenever writing or reviewing C++ code under tiger style.
---

# C++ Rules

How TigerStyle and NASA's Power of 10 map onto C++. Read `tiger-style-c` first — most of
Power of 10 translates the same way; this file covers what C++'s type system adds:
the four named casts, `[[nodiscard]]`, and class-shaped dispatch. Every behaviour and
check named here was run on Apple clang++ 21.0.0 (`/usr/bin/clang++`, Xcode's default
toolchain) and Homebrew LLVM 23.1.5 (`clang++`, `clang-tidy`, formula `llvm`
23.1.5_1), cross-checked where noted. `cppcheck` is not installed on this machine and
is not cited.

Each rule follows the rule contract in tiger-style-core: Default, Banned, Why, Exception, Proof, Enforced by.

Scale to the stakes (SKILL.md "When to apply"): the full set is for code where a bug
corrupts data or state.

## 0. Project setup (copy this first)

`.clang-tidy` at the project root:

```yaml
Checks: >
  bugprone-*,
  cert-*,
  clang-analyzer-*,
  cppcoreguidelines-pro-type-*,
  google-readability-casting,
  misc-*,
  modernize-avoid-c-style-cast,
  modernize-use-nodiscard,
  readability-*
CheckOptions:
  readability-function-size.LineThreshold: '70'
  bugprone-assert-side-effect.CheckFunctionCalls: true
```

Compiler flags:

```
-Wall -Wextra -Wconversion -Wcast-qual -Wswitch-enum -std=c++17
```

Verified: `bugprone-unused-return-value`'s default `CheckedFunctions` list already
covers `std::unique_ptr::release`, `std::basic_string::empty`, and similar standard
functions, unlike C where the project's own functions must be added by name (see
`tiger-style-c` §7) — C++'s standard library is covered out of the box.

## 1-8. Power of 10, unchanged from C

Rules 1 (simple control flow), 2 (bounded loops), 4 (short functions), 6 (smallest
scope), 7 (check every return value), 8 (limit the preprocessor): **Default**,
**Banned**, **Why**, **Exception** and **Proof** are identical to `tiger-style-c`
(same sections, same numbers) — read them there rather than duplicated here.
**Enforced by** (Run on `.cpp` fixtures, same command shape):
`misc-no-recursion` (rule 1), `bugprone-infinite-loop` (rule 2),
`readability-function-size` with `LineThreshold: '70'` (rule 4), review only (rule
6), `bugprone-unused-return-value` — now with no config needed, see §0 (rule 7),
review only (rule 8, `bugprone-macro-parentheses` cited not run, same as C).

Rule 5 (assertions): **Default/Banned/Why/Exception/Proof** same as `tiger-style-c`
§5. **Enforced by:** `bugprone-assert-side-effect` with `CheckFunctionCalls: true`
(Run, identical behavior to C). C++ adds one idiom, not a change to the rule: an
invariant checked at a class boundary may throw a typed exception instead of
`assert`-ing, still never for untrusted input (`principles.md` "Assertions").

Rule 3 (no allocation after init): **Default/Banned/Why/Exception/Proof** same as
`tiger-style-c` §3. **Enforced by:** review only — C++'s containers hide `realloc`
inside `push_back`, so the review question becomes "is this container reused
across the hot path, or re-created with an unbounded grow".

## 9. Restrict pointers and casts — Power of 10 rule 9, C++'s real addition

- **Default:** `static_cast` for a known-safe widening/narrowing conversion between
  related types; `dynamic_cast` for a base→derived downcast through a polymorphic
  type, checked against `nullptr` (pointer form) or caught (reference form, which
  throws `std::bad_cast`); construct through a real constructor instead of casting
  at all where one exists.
- **Banned:** a C-style cast (`(Derived *)b`) anywhere — it silently picks whichever
  of `static_cast`/`const_cast`/`reinterpret_cast` compiles, hiding which one you
  meant; `reinterpret_cast` between unrelated types; `const_cast` to mutate through
  a pointer declared `const`; `static_cast` for a base→derived downcast (it skips
  the runtime type check `dynamic_cast` would do); laundering a typed pointer
  through `void *` and back to an unrelated type.
- **Why:** each of these deletes type information the compiler already had and
  fakes it back — `principles.md` "Types and boundaries": never launder type
  evidence.
- **Exception:** a genuine type-erasure boundary (a C ABI callback, placement-new
  into raw storage) — name the real type beside the cast in a comment, and prefer
  `std::bit_cast` (C++20) or a `union` with a comment over a raw `reinterpret_cast`
  where possible.
- **Proof:** point at the named cast used and, for a downcast, the `nullptr`/
  exception check right after it.
- **Enforced by:**
  - `cppcoreguidelines-pro-type-cstyle-cast` / `modernize-avoid-c-style-cast` /
    `google-readability-casting` (Run, all three — same C-style cast fired all
    three with matching "use static_cast"-shaped messages; clean on a `dynamic_cast`
    equivalent). Misses: nothing about *which* cast is right, only that it must be
    named.
  - `cppcoreguidelines-pro-type-reinterpret-cast` (Run — "do not use
    reinterpret_cast", unconditional ban regardless of whether the types are
    actually related). Misses: it fires even on a justified low-level cast; use the
    Exception above and silence per-line with a stated reason.
  - `cppcoreguidelines-pro-type-const-cast` (Run — "do not use const_cast to remove
    const qualifier", unconditional). Misses: same as above — a genuine
    const-correctness-API-mismatch case still needs a stated-reason suppression.
  - `cppcoreguidelines-pro-type-static-cast-downcast` (Run — "do not use
    static_cast to downcast from a base to a derived class; use dynamic_cast
    instead"). Misses: a downcast through a non-polymorphic hierarchy (no virtual
    functions) — `dynamic_cast` requires RTTI, which requires at least one virtual
    function; if the hierarchy has none, this check and `dynamic_cast` both can't
    help, and the real fix is adding a virtual interface or dropping the downcast.
  - `bugprone-casting-through-void` (Run — "do not cast 'Widget *' to 'Gadget *'
    through 'void *'; use reinterpret_cast instead"). This check is **C++-only**:
    confirmed it does not fire on the identical pattern compiled as C
    (`tiger-style-c` §9) — write it here, not in the C file.
  - `-Wcast-qual` (Run, same as C) for the const-qualifier-loss case specifically.
  - **Not a mechanical check:** "dynamic_cast laundering" — repeatedly
    `dynamic_cast`-probing a type instead of using virtual dispatch, or omitting the
    null/exception check after a `dynamic_cast`. Searched the full 604-check
    listing; nothing named for this. **Review only.**

## 10. All warnings, every day

Same flags as C (`-Wall -Wextra -Wconversion -Wcast-qual -Wswitch-enum`), plus
`-std=c++17` or later so `[[nodiscard]]` and `static_assert` with no message are
available.

## 11. C++-specific extras

### 11.1 `[[nodiscard]]` and unchecked return values

- **Default:** mark every function whose return value matters `[[nodiscard]]`; call
  sites check or explicitly discard with `std::ignore =` / a cast to `void`.
- **Banned:** calling a `[[nodiscard]]`-marked function (or a standard function like
  `std::unique_ptr::release`) and throwing away the result with no comment.
- **Why:** this is the same "check every return value" rule as `tiger-style-c` §7,
  made checkable by the compiler itself instead of needing a per-function list —
  discarding a value marked `[[nodiscard]]` is exactly the shape that call matters.
- **Exception:** the explicit `std::ignore = call();` / `(void)call();` form, which
  states the discard is deliberate rather than an oversight.
- **Proof:** point at the attribute on the declaration and the check/`(void)` at
  each call site.
- **Enforced by:**
  - `[[nodiscard]]` is a language feature the compiler itself enforces (Run —
    `-Wall` alone, no clang-tidy needed: "ignoring return value of function declared
    with 'nodiscard' attribute" `[-Wunused-result]`).
  - `modernize-use-nodiscard` (Run — "function 'size' should be marked
    [[nodiscard]]" on a const getter with no side effects). Misses: a function it
    can't prove is side-effect-free (anything calling into an opaque dependency) —
    it won't suggest the attribute there even if the value matters; add it by hand.
  - `bugprone-unused-return-value` (Run — fires on `std::unique_ptr::release()`
    discarded, with **no configuration needed**: the default `CheckedFunctions`
    list already covers common standard-library footguns, unlike C's version of
    this check which needed the project's own functions added by name).

### 11.2 NOLINT without a stated reason

Same verdict as `tiger-style-c` §11.1: **review only**. No check in this clang-tidy
requires a reason on `// NOLINT`.

### 11.3 Switch over an enum: no catch-all `default`

Same rule and checker as `tiger-style-c` §11.2: `-Wswitch-enum` (Run — "enumeration
values 'Retry' and 'Failed' not explicitly handled in switch" on an `enum class`
switch with a `default:`; plain `-Wswitch` on the identical file stayed silent).
`enum class` makes the exhaustive form easier to keep honest than C's plain `enum`,
since it has no implicit conversion to `int` to fall back on if a case is missed.
`bugprone-switch-missing-default-case` rejected for the same reason as the C file:
it demands the opposite of this rule.

## 12. Testing

Same discipline as `tiger-style-c` §12. C++ adds: a `dynamic_cast` failure path
(returns `nullptr` for pointers, throws for references) needs a test exercising the
failure, not just the success case — the paired-assertion idea from
`tiger-style-rust` §5, applied to a runtime type check instead of an `assert`.

## 13. Gates

```fish
set -x PATH /opt/homebrew/opt/llvm/bin $PATH   # clang-tidy isn't on PATH by default
clang++ -std=c++17 -Wall -Wextra -Wconversion -Wcast-qual -Wswitch-enum -c src/*.cpp
clang-tidy src/*.cpp -- -std=c++17
```

## Sources

- Gerard J. Holzmann, "The Power of 10: Rules for Developing Safety-Critical Code",
  NASA/JPL, IEEE Computer, 2006: https://spinroot.com/gerard/pdf/P10.pdf (written for
  C; this file is the C++ translation, same as `tiger-style-rust` is the Rust one).
- `clang-tidy` 23.1.5 check documentation: https://clang.llvm.org/extra/clang-tidy/checks/list.html
- TigerBeetle, `TIGER_STYLE.md` (copy in `upstream/`).
