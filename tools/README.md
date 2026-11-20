# tools: evals and trajectory analysis

Two command-line tools, built with `uv`. Fork them and point them at your own rules and sessions.
This copy is generated from my private setup; change things in your fork.

## Before you start

- `uv` and Python 3.12 or newer, and `claude` on `PATH`. `kiro-cli` is optional.
- `trajectory/bin/check` is a fish script and also needs `ruff`, `pyright` and `jq` on `PATH`.
- Keep both folders side by side: a trajectory test compares its helpers with the evals copy.

## evals

Runs the agent on each test case with and without a rule or skill, grades what it did, and appends
one line per run to a ledger under `evals/ledger/` (created on the first run).

    cd tools/evals
    uv run --with pytest python3 -m pytest -q    # the tool's own tests
    bin/eval --help

- **Run the shipped suite.** `eval` copies a skill from `<home>/skills/<name>/`, not from the
  plugin cache. Copy `plugins/tiger-style-core/skills/tiger-style` into `~/.claude/skills/` first.
  Then run the `skills/tiger-style` suite at the `smoke` tier, whose caps are in `limits.json`.
- **Add your own rule.** Put the rule in `~/.claude/steering/<name>.md` and import it from
  `~/.claude/CLAUDE.md` with a line `@~/.claude/steering/<name>.md`. Then copy
  `steering/shell/` to `steering/<name>/` and rewrite its cases for your rule.
- **What's skipped here.** About 30 tests replay recorded CLI sessions. Those recordings stay private,
  so the tests skip with a message saying which recording is missing. Record your own and they run.

## trajectory

Reads the session logs the CLI already writes, `~/.claude/projects` by default, and writes one
summary row per session plus a weekly report.

    cd tools/trajectory
    bin/check        # lint, types and tests
    bin/traj --help
