# Setup details

The [README](README.md) covers the normal install. This page covers the two pieces that need a manual step, and how to remove things.

## Pre-push

The `pre-push` plugin gives Claude the rule. The git hook itself has to be copied into each repo you want it in. From inside that repo, first check you don't already have one:

```
ls .git/hooks/pre-push
```

If that says "No such file", install it:

```
curl -fsSL https://raw.githubusercontent.com/danyshs/agent-kit/main/plugins/pre-push/git-hook/pre-push -o .git/hooks/pre-push
chmod +x .git/hooks/pre-push
```

If you already have a pre-push hook, or your repo uses Husky or `core.hooksPath`, this copy won't run. Move your own checks to `.githooks/pre-push` and this hook runs them for you.

On the first push of a new branch (never `main` or `master`), the hook asks whether a tiger-style review has run, and waits for `yes`. Claude can't type into it, so it pushes with `REVIEW_DONE=1 git push` after running the review. To skip the hook once, use `git push --no-verify`.

If the repo has its own checks in `.githooks/pre-push`, this hook runs them too.

## Status line

Needs bash, jq and git.

1. Copy the script: `curl -fsSL https://raw.githubusercontent.com/danyshs/agent-kit/main/snippets/statusline.sh -o ~/.claude/statusline.sh`
2. Make it runnable: `chmod +x ~/.claude/statusline.sh`
3. Open `~/.claude/settings.json` and add this inside the outer `{ }` (add a comma after the line before it if needed):

```json
"statusLine": { "type": "command", "command": "~/.claude/statusline.sh" }
```

Restart Claude Code.

## Guards: changing the limits

- Budget: type `budget $5` (or `budget off`) as the first line of a message.
- Turn cap: type `turn cap 30m` (or `turn cap off`).
- The model ceiling is the `ceiling` entry in the plugin's `scripts/prices.json`.

## Turn sound

Install jq and ffmpeg for the per-session sounds (`brew install jq ffmpeg` on a Mac, `sudo apt install jq ffmpeg` on Ubuntu). Without ffmpeg, a Mac plays a system chime and Linux stays silent.

## Removing things

- One module: `claude plugin uninstall <name>@agent-kit`
- Everything: uninstall each module listed by `claude plugin list`, then `claude plugin marketplace remove agent-kit`.

## Copying rules by hand

Every rule is also a plain file in `rules/`. You can paste one into your own `~/.claude/CLAUDE.md` instead of installing the plugin. This works for every rule except `attention-levels`, `guards` and `pre-push`, which need their plugin's scripts.
