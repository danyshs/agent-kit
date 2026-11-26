---
name: resume-setup
description: Sets up resume-kit for one LaTeX resume, then builds or updates the evidence file every resume claim is checked against. Run it once before /resume; run it again to answer open questions or add new facts. "/resume-setup prefill" or "/resume-setup <path(s)>" drafts facts from sources for the friend to confirm.
---

# /resume-setup

`S` is the plugin's `scripts/` folder (`../../scripts` from this skill's base directory; use the absolute path). The resume folder is the working directory, `T` the resume's `.tex`, `K` its settings file `<resume>.resumekit.json` next to it. Ask in plain words. Before the first question, ask the friend how many questions they want per message (suggest 10; "1" keeps it one at a time) and use that batch size for the rest of the session. A batch is a numbered list they can answer by number in one reply. Tell them, with the first batch, the two words that answer any question:

- `SKIP`: not now. It isn't asked again this session; it stays open in the evidence file for the next `/resume-setup`.
- `REMOVE`: never ask it. Its `questions` line is deleted. A number question removed means no number (`is_measure: no`), a terms question removed means no `terms`, and a removed read-back drops that draft fact; list what was dropped in your next message so they can undo it. Every `REMOVE` also appends one line to `removed-questions.md` next to the evidence file (create it if missing), so a later session can ask why:

  ```
  - 2026-11-26 | acme-build-time | What is the number behind "cut build time"? | why: ?
  ```

  The second field is the fact ID, or `dropped: <the fact's text>` for a removed read-back. `why: ?` means not asked yet. Never delete a line from this file.

A question they didn't answer at all was likely missed, not skipped: it goes in the next batch.

Read-backs of facts you drafted (`references/build.md` step 3.1) use the multiple-choice question tool, not a typed list. The tool takes at most 4 questions per call, so a batch of 10 is three calls in a row (4, 4, 2). Each question shows the fact's text and its ID; the options are `Yes`, `No`, `Skip`, `Remove`, and the tool adds a free-text answer on its own. `Yes` accepts the text as written. `No` means it's wrong as written: keep it a draft, add the question "what's right here?" to its `questions`, and ask that in the next typed batch. Free text is their correction, in their words. `Skip` and `Remove` are `SKIP` and `REMOVE` above. Numbers, terms and open questions stay typed batches, since their answers are free text.

## Prefill: `/resume-setup prefill`, `/resume-setup <path(s)>`

Drafts facts from sources, the way `references/build.md` steps 1-2 do, then confirms them as above. `prefill` alone: ask which sources, as in build.md step 1. Paths: those files and folders are the sources; read nothing else.

Before reading anything, say what will happen and wait for a yes: "I'll read <each source>, draft one fact per claim a resume could make into <evidence file> as drafts, and ask you to confirm each one, 10 at a time. Your resume and your confirmed facts don't change. Go ahead?" Anything but a yes: stop.

No `K` yet: run first setup through step 4, then use these sources for step 5. `K` exists: run "Evidence file exists" step 1, draft from the sources (a claim already in the file is not drafted twice; a source that contradicts a confirmed fact becomes a `questions` line on that fact), then carry on from step 2.

## Evidence file exists

`K` exists and names an evidence file that is there: pick up where the last session stopped.

1. `evidence.py lint <evidence>`. Fix each problem at the line it names, rerun until clean.
2. `evidence.py todo <evidence>`: work through its draft facts and open questions with the friend, as in `references/build.md` step 3. In the same batches, for each `why: ?` line in `removed-questions.md`, ask why they removed that question, and write their words after `why:`. `SKIP` leaves it `?`; `REMOVE` writes `why: none given` and it is never asked again.
3. Ask: "Anything new to add since last time? A job, a project, a result, a link." Add each as in `references/build.md`.
4. `evidence.py validate <evidence>` passes, then say: "Done. Next: /resume check".

## No evidence file: first setup

### 1. Runner

- `uv --version` works: `runner` is `uv`. Scripts run as `uv run S/<script>.py`. Nothing to install.
- No `uv`: `python3 --version` must be 3.10 or newer; if not, say so and stop. Read `requirements.txt` next to this file and ask once: "Install <each line of requirements.txt> into a Python virtual environment in this folder (`.resumekit-venv`)?" Yes: `python3 -m venv .resumekit-venv` then `.resumekit-venv/bin/python -m pip install -r <abs requirements.txt>`. No: print those two commands and stop; no script runs without them. Then `runner` is `python`, and scripts run as `.resumekit-venv/bin/python S/<script>.py`.

Every later command goes through the recorded runner, never bare `python` or `uv`.

### 2. Settings

Find `T` (one `.tex` with `\documentclass`; several: ask which). Ask the two choices that change what gets checked (one batch, or one at a time if the batch size is 1):

- Page rule: `1` (one page, the default under about 10 years of experience), `2`, or `none`.
- Mode: `opinionated` (the shipped rules, the default) or `flexible` (the friend can add their own rulings in `rules.local.json`).

Write `K`:

```json
{"page_rule": 1, "mode": "opinionated", "evidence": "ACCOMPLISHMENTS.md", "runner": "uv", "build": "latexmk -pdf {tex}"}
```

`build` is the friend's own build command if they have one, with `{tex}` where the file goes.

In a git repo (`git rev-parse --git-dir` works), append `runs/` and `.resumekit/` to `<git dir>/info/exclude`, each only if that line isn't there yet. That file is never committed, so the friend's run files stay out of `git status` without a change to their `.gitignore`.

### 3. Markers

The markers tell the scripts where each section, entry and bullet is. They change nothing on the page, and step 3.5 proves that.

1. Copy `T` to `T.bak`. Build it (`build` from `K`, 120-second timeout) and copy the PDF to `before.pdf`. A failed build: show its last 20 lines and stop; markers can't fix a resume that doesn't build.
2. Copy `../../tex/resumekit.sty` next to `T` and add `\usepackage{resumekit}` to the preamble.
3. Wrap each of the template's own calls, never its text:
   - each section heading: `\rkSection{<kind>}{<the heading call>}`, kind one of `experience`, `research`, `projects`, `education`, `skills`, `other`; one section per kind;
   - each entry heading: `\rkEntry{<slug>}{<the entry heading call>}`, slug lowercase letters, digits and hyphens, unique (`acme-backend`);
   - each bullet: `\rkBullet{<the bullet call>}`;
   - each skills line, inside the skills section: `\rkSkillLine{<label>}{<the line call>}`.
4. `resume_parse.py ids T` fills each bullet's ID (`<slug>-b<k>`). IDs are never renumbered later.
5. Build again, then `lint.py body-font <pdf>` and `lint.py same-text before.pdf <pdf>`. Different text: copy `T.bak` back over `T`, show the diff, and stop. Text that appears twice means a template macro reads its argument twice and can't be wrapped: name that macro to the friend. Same text: write the body-font number into `K` as `"body_font_pt"`.
6. `resume_parse.py T --out resume.json` and show the sections and entries it found, one line each. Ask the friend once: "Is this every section and job on your resume?" No: fix the markers and repeat from 3.5.

### 4. The lint hook

Recommend it, say why, and let the friend decline: "Your agent will sometimes break the YAML in the evidence file. A hook runs the linter after every edit, so a broken line costs one line of output to fix instead of a long hunt. Add it?" Yes: add to the project's `.claude/settings.json` (merge, never overwrite):

```json
{"hooks": {"PostToolUse": [{"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "<runner> <abs S>/evidence.py lint --hook"}]}]}}
```

Hook or not: after every edit you make to the evidence file, run `evidence.py lint <evidence>`. Never read the YAML yourself to find a problem; the linter names the line.

### 5. The evidence file

Follow `references/build.md`. When `evidence.py validate` passes, say: "Setup done. Add a posting as postings/<name>/posting.txt, then run /resume check."
