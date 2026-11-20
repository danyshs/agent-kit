---
name: rhino-owl
description: How to run a Rhino-Owl exploration: the launch sheet format, the launcher's commands, what it checks and doesn't, and where results land. Read before writing a launch sheet.
---

# Running Rhino-Owl

The launcher's path is in the rhino-owl rule; below, `rhino.py` means that path.

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
