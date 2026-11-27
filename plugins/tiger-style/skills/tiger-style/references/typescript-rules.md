# TypeScript Rules

How TigerStyle's "Types and boundaries" principles (`principles.md`) map onto
TypeScript. Read this instead of `rust-rules.md` when the target is TypeScript.
Every rule here was checked on `typescript@6.0.3`, `eslint@10.15.0`,
`typescript-eslint@8.72.2` (npm, a scratch project, 2026-11-15).

Each rule follows the rule contract in SKILL.md: Default, Banned, Why, Exception, Proof, Enforced by.

Scale to the stakes (SKILL.md, "When to apply"): the full set is for code where a bug
corrupts data or state. For glue and small scripts, keep the boundary-parsing and
error-handling rules and relax the rest.

## 0. Project setup (copy this first)

```jsonc
// tsconfig.json
{
  "compilerOptions": {
    "strict": true,
    "noUncheckedIndexedAccess": true,
    "noImplicitOverride": true,
    "noFallthroughCasesInSwitch": true
  }
}
```

```js
// eslint.config.js
const tseslint = require("typescript-eslint");
module.exports = tseslint.config({
  plugins: { "@typescript-eslint": tseslint.plugin },
  languageOptions: {
    parser: tseslint.parser,
    parserOptions: { project: "./tsconfig.json" },
  },
  rules: {
    "no-empty": "error",
    "@typescript-eslint/no-explicit-any": "error",
    "@typescript-eslint/no-unsafe-argument": "error",
    "@typescript-eslint/no-unsafe-assignment": "error",
    "@typescript-eslint/no-unsafe-call": "error",
    "@typescript-eslint/no-unsafe-member-access": "error",
    "@typescript-eslint/no-unsafe-return": "error",
    "@typescript-eslint/no-unsafe-type-assertion": "error",
    "@typescript-eslint/no-non-null-assertion": "error",
    "@typescript-eslint/no-unnecessary-type-assertion": "error",
    "@typescript-eslint/ban-ts-comment": ["error", { "ts-ignore": "allow-with-description" }],
    "@typescript-eslint/switch-exhaustiveness-check": "error",
    "@typescript-eslint/no-floating-promises": "error",
  },
});
```

Verified: with this setup, `any` in an internal signature, an unchecked `as` cast, a
bare `!`, an undescribed `@ts-ignore`, a non-exhaustive `switch` over a literal union,
and an unawaited promise each fail the lint; `noUncheckedIndexedAccess` makes
`arr[0]` carry `| undefined` so using it unchecked fails `tsc`. A misspelt rule name
fails with "could not find plugin"/"Definition for rule … was not found", so a typo
here can't silently do nothing. Every rule name below was confirmed present in
`node_modules/@typescript-eslint/eslint-plugin/dist/rules/` before use.

## 1. Top types belong at the boundary

`principles.md` "Types and boundaries": a top type (`any`, `unknown`, `object`) is
allowed only where untrusted input enters — a parser, a deserializer, an error's
`cause`. Internal parameters, returns, and dictionary values name the domain type.

- **Default:** every function parameter and return type names a concrete interface,
  type alias, or union. A boundary parser takes `unknown` and returns the named type.
- **Banned:** `any` anywhere in application code; `object` or an open `Record<string,
  any>` as a parameter or return type past the boundary.
- **Why:** `any` and casual `object` opt the value out of checking for every caller
  downstream, not just at the point written.
- **Exception:** a boundary parser's own input parameter (`function parseConfig(text:
  string): Config`, where the raw JSON/HTTP body is handled inside, not the
  parameter), and generic-library code genuinely polymorphic over any type.
- **Proof:** point at the named type in the signature, or at the one parser function
  that takes the untyped input.
- **Enforced by:** `@typescript-eslint/no-explicit-any` (Run — fired 3× on a
  `weights: any` parameter and its untyped `.reduce` callback in
  `no-explicit-any.bad.ts`; clean once typed `number[]`); `no-unsafe-return`,
  `no-unsafe-call`, `no-unsafe-member-access` (Run — each fired on the same
  fixture and again on `no-unsafe-member-access.bad.ts`'s `input: any` chain;
  clean on both typed versions); `no-unsafe-assignment` (Run — fired on
  `no-unsafe-assignment.bad.ts`'s `parseJson(text): any` assigned into a typed
  `const`; clean once decoded via a `value is Config` type guard);
  `no-unsafe-argument` (cited, not run — enabled in the same config but no
  fixture isolated it from the rules above).
  Misses: none of these fire on `unknown` or `object` parameters — typescript-eslint
  has no standard rule banning those two outright (only the vendored
  `dmmulroy/anti-slop` Oxlint plugin does, not installed here). Banning `unknown`/
  `object` specifically on internal signatures is review only.

## 2. Never launder type evidence

Don't widen a value the checker already knows and cast it back; don't chain casts
through a wide type. Each one deletes a proof and then fakes one.

- **Default:** narrow with a type guard (`value is Domain`) backed by a real runtime
  check, or a discriminated-union `switch`.
- **Banned:** `x as T` where `T` narrows past what the checker can verify (`Animal as
  Dog` with no check); `x as unknown as T` or any two-`as` chain.
- **Why:** a cast is a claim with no proof attached; a chain hides which claim failed
  when it's wrong.
- **Exception:** a cast whose target genuinely is a supertype (widening), or a literal
  `as const`.
- **Proof:** point at the type guard's runtime check, or the exhaustive `switch`.
- **Enforced by:** `@typescript-eslint/no-unsafe-type-assertion` (Run — fired
  "Unsafe type assertion: type 'Dog' is more narrow than the original type" on
  `animal as Dog`; clean on an `in`-operator type guard); `no-unnecessary-type-assertion`
  (Run — fired on `(message as string).toUpperCase()` where `message` was already
  `string`; the inverse smell: a cast kept out of habit, not need). Misses: a cast chain through `unknown` or
  `any` first can dodge `no-unsafe-type-assertion`, because each individual step can
  look like a widen or a same-type cast (confirmed: no typescript-eslint rule targets
  the chain shape). Cast chains are review
  only.

## 3. Decode once at the boundary, then branch on the domain value

Untrusted input becomes a named type in one place. Past that place, don't re-probe
its raw representation.

- **Default:** one parser/type-guard per external shape; every caller after that
  branches on the resulting domain type (`switch`, `instanceof`, a type-predicate
  check on your own classes).
- **Banned:** asserting a value is non-null with `!` instead of checking; re-running
  `typeof`/structural checks on data already decoded.
- **Why:** re-probing after decoding means the "decoded" step proved nothing — every
  caller re-derives its own belief about the shape instead of trusting one.
- **Exception:** `!` is fine where the language can't express the invariant the
  programmer already proved two lines above (rare; prefer a narrowing `if`).
- **Proof:** point at the one decode site; show that every later `typeof`/`in` check
  is on the program's own domain type, not the raw external value.
- **Enforced by:** `@typescript-eslint/no-non-null-assertion` (Run — fired
  "Forbidden non-null assertion" on `users.get(id)!`; clean once rewritten as an
  explicit `undefined` check that throws instead of asserting). Misses: it can't tell a one-line-proven
  `!` from a lazy one — that split is review. Re-probing past the boundary has no
  dedicated lint (`no-runtime-typeof` exists only in `dmmulroy/anti-slop`): review only.

## 4. Every checker bypass states its invariant

A cast, a non-null `!`, a `@ts-ignore`/`@ts-expect-error`: one line beside it says why
the checker is wrong here. A rule code alone is not a reason.

- **Default:** `// @ts-expect-error invariant: <why>` with the actual reason.
- **Banned:** `@ts-ignore`/`@ts-expect-error` with no description, or a description
  that just repeats the error code.
- **Why:** the comment is what a reviewer six months later has instead of your head.
- **Exception:** none — every directive gets a reason, however short.
- **Proof:** point at the description text.
- **Enforced by:** `@typescript-eslint/ban-ts-comment` with `{ "ts-ignore":
  "allow-with-description" }` (Run — fired "Include a description after the
  @ts-ignore directive..." on an undescribed `// @ts-ignore`; clean on
  `// @ts-expect-error invariant: ...` with a stated reason; requires ≥3
  characters after the directive). Misses:
  it only checks length, not whether the reason is true or specific — "whatever" (3+
  chars) passes. Quality of the reason is review.

## 5. Exhaustive dispatch over sum types

`switch` over your own union or discriminated type writes out every case; no
wildcard `default` papers over a missing one.

- **Default:** every literal in the union gets its own `case`; adding a union member
  later is a compile/lint failure everywhere it's switched on, not a silent fallback.
- **Banned:** a `default:` that swallows unmatched cases on a union you own.
- **Why:** AI-written dispatch code reaches for `default` reflexively; that's exactly
  where a new variant goes unhandled.
- **Exception:** a `default` is fine on a type you don't own (e.g. a third-party enum
  that may grow), with a comment saying why.
- **Proof:** point at the switch; every member of the union has a case.
- **Enforced by:** `@typescript-eslint/switch-exhaustiveness-check` (Run — fired
  'Switch is not exhaustive. Cases not matched: "failed"' on a switch missing one
  of three literal-union cases; clean once all three are written out). Misses: it only
  fires on `switch`, not an `if`/`else` chain doing the same dispatch by hand — review
  for those.

## 6. Handle every error

Nothing swallowed; a dropped promise or an empty `catch` is a bug pretending to be
handled code.

- **Default:** `await` every promise whose result or failure matters, or mark it
  `void foo()` with a reason it's genuinely fire-and-forget; every `catch` does
  something (log, rethrow, return a typed error).
- **Banned:** a floating (unawaited, unhandled) promise; `catch {}`/`catch (e) {}`
  with an empty body.
- **Why:** 92% of catastrophic failures in distributed systems come from mishandled
  non-fatal errors (`principles.md` "Errors"); an unawaited promise's rejection is
  exactly that, one `async` away from a silent crash-free failure.
- **Exception:** a logging/metrics call you deliberately don't want to block on —
  mark it `void call()` to say so explicitly.
- **Proof:** point at the `await`/`void`, or at the `catch` body's handling.
- **Enforced by:** `@typescript-eslint/no-floating-promises` (Run — fired
  "Promises must be awaited..." on `save(id);` called without `await` from a
  non-async function; clean once awaited); core ESLint `no-empty` (Run — fired
  "Empty block statement" on `catch { }`; clean once the catch body handles the
  error). Misses:
  `no-floating-promises` only sees expressions whose static type is Promise-like — a
  callback-style async API with no Promise wrapper isn't covered. `no-empty` doesn't
  check that a non-empty `catch` body actually does something useful (a `catch (e) {
  }` with only a comment still counts as non-empty to the rule); that's review.

## 7. `tsc` strict flags are part of the gate, not optional

- **Default:** `strict: true` plus `noUncheckedIndexedAccess: true` in every
  `tsconfig.json` (section 0).
- **Banned:** disabling a strict flag project-wide to silence errors instead of
  fixing them.
- **Why:** `noUncheckedIndexedAccess` is off by default even under `strict`, and
  without it `arr[i]` types as the element type with no `undefined`, so an
  out-of-range read type-checks and crashes at runtime instead of at compile time.
- **Exception:** a narrow local disable only when a tuple/array's bound is already
  proven (e.g. just checked `.length`), stated in a comment.
- **Proof:** point at `tsconfig.json`'s `compilerOptions`.
- **Enforced by:** `tsc --noEmit` (Run — fired `error TS18048: 'first' is possibly
  'undefined'.` on an unchecked `items[0]` read under `noUncheckedIndexedAccess`;
  clean once the `undefined` case is checked first) failing on a strict
  violation. Misses: `tsc` only catches what the type system can express; a logically
  wrong but well-typed value passes clean — that's what the assertion and testing
  rules in `principles.md` are for.

## Gates

```fish
npx tsc --noEmit
npx eslint .
```

## Sources

- `principles.md` "Types and boundaries" (the language-neutral statement these rules
  implement).
- typescript-eslint rule docs: https://typescript-eslint.io/rules/ (rule names
  confirmed against the installed plugin, not the docs site).
- Ideas restated, not copied, from dmmulroy/anti-slop (MIT) — see SKILL.md
  "Provenance and licensing" for what was and wasn't carried.
