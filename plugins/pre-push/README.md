# pre-push

Before a branch's first push (never `main` or `master`), asks you to confirm a [`tiger-style`](../tiger-style/README.md) review ran. It takes your word for it: a reminder, not a lock.

    claude plugin install pre-push@agent-kit

Needs git and `tiger-style`, plus a manual step: the git hook has to be copied into each repo you want it in. [SETUP.md](../../SETUP.md#pre-push) shows how.

## Parts

| Part | File | When it runs |
|---|---|---|
| Rule | `rules/pre-push.1.json` ([plain copy](../../rules/pre-push.md)) | In context from the start of every session |
| Git hook | `git-hook/pre-push` | git runs it on `git push`, only in repos you copied it into. It reads the refs being pushed, so it checks the branch you push, not the one you have checked out |

Claude can't type `yes` into the hook, so it runs the review, then pushes with `REVIEW_DONE=1 git push`. If the repo has its own checks in `.githooks/pre-push`, the hook runs them too.

Writes no files.

Back to the [plugin list](../../README.md#plugin-docs).
