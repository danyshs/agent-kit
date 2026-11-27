# guards

Hooks that refuse a sub-agent launch over the budget you typed or above a model price ceiling, and stop a turn past a token cap. Part of the Starter pack.

    claude plugin install guards@agent-kit

Needs python3. Without it the hooks can't check anything, so a sub-agent launch is refused and other tool calls go through with a warning.

## Parts

| Part | File | When it runs |
|---|---|---|
| Rule | `rules/guards.1.json` ([plain copy](../../rules/guards.md)) | In context from the start of every session: how to propose a budget, and what to do when a guard refuses |
| Claude Code hook | `scripts/guard.py agent` | Before every sub-agent launch: the model ceiling, then the budget |
| Claude Code hook | `scripts/guard.py turn` | Before every tool call: the turn's token cap |
| Data | `scripts/prices.json` | Per-token API prices and the ceiling |
| Tests | `tests/test_guard.py` | `python3 -I plugins/guards/tests/test_guard.py`; add `--mutants` to check the tests catch planted bugs |

Spend is read from the session's own logs: each sub-agent's usage, priced from `prices.json`. A launch is refused when what's spent, plus the floors still held by earlier launches, plus a floor for the new launch is over the budget. Launches sent in one message all read the spend from before any of them ran, so each allowed launch holds its floor until its sub-agent's first reply lands, or 10 minutes pass.

## Settings

| What | How | Default |
|---|---|---|
| Sub-agent budget | Type `budget $5` or `budget off` as the first line of a message. Claude may also propose one, ending its reply with `proposed budget $N`; a plain "yes" approves it, up to $20 a session. A typed budget line wins over approved proposals, and the guard says so when it ignores them. | No budget line: launches stop at $10 a session |
| Turn cap | Type `turn cap 30m` or `turn cap off`. | 10 million tokens per turn |
| Model ceiling | The `ceiling` entry in `scripts/prices.json`. | Opus 5.5: anything priced above it is refused |

**When a guard stops Claude**, Claude stops calling tools, quotes the refusal line and waits. What to type next:

- Turn cap: any reply starts a new turn, and the count starts again from zero. For a long task, put `turn cap 30m` on the first line of a message; it holds for the rest of the session, up to `100m`. `turn cap off` lifts the cap for that one turn only.
- Budget: reply `yes` to a message whose last line is `proposed budget $N`, or put `budget $N` on the first line of a message to set the figure yourself. A typed budget wins over approved proposals.

## Files it writes

`guard-launches.jsonl` in the session's log folder, next to its `subagents/` folder: one line per allowed launch whose sub-agent hasn't replied yet, with the time and the floor it holds. Each launch rewrites it under a lock and drops lines older than 10 minutes. With `budget off` nothing is written. Everything else it reads from the session logs Claude Code already writes.

Back to the [plugin list](../../README.md#plugin-docs).
