# fork-pr-check

In a fork, a bare `gh pr view` can look in the wrong repo and show nothing. Claude pins the upstream and names the repo before it says there's no PR or no review comments.

    claude plugin install fork-pr-check@agent-kit

## Parts

| Part | File | When it runs |
|---|---|---|
| Rule | `rules/fork-pr-check.1.json` ([plain copy](../../rules/fork-pr-check.md)) | In context from the start of every session: one paragraph |
| Skill | `skills/fork-pr-check/SKILL.md` | Claude reads it before saying a PR is absent: the queries to run |

Writes no files.

Back to the [plugin list](../../README.md#plugin-docs).
