# Go Rules

How TigerStyle maps onto Go, with the per-language anti-slop checks from
`principles.md` "Types and boundaries". Every claim below was checked with
`go1.27.4 darwin/arm64` and `golangci-lint 2.15.0` (built with go1.27.4,
Homebrew-adjacent `go install` into a scratch `GOBIN`, macOS, 2026-11-15).

Each rule follows the rule contract in SKILL.md: Default, Banned, Why, Exception, Proof, Enforced by.

Scale to the stakes (SKILL.md, "When to apply"): the full set is for code where a bug
corrupts data or state. For glue and small tools, keep the asserts, bounds and error
checks, and relax the rest.

## 0. Project setup (copy this first)

```yaml
# .golangci.yml
version: "2"
linters:
  default: none
  enable:
    - govet
    - errcheck
    - forcetypeassert
    - nolintlint
    - exhaustive
    - gochecksumtype
    - forbidigo
    - depguard
    - funlen
  settings:
    errcheck:
      check-type-assertions: true   # a := b.(T) with no ", ok" is reported too
      check-blank: true             # n, _ := strconv.Atoi(s) is reported too
    nolintlint:
      require-explanation: true     # //nolint:x with no reason is reported
      require-specific: true        # bare //nolint is reported
    exhaustive:
      default-signifies-exhaustive: false   # a `default:` no longer hides a missing case
    gochecksumtype:
      default-signifies-exhaustive: false   # same, for sealed-interface sum types
    forbidigo:
      forbid:
        - pattern: '^(fmt\.Print(|f|ln)|print|println)$'
          msg: "debug print left in; use the project's logger or delete it"
    depguard:
      rules:
        main:          # name your own quarantined/banned packages here
          deny:
            - pkg: "myproject/internal/legacy"
              desc: "internal/legacy is quarantined; migrate callers instead"
    funlen:
      lines: 70
      statements: 60
```

Verified: with this config, `golangci-lint run` reports a type assertion with no
`, ok`, a blank-identifier-discarded error, a bare `//nolint`, a `switch` over an
enum or a sealed sum type that relies on `default:` to cover a missing variant, a
leftover `fmt.Println`, an import from a denied package, and a function over the
line/statement budget. A misspelled linter name is rejected: `golangci-lint linters`
lists the installed set, and an unknown name in `enable:` fails `golangci-lint run`
before it lints anything.

## 1. Checked type assertions — no `forcetypeassert` violations

- **Default:** `v, ok := x.(T)`; handle `!ok` as an ordinary error, not a crash.
- **Banned:** `v := x.(T)` outside a context where a panic is the correct response
  to a programmer error you already ruled out (e.g. immediately after a `switch
  x.(type)` case that proves the type).
- **Why:** an untyped boundary value (decoded JSON, a `map[string]any`, a plugin
  return value) can be the wrong shape; a bare assertion turns that into a panic
  instead of a handled error.
- **Exception:** a type switch's own matched case — the compiler-checked form.
- **Proof:** point at the `, ok` and the branch that handles `!ok`.
- **Enforced by:** `forcetypeassert` (Run: fired on `raw.(int)`, silent on the
  `, ok` form). `errcheck`'s `check-type-assertions: true` setting flags the same
  shape from the errcheck side — Run, both catch the forced-assertion fixture.
  **Misses:** a type switch's own `.(type)` cases, which are the compiler-checked
  form this rule's Exception allows — neither linter flags those, by design.

## 2. Checked errors, including the blank-identifier form

- **Default:** check every error-returning call: `if err != nil { ... }`, or an
  explicit `_ = call()` **with a one-line reason** when the error truly can't matter.
- **Banned:** `v, _ := call()` or a bare `call()` on anything that returns `error`,
  with no comment.
- **Why:** 92% of catastrophic distributed-system failures trace back to mishandled
  non-fatal errors (principles.md "Errors"); Go's multi-value returns make it easy
  to silently drop the error half.
- **Exception:** `_ = f.Close()` style best-effort cleanup, commented with why the
  error is not actionable there.
- **Proof:** every error-returning call site either checks `err` or has a comment
  on the discard.
- **Enforced by:** `errcheck` with `check-blank: true` (Run: fired on
  `strconv.Atoi`'s dropped error, `os.Create`'s dropped error, and a discarded
  `WriteString` return — all three in one fixture; silent once each is checked).
  **Misses:** the commented `_ = f.Close()` form this rule's Exception allows —
  `check-blank` flags every blank-identifier discard regardless of a comment, so a
  deliberately-ignored error still needs an inline `//nolint:errcheck` to pass, not
  just a comment.

## 3. Every checker bypass states its invariant

- **Default:** `//nolint:specificlinter // invariant: <why this is safe here>`.
- **Banned:** a bare `//nolint` (no linter named, no reason).
- **Why:** this is `principles.md`'s "every checker bypass states its invariant"
  rule, generalized from Rust's `// SAFETY:` and Lua's suppression-reason rule to Go.
- **Exception:** none — if the directive is worth keeping, it's worth one line.
- **Proof:** the comment after `//` states the invariant, not just a restated rule name.
- **Enforced by:** `nolintlint` with `require-specific: true` and
  `require-explanation: true` (Run: a bare `//nolint` is flagged for missing linter
  name; `//nolint:errcheck` with no trailing comment is flagged separately for
  missing explanation — verified both fire independently). **Misses:** the
  reason's quality — any non-empty trailing comment satisfies `require-explanation`,
  so `//nolint:errcheck // fine` passes the linter without actually stating an
  invariant; that's review.

## 4. Exhaustive dispatch over your own enums and sum types

- **Default:** `switch` over a typed int/string enum or a sealed interface lists
  every variant by name. For a sum type, seal the interface with an unexported
  method and declare it: `//sumtype:decl` above `type Shape interface { sealed() }`.
- **Banned:** a wildcard `default:` standing in for an unwritten case over your own
  closed type — adding a variant later then falls through silently.
- **Why:** this is the Go form of `principles.md`'s "non-exhaustive dispatch with a
  wildcard default over your own sum types" anti-slop pattern.
- **Exception:** a `default:` over a type you don't own (e.g. a third-party enum) is
  fine and often required — `exhaustive`'s `ignore-types`/`only-types` scope the check.
- **Proof:** every variant appears as a named `case`; a `default:` present is for
  genuinely unrelated fallback, not for skipped variants.
- **Enforced by:** `exhaustive` for typed-const enums, `gochecksumtype` for sealed
  interfaces, both with `default-signifies-exhaustive: false` (Run: `exhaustive`
  reported "missing cases in expression switch: StatusDone, StatusFailed" on a
  switch with a `default:`; `gochecksumtype` reported "missing cases for Square" on
  a sealed `Shape` interface with a `default:`. Both pass once every case is named).
  **Misses:** a `default:` that is itself a deliberate, documented catch-all for a
  type that may grow without every caller needing a change — that's a real design
  choice, not slop; say so in a comment rather than relying on the linter's silence.

## 5. No debug prints left in

- **Default:** the project's logger, or nothing, on the happy path.
- **Banned:** `fmt.Println`/`fmt.Print`/`fmt.Printf`/bare `print`/`println` in
  non-test, non-`cmd` code.
- **Why:** a stray `fmt.Println("DEBUG ...")` is a classic leftover from an
  exploratory pass — LLM-written code tends to leave these in more than it leaves
  TODOs, because nothing in the build fails on them.
- **Exception:** `cmd/` entry points and tests, where printing is the point.
- **Proof:** no forbidigo hit; intentional CLI output lives in `cmd/` or is behind
  the logger.
- **Enforced by:** `forbidigo` with the default-style pattern restated explicitly in
  section 0 (Run: fired on `fmt.Println("DEBUG total:", total)`; silent once removed).
  **Misses:** forbidigo matches call/selector expressions only — it cannot see a type
  used in a signature (see rule 9); don't reuse it for that.

## 6. Quarantined imports stay quarantined

- **Default:** import the maintained replacement; add a new package to the deny
  list the day it's scheduled for deletion.
- **Banned:** a new import of a package the project has marked legacy/quarantined.
- **Why:** a linter-enforced import ban is cheaper than relying on every reviewer
  remembering which package is being phased out.
- **Exception:** the migration's own commit that deletes the last caller.
- **Proof:** `depguard`'s `deny` list names the package and the reason.
- **Enforced by:** `depguard` (Run: fired on `import "example.com/fixtures/internal/legacy"`
  with the configured reason in the message; silent once the caller uses the
  replacement). The example list in section 0 is a template — name your own
  project's quarantined packages, not `internal/legacy` literally. **Misses:**
  anything not named in the `deny` list — a newly-quarantined package has no
  protection until someone remembers to add it.

## 7. Short functions

- **Default:** 70 lines / 60 statements per function (TigerStyle's limit, slightly
  under Holzmann's "fits on one printed page").
- **Banned:** functions over the hard limit (140 lines, SKILL.md) without a
  modularization reason.
- **Proof / Enforced by:** `funlen` with `lines: 70`, `statements: 60` (Run: fired
  "has too many statements (62 > 60)" on a 61-assignment function; silent once the
  same work is a bounded `for` loop).

## 8. All warnings, every day — `go vet`

- **Default:** `go vet ./...` and golangci-lint's `govet` clean, always.
- **Banned:** ignoring a `go vet` finding (format-verb/argument mismatches, lock
  copying, unreachable code, etc.).
- **Why:** this is Power of Ten rule 10 — compile with every warning on, from day
  one. `go vet` catches bugs that compile cleanly but are wrong at runtime.
- **Exception:** none; `go vet` has no "pedantic" dial to turn down.
- **Proof:** `go vet ./...` exits 0 with empty output (debugging-discipline rule 5 —
  check the output, not just the exit code; a build failure inside `go vet` also
  exits non-zero but prints a different message).
- **Enforced by:** `go vet` directly (Run: `go vet ./fixtures/govet/bad/...` printed
  `fmt.Sprintf format %s has arg count of wrong type int` and exited 1; the fixed
  version exited 0 with no output) and golangci-lint's `govet` wrapper (Run: same
  finding, same fixture, through `golangci-lint run --enable-only govet`).
  **Misses:** `go vet`'s own checks are a fixed set (printf, lock-copying,
  unreachable code, …) — a bug outside that set compiles and vets clean; it is not
  a general correctness checker.

## 9. Top types stay at the boundary — review only

- **Default:** name the domain type in internal parameters, returns, and map
  values. Decode `any`/`interface{}` once at the boundary (a JSON decode, a plugin
  call) into a named type, then pass that type everywhere else.
- **Banned:** `any`/`interface{}` in an internal function signature, `map[string]any`
  as an internal value type, a type alias that just renames `any`.
- **Why:** `principles.md` "Top types belong at the boundary" — the Go form of the
  same rule TypeScript (`unknown`), Python (`Any`) and Rust (`dyn Any`) all need.
- **Exception:** the boundary parser itself; a documented plugin interface where no
  narrower type exists.
- **Proof:** grep the signature list for `any`/`interface{}` outside `internal/*boundary*`
  or the handful of named parse functions.
- **Enforced by: review only.** Verified negative result, not a guess: `forbidigo`
  cannot flag `any`/`interface{}` used as a type (it matches identifier/selector
  *expressions*, not `ast.InterfaceType`/`ast.Ident`-as-type nodes) — probed with
  `func Dump(values []any) []string { ... }` under `forbidigo` alone: 0 issues.
  `asasalint` (pass-`[]any`-as-variadic) also did not fire on the upstream README's
  own worked example under this golangci-lint build — probed, 0 issues, not grounded
  enough to cite as Run. No golangci-lint linter bans a top type in a signature
  wholesale.

## 10. Never launder type evidence — review only

- **Default:** decode to the named type once; keep it.
- **Banned:** widen a known value to `any` and assert it back to its original type;
  chain two type assertions through `any`/`interface{}`.
- **Why:** `principles.md` "Never launder type evidence" — each step discards a
  proof the compiler already had and fakes a new one.
- **Exception:** none found; this is always a smell in Go, where the static type
  was already known before the widen.
- **Proof:** trace the value from its declaration; if it was concretely typed and
  became `any` only to be asserted back, that's the pattern.
- **Enforced by: review only.** `unconvert` only removes *redundant same-type*
  conversions (its own description), not suspicious widen-then-assert chains; no
  other golangci-lint linter targets this shape.

## 11. External I/O gets a bound

- **Default:** `context.WithTimeout`/`WithDeadline` on every outbound call
  (HTTP client, subprocess, DB query); pass that `ctx` through, don't build a
  fresh `context.Background()` deeper in the call chain.
- **Banned:** an HTTP client, `exec.Command`, or DB call with no deadline reachable
  from the caller.
- **Why:** Go has no implicit timeout (unlike some languages' socket defaults);
  without one, a hung dependency hangs the caller forever.
- **Exception:** a genuinely unbounded long-poll, with the bound moved to a
  surrounding supervisor instead.
- **Proof:** point at the `context.With*` call and where the resulting `ctx` is passed in.
- **Enforced by: review only.** `contextcheck` exists in golangci-lint's catalog
  and checks for non-inherited contexts, not for a missing deadline — not cited as
  Run for this rule; the deadline question itself has no mechanical check.
  **Misses:** everything — this is entirely a review-only rule, no partial
  coverage from any installed linter.

## 12. Testing

- Unit tests in `_test.go` files beside the code; `t.Run` sub-tests per case.
- Every assertion-equivalent error path gets a test that triggers it.
- Mutation testing: no mature, actively maintained Go mutation tool was installed
  or run in this pass (`go-mutesting` exists upstream but was not probed) — do a
  manual mutation pass per new branch (flip a boundary, change a constant) until a
  tool is adopted, per SKILL.md's mutation gate.

## 13. Gates

```fish
gofmt -l .                      # empty output required; or `goimports -l .`
go vet ./...                    # empty output required (rule 8)
golangci-lint run               # the linters in section 0
go test ./...
go build ./...                  # a hallucinated/renamed API fails here first
```

## Sources

- Gerard J. Holzmann, "The Power of 10", NASA/JPL, IEEE Computer, 2006 (quoted at
  length in `rust-rules.md`; the same rules translate above).
- `golangci-lint` docs and `.golangci.reference.yml`, settings verified against the
  installed 2.15.0 build, for `errcheck`, `nolintlint`, `exhaustive`,
  `gochecksumtype`, `forbidigo`, `depguard`, `funlen`.
- `alecthomas/go-check-sumtype` README for the `//sumtype:decl` sealed-interface
  pattern `gochecksumtype` relies on.
- Types-and-boundaries principles: dmmulroy/anti-slop (MIT) ideas, restated in our
  own words per `principles.md` and `SKILL.md`'s provenance note; no code copied.
