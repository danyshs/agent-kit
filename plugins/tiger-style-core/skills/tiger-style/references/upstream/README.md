# Upstream sources

Raw material for the per-language rules files, collected 2026-10-19 by searching
GitHub (`gh search repos` / `gh search code` for tigerstyle, tiger-style,
tiger_style, plus Rust and C++ variants). Nothing here is a rules file itself;
`tiger-style-<language>` files are written from these, and verified on the real
toolchain, when a project in that language first needs one.

Only content whose license allows redistribution is copied in. Repositories without
a license are all-rights-reserved by default, so they are listed by link only:
read them for ideas, write our own wording.

## Copied in (Apache-2.0; license text in `LICENSE-Apache-2.0`)

| File | Source | Notes |
|---|---|---|
| `TIGER_STYLE.md` | tigerbeetle/tigerbeetle `docs/TIGER_STYLE.md`, commit `ba8d4b347cbb29057fd52243d2909b7a830f9336` (2026-07-16), unmodified | The original. TigerStyle is written for Zig, so this **is** the Zig variant: use it directly as the Zig rules until a `zig-rules.md` is needed. |

Considered and left out:
- Baba01hacker666/tigerstyle `examples/rust_canonical.rs` (commit
  `a5431c576d8c258b94027b8f25d98f9d1f680fe0`): **does not compile** on rustc 1.99.1.
  It derives `Default` on a struct holding `[u8; 34]`, but std only implements
  `Default` for arrays up to 32 elements (E0277). With that fixed, its layout asserts
  hold (64 bytes, 64-byte aligned), but clippy then flags a missing `Default` impl
  for `EventQueue`, and the code uses `usize` throughout, which TIGER_STYLE.md says
  to avoid. Its ideas (fixed ring buffer, compile-time layout asserts, bounded loop
  with an iteration cap, paired asserts) are worth reusing when writing
  `tiger-style-rust`, in a version that builds.
- Baba01hacker666/tigerstyle `examples/zig_canonical.zig`: its comments claim
  cache-line alignment while its own assert checks 16-byte alignment, its padding
  comment contradicts the layout, and it asserts `size == 80 or size == 72` (a
  compound, unpinned assertion that TigerStyle itself forbids).
- Baba01hacker666/tigerstyle `examples/c_canonical.c`: C99, not C++.

## Link only (no license — do not copy text)

| Language | Repository | What it is |
|---|---|---|
| C++ | https://github.com/wyattgill9/CPPStyle | "TigerBeetles TIGERSTYLE but for C++": a single README. The only C++ variant found. |
| Zig | https://github.com/zigcc/skills (`zig-tiger-style/`) | Agent skill for TigerStyle in Zig 0.15–0.17. |
| Zig | https://github.com/M64GitHub/tiger-style | Claude Code skill; SAFETY / PERFORMANCE / DX / CHECKLIST files, examples checked against Zig 0.16. |
| Rust | https://github.com/nerdsane/redis-rust (`docs/adr/003-tigerstyle-coding-standards.md`, `.claude/agents/tigerstyle.md`) | A real Rust project's TigerStyle adoption decision and review agent. The closest thing to a Rust variant found. |
| Rust | https://github.com/skel84/allocdb | Deterministic Rust database built in TigerStyle; worth reading for patterns, no style doc. |
| Go | https://github.com/solvicode/Go-Tiger-Style | Go adaptation (not requested, noted for later). |
| Swift | https://github.com/skills404/tigerswift | Swift / SwiftUI adaptation (not requested, noted for later). |

No dedicated Rust rules document with a usable license was found.

## Quoted, not copied whole

| Source | Used in | Notes |
|---|---|---|
| https://github.com/Vhivi/Powerof10-NASA (MIT), `rules/Powerof10-C.md`, commit `29f6f3975bba9c6d6430f8638d8d561786b04c26` | `tiger-style-rust` (each of the 10 rules quoted) | Transcription of Holzmann's "The Power of 10" (NASA/JPL, IEEE Computer 2006; original PDF https://spinroot.com/gerard/pdf/P10.pdf). Also has a Python adaptation, `rules/Powerof10-Python.md`. No Rust or C++ adaptation. Its rule 5 says a failed assertion should trigger recovery; TigerStyle says crash. The skill follows TigerStyle and notes the conflict. |
| https://wiki.kevinliu.studio/wiki/style/tiger-style ("TigerStyle (Dedalus)"), read 2026-10-19 | SKILL.md: "When to apply", "Proof", rule-contract format | A short digest of TigerStyle for Dedalus's Rust systems code. No Rust-specific rules. |

## Other licensed material, not copied

- https://github.com/simonklee/tigerstyle (tigerstyle.dev): a rewrite of the guide,
  CC-BY-4.0 for its changes and Apache-2.0 for the original parts. Language-neutral
  wording; could be quoted with attribution if useful.
- https://github.com/Baba01hacker666/tigerstyle (Apache-2.0): a "universal" agent
  skill with safety / performance / experience references and a Python lint script.

## Updating

Re-fetch with `gh api repos/<owner>/<repo>/contents/<path> --jq .content | base64 -d`,
record the new commit SHA here, and diff against the copy before replacing it.
