# Setup details

The [README](README.md) covers the normal install. This page covers the two pieces that need a manual step, how to remove things, and where things are in this repo. Settings for each plugin are in its own README under `plugins/<name>/`.

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

## Removing things

- One module: `claude plugin uninstall <name>@agent-kit`
- Everything: uninstall each module listed by `claude plugin list`, then `claude plugin marketplace remove agent-kit`.

## Copying rules by hand

Every rule is also a plain file in `rules/`. You can paste one into your own `~/.claude/CLAUDE.md` instead of installing the plugin. This works for every rule except `attention-levels`, `guards`, `pre-push` and `rhino-owl`, which need their plugin's scripts. The `debugging-discipline`, `egm-helper` and `fork-pr-check` rules are short and point to a skill with the detail, so also copy `plugins/<name>/skills/<name>/` into `~/.claude/skills/`.

## Repo layout

| Path | What it is |
|---|---|
| `plugins/<name>/` | One folder per plugin, each with a README. |
| `rules/` | Every rule as a plain file, to read before installing or to copy by hand (above). |
| `.claude-plugin/marketplace.json` | The plugin list Claude Code installs from. |
| `snippets/statusline.sh` | The optional status line. |
| `bin/doctor` | Checks the tools the optional plugins need and lists what you installed. |
| `lint.sh` | The lint gate CI runs: ruff, pyright, shellcheck, fish and actionlint. |
| `SNAPSHOT` | The Claude Code version this was last tested with. |
| `tools/` | The eval and trajectory tools ([tools/README.md](tools/README.md)). |

Installed plugins live under `~/.claude/plugins/` on your machine.
