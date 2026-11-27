# attention-levels

Type `(ENABLE_LOW_ENERGY)` on a tired day and Claude asks fewer questions; `(ENABLE_HIGH_ENERGY)` for more.

    claude plugin install attention-levels@agent-kit

Needs python3. Without it the hook warns and the level stays Medium.

## Parts

| Part | File | When it runs |
|---|---|---|
| Rule | `rules/attention-levels.1.json` ([plain copy](../../rules/attention-levels.md)) | In context from the start of every session: how many questions to ask at each level |
| Claude Code hook | `scripts/attention.py` | On every message you send: reads the level you typed and adds a line like `Attention level: Low` |
| Helper script | `scripts/lowlog.py` | When Claude, at Low, makes a call it would otherwise have asked you about, it logs it |

## Settings

| What | How | Default |
|---|---|---|
| Attention level | Type `(ENABLE_LOW_ENERGY)`, `(ENABLE_MEDIUM_ENERGY)` or `(ENABLE_HIGH_ENERGY)` anywhere in a message. | Medium; `/clear` or a new session resets it |

Some things need your yes at every level: push or force-push, delete, spend over the budget, a model above the ceiling, raising a round cap, and final approval of a spec.

## Files it writes

`~/.claude/attention/` (the level, written only by the hook) and `~/.claude/lowlog/` (Claude's logged calls, for you to read later).

Back to the [plugin list](../../README.md#plugin-docs).
