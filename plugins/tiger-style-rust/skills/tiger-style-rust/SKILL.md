---
name: tiger-style-rust
description: tiger-style rules for Rust. Load together with the tiger-style skill whenever writing or reviewing Rust code under tiger style.
---

# Rust Rules

How TigerStyle and NASA's Power of 10 map onto Rust. Read this instead of
`tiger-style-python` when the target is Rust. Every behaviour and lint named here was
checked on rustc / cargo 1.99.1, clippy 0.1.99, rustfmt 1.10.0, edition 2024
(Homebrew `rust` formula, macOS, 2026-10-19).

Each rule follows the rule contract in tiger-style-core: Default, Banned, Why, Exception, Proof, Enforced by.

Scale to the stakes (tiger-style-core, "When to apply"): the full set is for code where a bug
corrupts data or state. For glue and small tools, keep the asserts, bounds and gates,
and relax the allocation and dispatch rules.

## 0. Project setup (copy this first)

```toml
# Cargo.toml
[lints.rust]
unsafe_code = "forbid"                         # rule 9; relax to "deny" + SAFETY comments if needed

[lints.clippy]
pedantic = { level = "warn", priority = -1 }   # priority -1 so single lints below can override it
unwrap_used = "deny"                           # expect("invariant: …") stays allowed, see rule 5
panic = "warn"
indexing_slicing = "warn"
as_conversions = "warn"
cast_possible_truncation = "warn"
let_underscore_must_use = "warn"
undocumented_unsafe_blocks = "deny"
too_many_lines = "warn"
wildcard_enum_match_arm = "warn"
allow_attributes_without_reason = "warn"

[profile.release]
overflow-checks = true   # without this, u32::MAX + 1 silently becomes 0 in release
panic = "abort"          # an assertion failure ends the process; nothing unwinds past it
```

```toml
# clippy.toml
too-many-lines-threshold = 70
```

Verified: with this setup an `unwrap()` fails the build, a 72-line function warns
"too many lines (72/70)", `x as u8` warns about truncation, and `v[0]` warns that
indexing may panic. A misspelt lint name is reported as `unknown lint`, so typos in
this table can't silently do nothing. `_ =>` over a local enum with two or more
unmatched variants fails the lint (`wildcard_enum_match_arm`). `#[allow(dead_code)]`
with no `reason = "…"` fails `allow_attributes_without_reason`; adding the reason
clears it.

## 1. Simple control flow — Power of 10 rule 1

> "Restrict all code to very simple control flow constructs—do not use goto
> statements, setjmp or longjmp constructs, or direct or indirect recursion."
> — Holzmann

- **Default:** loops and early `return`; split compound conditions into nested
  `if`/`else`; `match` with every arm written out.
- **Banned:** recursion (direct or through traits/closures); `match` arms that hide
  a case behind `_ =>` when the enum is yours.
- **Why:** an acyclic call graph lets you bound stack use and execution time.
- **Exception:** recursion over input that is finite by construction, with an
  explicit depth limit asserted and a test at the limit.
- **Proof:** point at the loop that replaced the recursion, or at the depth assert.
- **Enforced by:** recursion: review (rustc's `unconditional_recursion` only catches a
  function that can never return; ordinary recursion has no lint). `_ =>` arms: clippy
  `wildcard_enum_match_arm`. It also fires on a foreign `#[non_exhaustive]` enum, where `_ =>`
  is required; put `#[allow(clippy::wildcard_enum_match_arm, reason = "…")]` on that match
  (see rule 10 for the reason requirement).

## 2. Bounded loops — Power of 10 rule 2

> "Give all loops a fixed upper bound. It must be trivially possible for a
> checking tool to prove statically that the loop cannot exceed a preset upper
> bound on the number of iterations." — Holzmann

- **Default:** `for i in 0..COUNT_MAX` or `for item in fixed_slice`. Where the
  count is only known at runtime, keep a counter and assert it:
  `iterations += 1; assert!(iterations <= ITERATIONS_MAX, "…");`
- **Banned:** a bare `loop {}` or `while` with no cap.
- **Why:** runaway loops become an immediate, located crash instead of a hang.
- **Exception:** an event loop that must never end: assert that it never exits
  (e.g. the code after it is `unreachable!("event loop returned")`).
- **Proof:** name the bound constant for each loop.
- **Enforced by:** review.

## 3. No allocation after initialisation — Power of 10 rule 3

> "Do not use dynamic memory allocation after initialization." — Holzmann

- **Default:** fixed arrays (`[T; N]`), or `Vec::with_capacity(N)` at start-up and
  never grown past `N`. Pair the asserts: `assert!(v.len() < v.capacity())` before
  a push, `assert_eq!(v.capacity(), N)` after it, so a reallocation is caught.
- **Banned:** `push` / `collect` / `format!` / `String` building in the hot path of
  safety-critical code.
- **Why:** allocation is unpredictable in time and can fail; fixed memory can be
  reasoned about up front.
- **Exception:** start-up, CLI argument handling, error messages, tests, and glue
  code (scale to the stakes).
- **Proof:** name where each buffer is sized and the constant that sizes it.
- **Enforced by:** review (no stable lint bans allocation).

## 4. Short functions — Power of 10 rule 4

> "No function should be longer than what can be printed on a single sheet of
> paper … Typically, this means no more than about 60 lines of code per
> function." — Holzmann

- **Default:** 70 lines (TigerStyle's limit, a little looser than Holzmann's 60).
  Keep branching in the parent, push loops into pure helpers.
- **Banned:** functions over 140 lines (the skill's hard limit).
- **Proof / Enforced by:** clippy `too_many_lines` with `too-many-lines-threshold = 70`.

## 5. Assertions — Power of 10 rule 5

> "The code's assertion density should average to minimally two assertions per
> function. … Assertions must be side-effect free and should be defined as
> Boolean tests." — Holzmann

- **Default:** `assert!` with a message, for every argument, return value and
  invariant. One condition per assert: `assert!(a, "…"); assert!(b, "…");`.
  Compile-time facts go in const asserts:
  `const _: () = assert!(size_of::<Record>() == 16);` (verified; `size_of` is in
  the 2024 prelude).
- **Banned:**
  - `debug_assert!` for invariants. Verified: it is compiled out of release builds,
    so the check you rely on disappears exactly where it matters.
  - `unwrap()` anywhere outside tests (denied by the lints above).
  - Asserting on untrusted input. Bad input is an operating error: return
    `Result<T, Error>` with a typed error enum and use `?`.
- **Why:** an assertion turns a silent correctness bug into a loud crash at the
  place it happened.
- **Conflict to know about:** Holzmann's rule 5 continues, "When an assertion
  fails, an explicit recovery action must be taken such as returning an error
  condition to the caller". TigerStyle says the opposite: "The only correct way to
  handle corrupt code is to crash." This skill follows TigerStyle — `assert!` plus
  `panic = "abort"` — and uses `Result` only for operating errors, which are
  expected and not assertions at all.
- **Exception:** `debug_assert!` for a check too expensive for release (an O(n)
  scan in a hot loop), with a comment saying so. `expect("invariant: …")` where the
  message states the invariant; it is an assert with a message.
- **Proof:** every assert has a distinct message, and a test triggers it and checks
  that message: `#[should_panic(expected = "batch_limit must be positive")]`
  (verified: the test fails if the message differs).
- **Enforced by:** clippy `unwrap_used`, `panic`; review for density and for every
  `expect` message actually stating an invariant. (Don't turn on
  `clippy::expect_used`: with `-D warnings` it rejects the allowed
  `expect("invariant: …")` form too — checked.)

## 6. Smallest scope — Power of 10 rule 6

> "Declare all data objects at the smallest possible level of scope." — Holzmann

- **Default:** `let` at first use, inside the narrowest block; shadow rather than
  reuse a `mut` for a second purpose.
- **Banned:** `static mut`; long-lived `mut` locals declared at the top of a
  function.
- **Enforced by:** `static mut` needs `unsafe`, so `unsafe_code = "forbid"` rules
  it out; the rest is review.

## 7. Check every return value and every parameter — Power of 10 rule 7

> "Each calling function must check the return value of nonvoid functions, and
> each called function must check the validity of all parameters provided by the
> caller." — Holzmann

- **Default:** `Result` and `Option` are `#[must_use]`, so ignoring one is a
  warning. Add `#[must_use]` to your own functions whose result matters (clippy
  pedantic's `must_use_candidate` suggests where). Assert parameters at the top of
  each function.
- **Banned:** `let _ = fallible_call();` with no comment.
- **Exception:** the equivalent of Holzmann's `(void)` cast — `let _ = …;` with a
  comment saying why the result can't matter.
- **Enforced by:** rustc `unused_must_use`; clippy `let_underscore_must_use`.

## 8. Limit the preprocessor — Power of 10 rule 8

> "The use of the preprocessor must be limited to the inclusion of header files
> and simple macro definitions. … The use of conditional compilation directives
> must be kept to a minimum." — Holzmann

- **Default:** functions and generics over `macro_rules!`; at most one or two
  `#[cfg(…)]` switches beyond `#[cfg(test)]`.
- **Banned:** macros that hide control flow (`return`, `?`, loops inside a macro);
  proc macros you wrote yourself in safety-critical code.
- **Why:** each `cfg` switch doubles the number of builds that need testing.
- **Enforced by:** review.

## 9. Restrict pointers — Power of 10 rule 9

> "The use of pointers must be restricted. Specifically, no more than one level
> of dereferencing should be used. … Function pointers are not permitted." —
> Holzmann

- **Default:** references (`&T`, `&mut T`), which the borrow checker already
  restricts; generics with static dispatch.
- **Banned:** raw pointers and `unsafe` (forbidden by the lints above);
  `dyn Trait`, `fn` pointers and boxed closures in safety-critical code, because
  they hide which function runs and can hide recursion.
- **Exception:** `unsafe` where there is no safe way, each block with a
  `// SAFETY:` comment (required by `undocumented_unsafe_blocks`); `dyn Trait` at a
  plugin or test boundary.
- **Enforced by:** `unsafe_code`, `undocumented_unsafe_blocks`; review for dispatch.

## 10. All warnings, every day — Power of 10 rule 10

> "All code must be compiled, from the first day of development, with all
> compiler warnings enabled at the most pedantic setting available. All code must
> compile without warnings." — Holzmann

- **Default:** the lint table in section 0, with warnings treated as errors in
  the gate (`-D warnings`).
- **Exception:** a single `#[allow(clippy::…)]` on the smallest item, with a
  comment saying why. Holzmann: if a tool is confused, rewrite the code instead.
- **Every `#[allow(...)]` states its reason inline**, not just in a comment above it:
  `#[allow(clippy::foo, reason = "invariant: …")]`. **Enforced by:** clippy
  `allow_attributes_without_reason` (Run — fired "`allow` attribute without
  specifying a reason" on `#[allow(dead_code)]` with no reason, promoted to a hard
  error via `-D warnings`; clean once rewritten with `reason = "…"`). It only checks `#[allow(...)]` itself; a
  `#[expect(...)]` or a bare `// allow: …` comment with no attribute is not seen.

## 11. Rust-specific TigerStyle rules

- **Explicitly sized integers.** `u32`/`u64` over `usize`, except where the
  standard library needs `usize` (indexing, lengths). Convert at that boundary
  with `usize::try_from(x)`, not `as` (`as` truncates silently; flagged by
  `as_conversions` / `cast_possible_truncation`).
- **Overflow.** Release builds wrap silently unless `overflow-checks = true`
  (verified: `u32::MAX + 1` gave `0`). Where wrapping is intended, say so with
  `wrapping_add`; where it may fail, `checked_add` and handle the `None`.
  (`clippy::arithmetic_side_effects` exists but fired 71 times on a 70-line
  function of additions, so it's too noisy to turn on.)
- **Division.** `/` on integers floors; use `div_ceil` or `checked_div` to show you
  thought about rounding and zero.
- **Named arguments.** Rust has none, so two same-typed parameters get an options
  struct (`Transfer { source, target }`), the same pattern TigerStyle uses in Zig.
- **Units last:** `latency_ms_max`, constants `BATCH_COUNT_MAX` — Rust's naming
  conventions already match.
- **Large values by reference.** Pass structs over 16 bytes as `&T`; clippy pedantic's
  `large_types_passed_by_value` points them out.

## 12. Testing

- Unit tests in `#[cfg(test)] mod tests`, integration tests in `tests/`.
- Every assert has a test that triggers it and checks its message
  (`#[should_panic(expected = …)]`).
- Mutation testing: `cargo mutants` (Homebrew formula `cargo-mutants`, MIT,
  27.1.0; not installed as of 2026-10-19). Drive surviving mutants to zero.
- `unwrap()` is fine inside tests; the lint table applies `unwrap_used` to all
  targets, so add `#[allow(clippy::unwrap_used, reason = "tests assert via panic")]`
  on the test module.

## 13. Timeouts

`std::process::Command` and `std::net` have no built-in overall timeout. Set
`TcpStream::set_read_timeout` / `set_write_timeout`, and for child processes poll
`try_wait()` in a bounded loop and `kill()` on expiry, rather than calling
`wait()` and hoping.

## 14. Gates

```fish
cargo fmt --check                              # rustfmt; default max_width is 100 (verified)
cargo clippy --all-targets -- -D warnings      # lints from section 0
cargo test
cargo build --release                          # checks the release profile builds
cargo mutants                                  # before pushing
```

## Sources

- Gerard J. Holzmann, "The Power of 10: Rules for Developing Safety-Critical Code",
  NASA/JPL, IEEE Computer, 2006: https://spinroot.com/gerard/pdf/P10.pdf. Quotes
  taken from the transcription in https://github.com/Vhivi/Powerof10-NASA
  (`rules/Powerof10-C.md`, commit `29f6f3975bba9c6d6430f8638d8d561786b04c26`).
- TigerBeetle, `TIGER_STYLE.md` (copy in `upstream/`).
