---
name: tiger-style-python
description: tiger-style rules for Python. Load together with the tiger-style skill whenever writing or reviewing Python code under tiger style.
---

# Python Rules

How TigerStyle maps onto idiomatic Python, in any Python project. `ruff` / `pyright` /
`uv` is shown throughout as one concrete reference toolchain; in another project, use
that project's own formatter, type checker and test runner instead. Adapted from the
Python coding contract in robertguss/programming_tiger_style, with `mypy` replaced by
`pyright` and the mandatory test-first TDD cycle removed in favor of this skill's
test-after rule (see the Testing discipline section of SKILL.md).

Priority order is fixed: **safety > performance > developer experience**. On a forced
tradeoff, choose the higher-priority item.

## 1. Keep control flow simple and analyzable

- Recursion only when the input is finite/acyclic by construction, or with an explicit
  depth/size limit enforced and tested. Prefer iteration.
- Every loop has a clear upper bound, or a documented reason it is intentionally
  non-terminating.
- Prefer explicit branches over dense compound conditions. Split so positive and
  negative cases are both handled.
- `match` over your own `enum` must handle every member; no bare wildcard default that
  silently absorbs a new member. Enforced by: pyright `reportMatchNotExhaustive`,
  **off by default in basic mode** — set it explicitly (`pyrightconfig.json` or
  `pyproject.toml`'s `[tool.pyright]`) or run `typeCheckingMode: strict`, which enables
  it. It misses `match` over anything that isn't a closed literal/enum type (e.g.
  `str`), where a wildcard is required anyway.

## 2. Put explicit limits on work and resources

- Bound retries, queue depth, batch size, and per-request work.
- Bound payload sizes and the memory growth of in-process collections.
- Require explicit timeouts for all external I/O (HTTP, subprocess, DB, file/network).
  Calls like `urllib`/`requests` and `subprocess.run` must pass a timeout.
- Use bounded pools/executors for concurrency.

## 3. Assertions and invariants as design checks

- Assert preconditions, postconditions, and critical invariants. Aim for an average of
  at least two meaningful assertions per non-trivial function.
- Pair assertions across boundaries (e.g. before a write and after the matching read).
- Prefer several simple assertions over one dense compound assertion.
- **`assert` vs exceptions:** use `assert` for *programmer* invariants (conditions that
  are impossible unless there's a bug). Note Python strips `assert` under `-O`, so never
  use `assert` to validate *untrusted runtime input* — for that, raise an explicit typed
  exception. Validate untrusted input at the boundary before use.

## 4. Handle all errors explicitly

- No bare `except:` and no `except Exception: pass`.
- Catch specific exception types and preserve context (`raise ... from err`).
- Never silently swallow exceptions. Enforced by: `ruff` `E722` (bare `except`) and
  `BLE001` (`except Exception`/`except BaseException`), both active under ruff
  0.17.4's default selection (no `extend-select` needed). Neither fires on
  `except SomeSpecificError: pass` — a swallow of a named type is review only.
- Library/engine code raises typed/domain exceptions rather than calling `sys.exit` or
  printing and continuing. CLI boundary code (e.g. Typer commands) may translate those
  into exit codes and user-facing messages.

## 5. Keep state tight and single-sourced

- Smallest possible variable scope; compute close to use sites.
- Avoid duplicated state that can diverge; avoid aliasing the same value into two names.
- Avoid mutable global state unless explicitly justified.

## 6. Keep functions small and cohesive

- **Target 70 lines per function; hard limit 140**, excluding decorators and
  docstring. Prefer functions at or under 70 lines; up to 140 is allowed in
  service of better modularization and separation of concerns (e.g. a cohesive
  line-builder or orchestrator that would be more fragmented, not clearer, if
  split further). Past 140, split.
- One clear responsibility per function.
- Keep branch orchestration in the parent; move pure computation into helpers — e.g. a
  diagnostic command whose detection helpers (`detect_ram`, `detect_gpu`,
  `recommend_tier`) are pure and the command function orchestrates them.

## 7. Explicit types and units

- Type hints required on public functions, methods, and module-level constants.
- Validate untrusted input at boundaries before use.
- Encode units in names, **units last, descending significance**: `timeout_s`,
  `size_bytes`, `latency_ms_max`, `vram_gb` (e.g. `ram_gb`, `vram_gb`).
- Avoid implicit type coercion in critical paths.
- **Top types stay at the boundary.** `Any` only in a parser that takes untrusted input and
  returns a named type. Internal parameters, returns and `dict[str, Any]` values name the
  domain type. Enforced by: `ruff` `ANN401` (Run — fired "Dynamically typed expressions
  (typing.Any) are disallowed" on a `value: Any` parameter; `All checks passed!` once typed
  `int`) for a bare `Any` parameter or return. It misses
  nested `Any` (`-> dict[str, Any]` passes; ruff 0.17.4), so review catches those. Stock
  `pyright` has no `reportAny`; `basedpyright` does but is a second tool, so it is not adopted.
- **Don't launder type evidence.** No `cast(...)` of a value you widened first, no cast chains.
  Enforced by: `ruff` `TID251` banning `typing.cast`. It flags the `from typing import cast`
  line (at the import only) and `typing.cast(...)` at
  its use, but not a bare `cast(...)` after a from-import: that call is caught only through
  the import line. Chains: review. After `import typing`, an allowed call carries a per-call
  `# noqa: TID251 - <reason>`. After a from-import, a noqa on the call line gives `RUF100`
  (unused noqa, ruff 0.17.4), so write the reason in a comment beside the call and put the
  same noqa on the import line; that silences every use of `cast` in that file.
- **Decode once, then branch on the domain value.** Build a dataclass, `TypedDict` or enum at
  the edge; afterwards don't re-probe the raw data's representation. `isinstance` or `match`
  on your own domain classes is fine, and so are invariant `assert`s.
  Enforced by: review.
- **Every bypass states its invariant.** `# type: ignore[code]`, `cast`, `# noqa` each get one
  line of reason. Enforced by: `ruff` `PGH003` (Run — fired "Use specific rule codes
  when ignoring type issues" on a bare `# type: ignore`) requires a code on `# type: ignore` but not a
  reason; the reason is review.

## 8. Avoid dangerous dynamic behavior

- `eval` and `exec` are forbidden in production code.
- No dynamic imports driven by untrusted input.
- **No mutable default arguments** (`def f(x=[])`). Use `None` and build inside.

## 9. Be explicit at call sites

- Don't rely on implicit defaults for correctness- or safety-critical behavior.
- Prefer keyword arguments over ambiguous positional lists (especially two same-typed
  args in a row).
- Consume important return values; don't ignore a result that signals failure.

## 10. Zero-warning policy

Lint, typecheck, and tests pass with zero warnings before a change is done. Example
commands for a `ruff` / `pyright` / `pytest` project:

```bash
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest -q
```

Add to the project's own ruff config (if it has none, say so rather than inventing one):

```toml
[tool.ruff.lint]
extend-select = ["ANN401", "TID251", "PGH003"]

[tool.ruff.lint.flake8-tidy-imports.banned-api]
"typing.cast".msg = "cast discards type evidence; parse, or state the invariant"
```

New warnings block a PR. (The source contract uses `mypy .`; this skill's examples
use `pyright`.)

## 11. Python style specifics

- Line length limit: **100 columns** (TigerStyle) — note if the project's `ruff` config sets
  a different line length, follow the project config and flag the discrepancy rather
  than fighting it.
- `snake_case` for files, variables, functions.
- Use context managers (`with`) for resource lifetimes — files, locks, subprocess
  pipes, network connections.
- Async code must not block the event loop with synchronous I/O or `time.sleep`; offload
  CPU-bound work to a process pool. (N/A for a synchronous project.)

## 12. Testing (this skill's rule — overrides the source contract)

The source contract mandates test-first Red→Green→Refactor with an evidence gate.
**This skill doesn't use that.** Instead:

- Write tests **after** the feature's behavior is complete and its contract is stable —
  not against a half-written feature.
- Tests must be *useful*: verify real behavior and edge cases (valid, invalid,
  boundary, regression), not scaffolding that churns as the feature evolves.
- **No feature is done, and no PR is opened, until its new behavior has such tests.**
  This is the hard gate — at the PR boundary, not per commit.
- Give non-trivial tests a top-of-test comment stating goal and methodology (TigerStyle
  "say how for tests").

## Framework caveat (Typer)

A CLI built on Typer relies on decorators, callbacks, and default values that
TigerStyle's "minimal abstractions / explicit at call site / no reliance on defaults"
rules discourage. Don't fight the framework: apply TigerStyle to the engine and pure
logic, and treat Typer command signatures as a boundary where framework idioms win.
Flag the tradeoff rather than rewriting around Typer.
