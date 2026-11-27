rule: code-voice

# Code voice

Comments and writing: terse, about why, blunt, for a colleague.

- Comments say why or label a section (`-- wire the keymaps`); never restate the code. A clause, two lines at most.
- Note magic numbers and odd calls: `wait(0.17) -- min input delay`. Blunt is fine: "hack, fix when X lands".
- No banner boxes, license headers or line-by-line narration.
- Typed, linted languages keep their type annotations and header docs.
- Chat and PRs: direct, no filler, the uncomfortable thing said plainly.

Code: plain modules, functions and data, not class hierarchies; often a data entry plus a generic reader beats a new subsystem. Work with the grain of an object-oriented dependency. Smallest thing that solves it, explicit control flow, no scaffolding nobody asked for.
