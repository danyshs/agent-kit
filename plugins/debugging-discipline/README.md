# debugging-discipline

Seven debugging rules, each from a real mistake. Example: exit code 0 doesn't prove a check passed.

    claude plugin install debugging-discipline@agent-kit

## Parts

| Part | File | When it runs |
|---|---|---|
| Rule | `rules/debugging-discipline.1.json` ([plain copy](../../rules/debugging-discipline.md)) | In context from the start of every session: the seven titles, and when to read the skill |
| Skill | `skills/debugging-discipline/SKILL.md` | Claude reads it when it debugs, fixes a type error or warning, or writes a check or stop condition: each rule in full, with its incident |

Writes no files.

Back to the [plugin list](../../README.md#plugin-docs).
