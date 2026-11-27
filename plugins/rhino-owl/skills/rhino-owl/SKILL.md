---
name: rhino-owl
description: How to run a Rhino-Owl exploration: the launch sheet format, the launcher's commands, what it checks and doesn't, and where results land. Read before writing a launch sheet.
---

# Running Rhino-Owl

The launcher's path is in the rhino-owl rule; below, `rhino.py` means that path.

## Roles

- **You (the invoking agent):** write the launch sheet, show it to the user as a table, get the
  budget approved, run the launcher, relay `human-queue.md` requests, and remove the worktrees
  after the user accepts the Owl's rows.
- **Rhinos:** at most 4 throwaway runs, each down one direction, almost no planning. **Controls**
  go down a route expected to be weaker, so comparisons rest on facts. Failures are findings.
- **Owl:** a fresh run after every rhino has stopped. Writes unnumbered fact, derivation,
  hypothesis and test rows. It never picks a winner.

## Hard lines

- The launcher is the only way to start a rhino or the Owl. Nothing blocks a bare `claude -p`;
  don't use one.
- Rhinos aren't bound by licences or build-blind rules: their code dies with them. Whoever builds
  from the rows must not have opened a rhino's worktree, except its `human-queue.md`.
- Devices have no hard fence; "no lasting changes" is prompt text. Device rhinos run only with
  the user present.

## The launch sheet

JSON, next to the work (e.g. `rom/<topic>.json`):

```json
{"owl_model": "sonnet", "owl_cap_usd": 2.0,
 "rhinos": [{"name": "hid-claim", "question": "...", "direction": "...", "axis": "...",
             "label": "frontier", "stop_point": "...", "model": "sonnet", "cap_usd": 1.0,
             "device": "pixel"}]}
```

- `axis`: what this rhino differs on. `label`: `frontier` (best guess) or `control`.
- `stop_point`: "works, shown by <output>" or "fails at <named call> with the captured error".
  The launcher only checks it's there; making it checkable is on the writer.
- `device` is optional. Rhinos sharing one run one after another; the rest run in parallel.
- `scaffolding` is optional and off by default; see below before using it.

## Rhinos start bare on purpose

Rhinos run with no CLAUDE.md, rule files, settings, hooks, plugins or MCP servers. That is what
keeps them cheap: a rhino's cap goes on building the prototype, not on reading instructions at
every turn. Leave it that way unless the user asks for rules in their rhinos.

If the user insists, add `scaffolding` to the sheet. Only rhinos get it; the Owl always starts
bare, and the results file records what the rhinos loaded so the Owl knows.

```json
"scaffolding": {"setting_sources": ["project"], "rule_files": ["rom/rhino-rules.md"]}
```

- `setting_sources`: any of `user` (everything under `~/.claude/`: your CLAUDE.md and what it
  imports, settings, hooks, and the plugins you've enabled, rule plugins included), `project` (the
  repo's CLAUDE.md and `.claude/settings.json`) and `local` (`.claude/settings.local.json`).
- `rule_files`: up to 8 text files, 64 KB each and 128 KB together, paths relative to the sheet.
  Their text is added to every rhino's system prompt. Prefer this to `user`: it loads only the
  rules a rhino needs, not your whole setup.
- MCP servers stay off either way. The sandbox, deny list and dollar cap still apply.

**Raise the caps to match.** Everything loaded is paid for at each rhino's start, out of its own
cap. A bloated start can spend most of a rhino's cap before it builds much of a prototype, and it
stops `capped` with little to show. The launcher prints a notice when scaffolding is on; raise
each `cap_usd` unless the user has said not to.

## Commands

```
python3 rhino.py launch --sheet <sheet> --session <main session id> [--user-present]
python3 rhino.py owl    --sheet <sheet> --session <main session id> [--user-present]
python3 rhino.py remove --sheet <sheet>
```

Exit 0 ok, 1 a run failed or its spend couldn't be counted, 2 refused.

## What the launcher checks, and what it doesn't

Checked (refuses with exit 2):
- 1-4 rhinos, unique names (`[a-z0-9-]`, up to 32), every field present, each under 2000
  characters, label `frontier` or `control`.
- Every cap, the Owl's too, between $0.50 and $10.
- Every model at or below the ceiling, and the caps' total within what the budget line or the
  approved proposal has left after counted spend (the guards plugin's rules).
- A device on any rhino needs `--user-present`, for `launch` and `owl`.
- A rhino whose worktree already exists. To relaunch it, `remove` first or rename it.
- `owl` needs the results file, which `launch` writes only after every rhino has stopped.
- `remove` needs the Owl's status `done` and the evidence folder present.

Each run: `claude -p` with a dollar cap, a 30-minute wall clock, the OS sandbox (writes only
under its working directory), a deny list (Agent, sudo, git push, defaults write), and no
CLAUDE.md, rules, hooks or MCP servers, user's or project's. Its transcript is copied to
`<main session>/rhinos/`, where `guard.py` counts it.

Not checked: two launches at once both see the full budget; devices are fenced only by the
prompt; nothing blocks a bare `claude -p`.

## Where things land

- Rhino worktrees: `<repo>/.claude/worktrees/rom-<name>`, branched from committed HEAD
  (uncommitted work is invisible to rhinos). Each holds `rhino.log` and maybe `human-queue.md`.
- `<sheet>.results.json`: per rhino `done`, `waiting-human`, `capped` (no result, H rows at
  most), `crashed`, or `unknown` (no exact `STATUS:` last line); then the Owl's entry.
- The Owl runs at the repo root, not in a worktree (it writes the ledger). The sandbox keeps it
  inside the repo; "write no kept code, delete nothing" is its brief, not a fence.
- A capped or crashed Owl exits 1. Its rows so far stand; run `owl` again (a fresh Owl, new cap)
  and it overwrites the results file's Owl entry. `remove` stays refused until one finishes.
- Owl output, next to the sheet: `proposed-ledger.md` and `rom-evidence/<sheet name>/`.
