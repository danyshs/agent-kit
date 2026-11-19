rule: code-voice

# Code voice

## Comments and writing

Terse, focused on why, blunt. Written for a colleague, not documentation for a stranger.

- Comments explain why, or label a section. Never restate what the code does.
- Short: a clause, or two lines at most. Paragraph comments are rare and earned.
- Blunt and conversational is fine: "no need to wait, event already happened", "hack to survive a bad save state, fix when X lands".
- Section labels above a block are terse, often lowercase: `-- early-out guards`, `-- wire the keymaps`.
- Trailing notes on magic numbers and odd calls: `wait(0.17) -- min input delay`.
- No banner boxes of `====`, no license headers, no line-by-line narration.
- In typed, linted languages, keep the type annotations and header docs. The prose stays terse; the types stay.

The same register applies in chat and PRs: direct, no filler, and say the uncomfortable thing plainly.

## No OOP by default

Prefer plain modules and data over class hierarchies.

- New code: plain modules, plain functions, plain tables or structs. No class system, no inheritance trees, no closures-as-architecture, unless the problem really is one.
- Ask: could this be a data entry plus a generic reader, instead of a new subsystem? Often the answer is a table key, not a class.
- When a dependency is already object-oriented, work with its grain. The preference is for code you write from scratch.
- Smallest thing that solves it, with explicit control flow. No scaffolding nobody asked for.
