---
name: resume
description: Checks and improves a LaTeX resume against a job posting. "/resume check" gives one report with no rewrites; "/resume" runs the fix-then-polish loop on the newest posting; "/resume <posting>" picks one; "/resume express" runs the cheaper automatic pass. Needs /resume-setup to have run once.
---

# /resume

One router; each form loads one reference from `references/` next to this file and follows it.

## Paths

- `S` is the plugin's `scripts/` folder: `../../scripts` from this skill's base directory. Use the absolute path.
- The resume folder is the working directory. `K` is the settings file `<resume>.resumekit.json` next to the `.tex`; it records `runner`, `evidence`, `build`, `page_rule` and `mode`. Two `.tex` files with settings and none named: ask which, once.
- Run every script through the recorded runner: `uv run S/<script>.py ...` for `uv`, `.resumekit-venv/bin/python S/<script>.py ...` for `python`.
- Each posting is a folder `postings/<name>/` holding `posting.txt`, `keywords.txt` and `locks.json`; rounds write under its `runs/`.

## Every form starts here

1. No `K`, or no evidence file at the path it names: stop and say "Run /resume-setup first."
2. Run `evidence.py lint <evidence>` then `evidence.py validate <evidence>`. Any problem: show the lines and stop with "Run /resume-setup to fix the evidence file first."
3. Pick the posting: the one named, else the folder under `postings/` whose `posting.txt` changed last. Two equally new and none named: ask which, once. None at all: say how to add one (`postings/<name>/posting.txt`, the job ad as plain text) and stop.
4. A script that exits 2 printed what was wrong with its input: show that line and stop. Exit 3 printed the install command: show it and stop. Never retry a script past one rerun, and never edit the friend's `.tex` to get past a script.

## The forms

| Form | Reference |
| --- | --- |
| `/resume check` | `references/check.md` |
| `/resume`, `/resume <posting>` | `references/loop.md` |
| `/resume express` | `references/express.md` |

If the reference the form needs isn't there, say that form isn't built in this version and offer `/resume check`.

Every report ends with the one command to run next, which `report.py` writes on its last line. Show it as the last line of your reply.
