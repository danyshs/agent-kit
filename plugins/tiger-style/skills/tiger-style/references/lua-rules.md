# Lua Rules (Neovim)

How TigerStyle maps onto Lua as it runs inside Neovim: config code, plugins and
the headless scripts that test them. Read this instead of `python-rules.md` when
the target is Lua. Every behaviour named here was checked on Neovim
`v0.13.0-dev-1952` (bob nightly, LuaJIT), stylua 2.6.0 and lua-language-server
3.19.2, macOS, 2026-10-30. Where a project has its own STYLE.md, it wins (SKILL.md, "Find the
project's conventions").

Each rule follows the rule contract in SKILL.md: Default, Banned, Why, Exception, Proof, Enforced by.

Scale to the stakes (SKILL.md, "When to apply"): an editor config is UI and glue,
so keep the asserts, bounds, error handling and gates, and relax the allocation
rules. A plugin or script that writes user files or runs git gets the full set.

## 0. Project setup (copy this first)

```toml
# .stylua.toml
indent_type = "Spaces"
indent_width = 2            # the Neovim/Lua convention; flag it, don't fight it
column_width = 120          # or 100, TigerStyle's default, if the project has none
quote_style = "AutoPreferDouble"
call_parentheses = "Always"
syntax = "LuaJIT"
```

`.luarc.json`, with the nightly's own runtime as the library so `vim.*` is typed:

```json
{
  "runtime.version": "LuaJIT",
  "workspace.library": ["${env:HOME}/Library/Application Support/bob/nightly/share/nvim/runtime/lua"]
}
```

On Linux the bob runtime lives under `~/.local/share/bob/nightly/...`. Verified:
without the library line lua-language-server reports `vim` as an undefined global;
with it, a clean tree reports "no problems found".

## 1. Assertions vs handled errors

- **Default:** `assert(condition, "module.fn: what is wrong")` for the program's own
  invariants (an argument it controls, a state that cannot happen). `vim.validate(
  "name", value, "type")` at the top of a function that takes arguments. Bad input
  from outside (a user's file, a missing executable, a failed `git push`) gets
  `vim.notify(msg, vim.log.levels.ERROR)` and an early `return`, or a returned
  `nil, err` for the caller to report.
- **Banned:** an `assert` on something the user wrote or the system did. Example
  found in review: `assert(#lines <= LINES_MAX)` on a hand-edited `edit.txt`; an
  oversized file crashed the command instead of being reported.
- **Why:** Lua never strips `assert` (LuaJIT has no `-O`), so an assert on outside
  input turns an operating condition into a crash with a stack trace.
- **Exception:** none. If the check can fail in normal use, it is not an assert.
- **Proof:** for each `assert`, name who controls the value. A test that drives the
  bad-input path and checks for a returned error, not a throw (`pcall` the call,
  expect `ok == true` and a nil result).
- **Enforced by:** review.

Gotchas, verified:
- `assert(v, msg)` returns **both** `v` and `msg`. `f(assert(x, "m"))` passes two
  arguments, and an API that takes exactly one fails with "Expected 1 argument".
  Assign first: `local x_checked = assert(x, "m")`, then call.
- `vim.validate` with a wrong type throws; it is an assertion, not input
  validation for user data.
- Constants that depend on each other are asserted once at module load, not on
  every call. Example: `assert(ANIM_FRAMES_MAX * ANIM_PERIOD_MS >= COMBO_TIMEOUT_MS)`
  would have caught an animation cap that froze power mode's bar at 60%.

## 2. Errors: nothing dropped

- **Default:** check what a call can fail on before calling it
  (`nvim_buf_is_valid`, `nvim_win_is_valid`, `vim.fn.maparg(lhs, mode) ~= ""`,
  `vim.fn.bufexists(name) == 0`). Where `pcall` is the only way (a `vim.fn` call
  that raises, an optional `require`), use both results: `local ok, err =
  pcall(...)` and act on `not ok`.
- **Banned:** a bare `pcall(...)` statement, which drops the failure, and a comment
  saying "ignore that" in its place.
- **Why:** a dropped `pcall` hides the error you did not expect along with the one
  you did. Example: `pcall(nvim_buf_set_name, buf, name)` silently left a review
  buffer unnamed whenever a buffer of that name existed (E95).
- **Exception:** none found yet. A blanket `pcall` was never the smallest correct
  option once the expected failure was named.
- **Proof:** `grep -rnE '^\s*pcall\(' lua/` finds nothing. For each guard that
  replaced a pcall, a test that hits the "already gone" case.
- **Enforced by:** the grep above, as part of the gate.

## 3. Bounds: loops, timers, shell-outs

- **Default:** `for i = 1, n` and `ipairs` over a table whose size is capped. A
  wait on the event loop is `vim.wait(ms, condition, interval)`. Every shell-out
  is `vim.system(argv, opts):wait(timeout_ms)` or `vim.system(argv, { timeout =
  ms }, on_exit)`, with the exit code checked and stderr in the error message.
  Timers (`vim.uv.new_timer`) are stopped and closed on every exit path, and a
  `VimLeavePre` autocmd cleans up anything still running.
- **Banned:** `while true`, `repeat ... until` without a counter; `vim.fn.system`,
  `io.popen`, `os.execute`; `vim.system(...):wait()` with no timeout.
- **Why:** an unbounded wait freezes the editor or hangs a headless test run.
- **Exception:** none. A loop that truly runs forever does not belong in a config.
- **Proof:** name each bound (the constant, with its unit last: `GIT_TIMEOUT_MS`).
  A test that drives the timeout path where it matters.
- **Enforced by:** review, plus a grep for the banned calls.

Verified: both `:wait(100)` and `{ timeout = 100 }` on a two-second command return
`code = 124`. `:wait()` kills the process with signal 9; `opts.timeout` sends TERM.
Treat 124 as "timed out", and name the timeout in the message.

A bound must also do work (principles.md). Example: power mode's animation was
capped at 200 frames (4 s) under a 10 s decay, so the bar it drew stopped
draining at 60%. Bounded, and wrong.

## 4. Exit 0 is not proof

- **Default:** when a command's point is a file, check the file after exit 0:
  it exists, it is non-empty, and it is valid (ffprobe reports a duration).
  When a gate's pass condition is "no output", capture stdout and stderr and
  assert they are empty.
- **Banned:** treating `nvim --headless +qa`'s exit code as a startup check.
- **Why:** verified: `nvim --headless -c 'lua error("x")' +qa` exits **0**. An
  `init.lua` that throws on every launch passes an exit-code check.
  (`nvim -l script.lua` does exit 1 on an uncaught error, and `os.exit(n)`
  exits `n`.)
- **Exception:** none.
- **Proof:** the gate script's line that captures the output and tests it is
  empty, e.g. fish `set out (cmd 2>&1); test -z "$out"`.
- **Enforced by:** the gate script.

Related: ffmpeg can exit 0 and a full decode can report nothing while the output
is wrong (a 48 kHz clip joined after a 44.1 kHz one played at the wrong rate).
Check the property you need (sample rate, duration), not just "it decoded".

## 5. Types: LuaLS annotations, zero warnings

- **Default:** `---@param name type`, `---@return type name` on every function,
  `name?` for optional parameters, `---@class` for a table shape that crosses a
  file. `lua-language-server --check . --checklevel=Hint` reports "no problems
  found".
- **Banned:** `---@diagnostic disable` in config or plugin code. In tests, only a
  line-scoped `duplicate-set-field` where a stub replaces an API function.
- **Why:** LuaLS reads the annotations, so hover shows them and the checker
  catches wrong calls before a restart.
- **Exception:** a suppression scoped to one line, with the reason beside it,
  when the checker is provably wrong there. Prefer rewriting the line so it
  passes (`markers and markers[1]` instead of suppressing `need-check-nil`).
- **Proof:** the check's final line.
- **Enforced by:** lua-language-server in the gate.

## 6. Function shape and naming

- **Default:** target 70 lines, hard limit 140. Parents branch, helpers compute.
  A helper with one caller is named after it (`on_attach_toggles`). `snake_case`;
  units last (`timeout_ms`, `duration_ms_max`); no abbreviations.
- **Banned:** a function past 140 lines; module-level globals (everything is
  `local`; module state lives in one table).
- **Why:** as principles.md. In Lua, `function name()` without `local` defines a
  global. A function that calls a `local` helper defined further down the file
  is really calling a global of that name, which is nil: verified, it fails
  with "attempt to call global 'helper' (a nil value)", and only when that line
  runs. Define helpers above their callers; lua-language-server flags the
  undefined global.
- **Exception:** a declarative table builder (a statusline component list) may
  pass 70 with a reason; never 140.
- **Proof:** a treesitter scan of function spans (Lua parser:
  `(function_declaration) (function_definition)`), listing anything over 70.
- **Enforced by:** review, with that scan.

Off-by-one: Lua tables are 1-indexed, so a 0-based frame number indexes as
`OFFSETS[frame + 1]`. Name which one a variable is (`frame` vs `index`).

## 7. Tests (headless Neovim)

- **Default:** one spec per feature, run as `nvim --headless -i NONE -n -l
  spec.lua`. Each case prints `ok` or `FAIL`, counts failures, and the spec ends
  with `os.exit(failures > 0 and 1 or 0)`. A spec that needs the whole config
  loads it first (e.g. a `-- runner: full-startup` marker the runner reads) and
  is wrapped in `pcall(dofile, spec)` so a crash exits 1.
- **Isolation:** each spec gets its own `XDG_DATA_HOME`, `XDG_STATE_HOME` and
  `XDG_CACHE_HOME`. If plugins live under the data folder, symlink its `site`
  into the scratch data folder. A spec that writes to the data folder checks a
  marker the runner sets and refuses to run without it.
- **Banned:** a spec that writes to the user's real data. Found in review: a
  runner that isolated state and cache but not data let three specs overwrite
  the user's NeetCode progress and append test bans on every run.
- **Why:** a test suite runs often; a side effect on real data compounds.
- **Exception:** none.
- **Proof:** the real files' modification times before and after a suite run.
- **Enforced by:** the runner, and the guard in each data-writing spec.

UI-only behaviour: some things only happen with a UI attached. Verified: Neovim
pulls a float that overflows the editor back on screen with a TUI attached, and
not headless. Test the state headless; check the behaviour once in a pty
(`script -q /dev/null nvim --clean -c "luafile probe.lua"`, with the probe
writing its result to a file) and record it.

## 8. Mutation testing

Mutate a **copy** of the project, never the working tree: copy it to a scratch
folder, then for each mutant read the file in Lua, check the target string
occurs exactly once, replace it with `string.find(..., 1, true)` (plain, no
patterns), run the spec with `nvim --clean` (so modules come from the copy, not
the runtimepath), and restore. Shell `sed` on Lua source is banned: escapes and
patterns corrupt it silently and give false survivors.

A survivor means a missing test, except a backstop that only matters when other
code is already broken (for example a frame cap that a decay reset always
pre-empts). Name that one, with the reason, in the spec header.

## Gates

```fish
stylua --check .                                   # format
lua-language-server --check . --checklevel=Hint    # types and lint: "no problems found"
set out (env NVIM_APPNAME=<app> <nvim> --headless +qa 2>&1)
test $status -eq 0; and test -z "$out"             # startup: exit 0 AND no output
grep -rnE '^\s*pcall\(' lua/                       # expect nothing (rule 2)
env NVIM_APPNAME=<app> <nvim> -l tests/run_all.lua # whole suite: exit 0
```
