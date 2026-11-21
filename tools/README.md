# tools: evals and trajectory analysis

Two command-line tools, built with `uv`. Fork them and point them at your own rules and sessions.
This copy is generated from my private setup; change things in your fork.

## Before you start

- `uv` and Python 3.12 or newer, and `claude` on `PATH`. `kiro-cli` is optional.
- Each tool's `bin/check` is a fish script and also needs `fish`, `ruff`, `pyright` and `jq` on
  `PATH`.
- Keep both folders side by side: a trajectory test compares its helpers with the evals copy.

## evals

Runs the agent on each test case with and without a rule or skill, grades what it did, and appends
one line per run to a ledger under `evals/ledger/` (created on the first run).

    cd tools/evals
    bin/check        # lint, types, tests and their floors
    bin/eval --help

- **Run the shipped suite.** `eval` copies a skill from `<home>/skills/<name>/`, not from the
  plugin cache. Copy `plugins/tiger-style-core/skills/tiger-style` into `~/.claude/skills/` first.
  Then run the `skills/tiger-style` suite at the `smoke` tier. Every tier has a cap per run and
  per suite in `limits.json`: the `standard` tier (3 repeats) stops a Claude suite at $30.
- **Add your own rule.** Put the rule in `~/.claude/steering/<name>.md` and import it from
  `~/.claude/CLAUDE.md` with a line `@~/.claude/steering/<name>.md`. Then copy
  `steering/shell/` to `steering/<name>/` and rewrite its cases for your rule.
- **What a Claude run can see.** Only its throwaway project folder and home. File tools are
  limited to those two folders, and the agent's shell runs in the CLI's sandbox with your home
  folder denied for reading (uv's Python installs under `~/.local/share/uv` are the one exception).
  Each suite starts with a quick check that a planted file in `~/.cache` can't be read; if it
  can, the suite refuses to start. So a tool installed elsewhere under `~` can't run inside the
  agent's shell, and the agent can't write to git. Graders run outside the sandbox and aren't
  affected. Kiro runs don't get this isolation.
- **What's skipped here.** 27 tests replay recorded CLI sessions or need a home with the skill
  installed. The recordings stay private, so those tests skip with a message saying what's missing.
  `bin/check` allows only those reasons, up to `skipped_max`; lower it as you add recordings.

## trajectory

Reads the session logs the CLI already writes, `~/.claude/projects` by default, and writes one
summary row per session plus a weekly report.

    cd tools/trajectory
    bin/check        # lint, types and tests
    bin/traj --help
