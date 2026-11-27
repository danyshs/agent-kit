# tiger-style

TigerBeetle's TigerStyle for Claude Code: safety first, then performance, then developer experience; a limit on everything; lots of assertions. Part of the Starter pack.

    claude plugin install tiger-style@agent-kit

## Parts

A skill and nothing else, so it costs one line of context until Claude loads it. It loads when you ask ("write this in tiger style", "tiger style review") or name one of its rules (the 70-line function limit, units in names).

| File | What it holds |
|---|---|
| `skills/tiger-style/SKILL.md` | The protocol: reference mode (writing code, the default), review mode (auditing a diff) and the review's report format |
| `references/principles.md` | Every rule, with its reason |
| `references/review-checklist.md` | What review mode walks through |
| `references/<language>-rules.md` | One per language: Python, TypeScript, Rust, Go, C, C++, Java, Lua and fish. Claude reads only the one for the code at hand |
| `references/upstream/` | TigerBeetle's original guide, unchanged, with its licences. Zig uses it directly |

Writes no files. [`pre-push`](../pre-push/README.md) asks for its review mode before a branch's first push.

Back to the [plugin list](../../README.md#plugin-docs).
