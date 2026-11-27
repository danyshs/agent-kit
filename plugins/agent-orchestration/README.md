# agent-orchestration

When Claude should use sub-agents and when it shouldn't, how to brief one, and how to keep context small.

    claude plugin install agent-orchestration@agent-kit

## Parts

| Part | File | When it runs |
|---|---|---|
| Rule | `rules/agent-orchestration.1.json` ([plain copy](../../rules/agent-orchestration.md)) | In context from the start of every session: when one agent is enough, and when to read the skill |
| Skill | `skills/agent-orchestration/SKILL.md` | Claude reads it before spawning a sub-agent or planning a fan-out or loop: the ladder of structures, briefs, round caps and worst-case cost |

Writes no files.

Back to the [plugin list](../../README.md#plugin-docs).
