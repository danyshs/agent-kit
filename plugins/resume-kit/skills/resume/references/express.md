# /resume express

The loop with more agents per step and one question. `P`, `T`, `R` and `L` are as in `loop.md`, but start with `L start --express`.

## The one question

Before anything runs, show the estimate and ask once whether to go ahead:

- each pass is up to 50 agent runs: 8 variant runs, 4 scorer runs for each of the 8 variants, and one paired vote of up to 10 judge runs;
- up to 3 passes, after a fix phase of up to 25 scorer runs if the resume isn't ready yet;
- `AGENT_RUNS_MAX` 230 across everything for this posting.

No: stop. Yes: ask nothing more until `next` says `done` or `wait`.

## `fix-round`

As in `loop.md`. Express passes start from a ready resume.

## `express-pass`

Each pass gets a new folder `R = P/runs/express-<n>/`.

1. **The brief.** `evidence.py public <evidence> --out R/evidence-public.md` (confirmed, non-confidential facts only). Launch 8 `resume-kit:resume-variant` agents, variant numbers 1 to 8, each told only: the paths of `resume.json`, the last `report.md`, `R/evidence-public.md`, and its number. `L record-runs 8`.
2. **Build.** Save each reply to `R/v<k>/variant.json`. Apply at most 6 edits per variant to a copy of `T` in `R/v<k>/`, build and parse it. A variant whose reply fails its schema, whose build fails, or that fails the truth gate or any sharp check is dropped.
3. **Score.** Each survivor gets 4 `resume-scorer` runs with the `score.md` brief, saved as `R/v<k>/scorer-1.json` to `scorer-4.json`, then `vote.py tally --keep 3 --resume R/v<k>/resume.json --locks P/locks.json --out R/v<k>/voted.json` over the 4. `L record-runs` for each batch.
4. **Best.** The variant with the fewest counted majors, then the fewest nits. Ties go to the one with fewer edits, then the lower number. Counts only, never averaged scores.
5. **Paired vote.** The best against the current resume, exactly as in `loop.md` polish step 4.
6. **Winner?** No winner (none survived, or the vote said `drop` or `reject`): `L express-done --winner no`. A winner: it becomes the resume; run `check.md` steps 1 to 5 into `R/check/`, then `L express-done --winner yes --report R/check/report.json`, and note what the winner carried (its `carried` list).

## `done`

As in `loop.md`, with each pass's winner and what it carried.
