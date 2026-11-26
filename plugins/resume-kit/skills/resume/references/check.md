# /resume check

One report on the current resume for one posting. Rewrites nothing. `P` is the posting folder, `R` a new folder `P/runs/check-<YYYY-MM-DD-HHMM>/`, `T` the `.tex`.

1. **Build.** Run the `build` command from the settings with `{tex}` replaced by `T`, with a 120-second timeout. A failed build: show its last 20 lines and stop. The PDF is `T` with `.pdf`.
2. **Parse.** `resume_parse.py T --out R/resume.json`.
3. **Sharp checks**, each writing its own findings file (exit 1 only means it found something):
   - `evidence.py check R/resume.json <evidence> <pdf> --out R/evidence.json`
   - `lint.py R/resume.json <pdf> --settings .resumekit.json --out R/lint.json`
   - `kw_check.py P/keywords.txt <evidence> <pdf> --out R/keywords.json`
4. **Scorer runs.** Follow `score.md`: write the brief, launch 5 `resume-scorer` runs, save each reply, tally with `--keep 3` into `R/voted.json`.
5. **Report.** `report.py R/evidence.json R/lint.json R/keywords.json R/voted.json --resume R/resume.json --settings .resumekit.json --posting <name> --out R`.
6. **Reply.** Show `R/report.md` as it is: verdict, counts, fit, then the findings. Add nothing the report doesn't say, and don't soften a blocker. End with its next command.

A verdict of ready always rests on the 5 scorer runs, never on fewer: if a run can't be completed, the check ends with the error, not with a verdict.
