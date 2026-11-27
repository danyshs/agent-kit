---
name: attention-levels
description: What changes at the Low and High attention levels: the table of ask-points per level, and how to record decisions at Low. Read when the "Attention level" line says Low or High, before your next question or decision.
---

# Attention levels: what changes

The attention-levels rule says how the level is set and what you always ask at every level. This is the rest.

- The level lasts for the session. `/clear` or a new session starts at Medium.
- It applies to the main agent only. Sub-agents report back; their choices reach the user through you.
- Medium is your normal behaviour. If you also use when-to-ask, Medium is exactly its defaults.

## The table

| Ask-point | Low | Medium | High |
|---|---|---|---|
| Final approval of a spec | Ask once; mark the rows you decided yourself | Table in chat, a verdict per row | As Medium |
| Review findings to act on | Resolve each, record the choice | User picks | User picks |
| A new design fork | Decide, record, go on | "I'd do X because Y", proceed unless it's a one-way door | Ask and wait |
| Reversible work | Proceed | Recommend and proceed | Ask |
| Ambiguous target | Pick a reading, record it | Ask once with your reading, proceed if no answer | Ask and wait |
| Done-definition unclear | Use the repo's own bar, record it | Ask once up front | Ask |
| Work contradicts an approved spec or a file the user wrote | Stop and ask | Stop and ask | Stop and ask |
| The user is physically needed (a password, sudo, a phone test) | Stop | Stop | Stop |
| Implementation steps and commits | Proceed | Proceed | State each step and wait; show the diff before each commit |

## Recording at Low

- A spec line you changed starts with `[LOW]`, so it doesn't read as user-approved. A default the spec doesn't cover is written in as an `Assumed:` line.
- Log each decision with one call to the `lowlog.py` path the rule names:
  `python3 <that path> append --repo <git top level> --session <id from the hook line> --ask-point TEXT --options TEXT --choice TEXT --reason TEXT --spec-edited yes|no --product-facing yes|no`
  On exit 2, fix the arguments and retry once. On exit 3 (log full), carry on, and list the unlogged decisions in your closing message.
- Never read the log (`~/.claude/lowlog/`) unless the user asks. It's the user's record for judging your choices later.
- End the task with one line: "N assumptions written to <spec path>".
- At Low, ctrl+c is the user's check: stop, say what you were doing, and wait.
