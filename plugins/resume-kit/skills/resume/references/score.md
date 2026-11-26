# Scorer runs and the vote

`R` is the round's folder and already holds `resume.json` and the built PDF's path.

## The brief

1. `rules.py --fuzzy --settings .resumekit.json > R/rules-fuzzy.txt`.
2. If `P/locks.json` doesn't exist, write `{"locks": []}` there.
3. The brief, the same for all 5 runs, is only these lines (no summary of the resume, no opinion of yours, nothing from this conversation; the scorer judges the page alone):

   ```text
   Score this resume against this posting. Read only these files:
   - rules: <abs R>/rules-fuzzy.txt
   - posting: <abs P>/posting.txt
   - resume: <abs R>/resume.json
   - PDF: <abs pdf>
   - locks: <abs P>/locks.json
   Reply with the JSON object your instructions describe, and nothing else.
   ```

## The runs

4. Launch 5 `resume-scorer` agents with that brief, in parallel where you can.
5. Save each reply exactly as returned to `R/scorer-1.json` ... `R/scorer-5.json`. If a reply is wrapped in a code fence, strip only the fence.
6. `vote.py tally --keep 3 --resume R/resume.json --locks P/locks.json --out R/voted.json R/scorer-1.json ... R/scorer-5.json`.
7. Exit 2 with "scorer output invalid: <file>": launch that one run again with the same brief, save over the file, and tally again. A second invalid reply from the same run ends the check: report "scorer output invalid" and the file, with no verdict.

The tally keeps a point only when 3 of the 5 runs raised it at the same rule and location, and sets its severity from the rule, so one run's taste never reaches the report.
