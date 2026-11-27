# Fish Rules

How TigerStyle maps onto fish shell scripts and functions. Read this instead of
`python-rules.md` when the target is a `.fish` file. Verified on fish 4.9.3
(macOS, 2026-10-19). The worked examples come from one fish function, `pk`.

**Write fish, not bash.** Fish has a native answer for nearly every reason to
reach for bash. Don't drop into `bash -c` or a `.sh` file because a bash idiom
comes to mind first — look it up in the table below. Fall back to bash only when
fish genuinely has no equivalent, and say which feature forced it.

## 1. Bash habit → fish equivalent

| Bash habit | Fish | Notes |
|---|---|---|
| `cat <<EOF ... EOF` (heredoc) | a multi-line quoted string, or `printf '%s\n' 'line 1' 'line 2'` | Quotes span newlines. See section 2. |
| `x=1` | `set x 1` (`-l` local, `-g` global, `-x` exported) | `x=1` is a syntax error. |
| `${HOME}` | `$HOME`, or `{$HOME}` to separate it from following text | `${…}` is a syntax error. |
| `VAR=val cmd` | same | Works since fish 3.1. |
| `$(cmd)`, `` `cmd` `` | `(cmd)` or `$(cmd)`; inside double quotes only `"$(cmd)"` | Backticks don't work. |
| `&&`, `\|\|` | same, or `; and` / `; or` | Both work. `or return 2` reads well after an assert. |
| `[[ … ]]`, `[ … ]` | `test …` | There is no `[[`. |
| `$?` | `$status`; `$pipestatus` for every stage of a pipe | `false \| true; echo $pipestatus` → `1 0`. |
| `set -e` / errexit | `cmd; or return N` at each step that can fail | There is no errexit; every failure is handled where it happens. |
| `trap … EXIT` | `function _cleanup --on-event fish_exit` | `--on-signal INT` for Ctrl-C. |
| `<(cmd)` | `(cmd \| psub)` | Gives a temp file path; works with `diff`. |
| `$(( 7 / 2 ))` | `math 7 / 2` → `3.5`; `math "floor(7 / 2)"` → `3` | `math` is floating point, so round on purpose. |
| `sed` / `awk` for small text jobs | `string replace [-r] [-a]`, `string split`, `string match -r` | `string match -r` also prints capture groups. |
| `getopts` | `argparse` | Puts flags in `_flag_*` and leaves only the positional arguments in `$argv`. |
| `grep -q x <<< "$s"` | `string match -q -- '*x*' $s` | No subprocess. |
| `[[ " ${a[@]} " =~ " x " ]]` | `contains -- x $a`; `contains -i` gives the index | |
| `${#a[@]}` | `count $a` | |
| `${a[@]:1}` | `$a[2..-1]` | Lists are 1-indexed. |
| `which x` / `command -v x` | `command -q x` | Quiet test for "is it installed". |
| `$BASH_SOURCE` | `status current-filename`; `functions --details f` for a function's file | `status fish-path` is the running fish binary. |
| `source file` | same | |
| `local IFS=…; read` | `string split SEP`, `read --delimiter` | Fish never word-splits variables. |

## 2. Heredocs and commit messages

Fish has no `<<EOF`. The fish docs (fish_for_bash_users) suggest `printf`, or
a quoted string that spans lines, because quotes continue across newlines. A
heredoc is just "this text on stdin", which is the same as piping into the
command.

Three ways to write a multi-line git commit message, in order of preference:

```fish
# 1. One printf argument per line, piped to git's stdin. Every line is its own
#    argument, so each can use whichever quotes suit it. Most robust.
printf '%s\n' \
    'fish: add pk, a process-name lister for pkill' \
    '' \
    "Filters with -o for any-of-several words. It's tested by pk --test." \
    '' \
    'Refs: #12' | git commit -F -

# 2. Repeated -m: git turns each one into its own paragraph, with a blank line
#    between them. Good for subject + one or two body paragraphs.
git commit -m 'fish: add pk' -m 'Body paragraph.' -m 'Refs: #12'

# 3. One quoted string spanning lines. Shortest, but the quoting rules below
#    apply to the whole message at once.
git commit -m 'fish: add pk

Body line one.
Body line two.'
```

Quoting rules that bite in all three:
- **Single quotes**: nothing expands, but an apostrophe must be written `\'`
  (`'It\'s'`). `\\` is a backslash; every other backslash is literal.
- **Double quotes**: `$var` and `$(cmd)` expand, so write a literal dollar as
  `\$`. Inside double quotes `\n` is NOT a newline; use a real line break.
- Never build the message with `echo -e` or `\n` escapes; use real line breaks
  or one `printf` argument per line.

## 3. Assertions

Fish has no `assert`. Use one small helper per script; `pk`'s `_pk_assert`
is the pattern:

```fish
function _name_assert -d "Fail loudly when an invariant breaks"
    if not $argv[2..-1]
        echo "name: assertion failed: $argv[1]" >&2
        return 2
    end
end

_name_assert "launchd (PID 1) is missing from the list" contains -- launchd $names
or return 2
```

- A function can't stop its caller, so every assert is followed by
  `or return 2`. Status 2 means "bug in this script", kept distinct from
  status 1, "bad input or nothing found", so tests can tell them apart.
- The command runs from a variable, which works for builtins and `test`, but
  not for keywords: `not`, `and`, `or`, `begin`, `if` fail with "The expanded
  command is a keyword". Negate with `test !` or `test (count …) -eq 0`.
- Asserts are for the script's own invariants. Bad user input gets a plain
  error message and `return 1`, never an assert: same split as Python's
  `assert` vs typed exceptions.
- Give every assert a distinct message, and have tests check that message.
  Two asserts can fire on the same broken input (an empty list also lacks
  `launchd`). If the tests only check the status, one assert hides the other
  and a mutation that removes it survives.
- Split compound conditions: `test a; and test b` in one `if` can't say which
  failed.
- "At least two asserts per function" doesn't fit 3-line shell helpers. The
  fish rule is: every invariant has an assert, and every assert is shown to
  fire (see section 6).

## 4. Gotchas that cause silent bugs

Each of these was hit or checked while building `pk`:

- **`(cmd)` splits its output on newlines** into list items. To keep text whole
  (a file, a multi-line message, generated code) end the pipe with
  `string collect`; add `-N` to keep trailing newlines.
- **`printf '%s\n'` with an empty list still prints one newline.** Guard with
  `if test (count $list) -gt 0`. Otherwise an "empty" list turns into one
  blank line.
- **A range whose start is past its end runs backwards**: with `a = 1 2 3`,
  `$a[3..1]` is `3 2 1`. When either side of a split may be empty, loop
  instead of slicing. (`$argv[3..-1]` with only 2 items is empty, which is
  safe.)
- **`"$list"` joins items with spaces**, while `$list` passes each item as its
  own argument. Use the unquoted form when passing words on; quote only when
  you want one string.
- **No word splitting**: `set x 'a b'; cmd $x` passes one argument. This is the
  reason most bash quoting defences aren't needed. Don't add them.
- **Fish ships wrapper functions for common commands**, e.g. `grep` →
  `command grep --color=auto`. Inside scripts call `command grep`,
  `command ps`, `command sort` so the behavior doesn't depend on wrappers or
  the user's aliases.
- **A word that starts with `-`** gets read as a flag by `argparse` and by most
  commands. Pass `--` before user words (`grep -e $word`, `string match -- …`).
- **`math` is floating point**: `math 7 / 2` is `3.5`. Use `floor()` /
  `round()` explicitly when you need an integer.
- **`set` keeps the status of a command substitution**:
  `set -l out (cmd); set -l rc $status` captures `cmd`'s status. Save it on
  the very next line, because any other command overwrites it.

## 5. Limits and timeouts

- Every `for` loop runs over a finite list (`$argv`, a command's output, a
  fixed table), so it's bounded by its input. `while` loops need a counter
  with a maximum.
- Fish has no built-in timeout. `timeout` comes from GNU coreutils
  (`/opt/homebrew/bin/timeout` here) and is not part of base macOS. Use it for
  child processes that could hang, wrapped in `command -q timeout`, or accept
  the risk and write down why.

## 6. Testing pattern

- `name --test` runs the script's own tests. Helper functions inside a file
  only exist after the main function has autoloaded, so a flag on the main
  function is the reliable way in.
- Fixture tests run in a throwaway fish:
  `VAR=1 (status fish-path) --no-config -c 'source $FILE; and _name_test_fixtures'`.
  Swapping out a data-source helper (e.g. `_pk_names`) there can't leak into
  the user's shell.
- Each case states the exact expected status, every output line, the line
  count (no lines and one blank line both join to ""), and the exact error
  message. When no error is expected, the case still checks the message
  is empty.
- Mutation test: copy the file to a temp path, replace one snippet, which must
  appear exactly once in the code above the Testing heading or the run stops,
  then rerun the fixture tests on that copy and expect them to fail. Run it
  only after the fixture tests pass on the real code.
- A double-check assert that only fires when something else is already broken
  (pk's re-check of grep's output) can't be killed by any outside test. Leave
  it out of the mutation list, and say so in a comment.
- Clean up temp files in a `--on-event fish_exit` handler so Ctrl-C doesn't
  leave them behind.

## 7. Gates (fish's version of ruff / pyright / pytest)

```fish
fish --no-execute file.fish   # syntax check, runs nothing
fish_indent --check file.fish # formatting; fish_indent -w file.fish fixes it
name --test                   # the script's own fixture + mutation tests
```

All three pass before a change is done. Line limit is 100 columns, as in
TigerStyle generally.

## 8. Comments

Follow the project's existing comment style over TigerStyle's "why, not what".
In one fish config this meant the banner header, `Function:` /
`Description:` / `Arguments:` blocks, short `====` section headings inside
functions, and comments that explain each flag of a command in plain words:
the aim is every line understood, not just the non-obvious ones. A project's own comment style wins over this.
