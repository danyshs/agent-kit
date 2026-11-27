# agent-orchestration

When Claude should use sub-agents and when it shouldn't, how to brief one, and how to keep context small.

    claude plugin install agent-orchestration@agent-kit

## Parts

| Part | File | When it runs |
|---|---|---|
| Rule | `rules/agent-orchestration.1.json` ([plain copy](../../rules/agent-orchestration.md)) | In context from the start of every session |

Writes no files.

Back to the [plugin list](../../README.md#plugin-docs).
