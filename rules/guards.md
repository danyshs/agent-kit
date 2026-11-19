rule: guards

# Guards: budget, model ceiling and turn cap

This module's hooks check every sub-agent launch and every tool call. A refused call comes back with a line starting `guard:` that says why and how to fix it.

- **Budget.** With a budget set, a sub-agent launch that would take spending past it is refused. Without one, launches go through with a note. The user sets one by typing `budget $N` (or `budget off`) as the first line of a message. You can also propose one: end your reply with the line `proposed budget $N`, and a plain "yes", "go" or "sure" from the user approves it. Approved proposals add up to at most $20 a session; past that, ask the user to type `budget $N`.
- **Model ceiling.** A launch priced above the `ceiling` model in `prices.json` (Opus, as shipped) is refused.
- **Turn cap.** One turn stops at 10 million tokens unless the user types `turn cap 30m` (or `turn cap off`).
- When a guard refuses: stop calling tools, tell the user what has been spent, what's unfinished and the refusal line, and wait.
- Before a fan-out, say what's been spent this session, what you'll add, and the worst case.
