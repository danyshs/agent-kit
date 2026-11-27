rule: single-evidence-ledger

# Single evidence ledger

Notes that argue with each other make every new session work out which is current.

- **One home per fact.** Other files point to it, never restate it. Edit a status line at its home.
- **Current state, not history.** No notes about what's no longer true ("X was removed", "Done: abc123"); git holds that. Keep why a rule exists, and who decided what, when.
- **Date the claims a reader acts on:** when checked and against what, with the time from `date '+%Y-%m-%d %H:%M %z'`.
- **Evidence beats summaries:** code, git, ledgers and approved specs win over any note.

For research, debugging or reverse-engineering that runs past one session, keep a `FACTS.md` ledger. Read the `single-evidence-ledger` skill before creating or editing one, or a handoff or status doc.
