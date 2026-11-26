# /resume: the loop

Fix the resume until it is ready, then polish it until no change wins. `P` is the posting folder, `T` the `.tex`, `L` is `loop.py --state P/runs/state.json`. `loop.py` decides every stop: after each step below, run `L next` and do what its `action` says. Never decide on your own that the loop is done.

## Start

1. `evidence.py lint <evidence>` and `evidence.py validate <evidence>`. Any problem: show it and stop.
2. Show the estimate: up to 75 agent runs for this posting (fix phase up to 25 scorer runs, polish phase up to 50 judge runs), `AGENT_RUNS_MAX` 230 across everything, Express included.
3. `L start`. It refuses while a loop for this posting is still running: run `L next` and carry on from there instead.

After **every** agent launch, retries included, run `L record-runs <n>` with the number launched. Exit 2 with "AGENT_RUNS_MAX hit": launch nothing more, and run `L next`.

## `fix-round`

1. Each round gets a new folder `R = P/runs/fix-<n>/`. Run `check.md` steps 1 to 5 into `R`, with `--state P/runs/state.json` added to the `report.py` call.
2. `L fix-done R/report.json`.
3. If `next` says `fix-round` again, rewrite `T` from `R/report.md`, blockers first, then majors:
   - Every number, date, name and link you write comes from a confirmed fact in the evidence file. No fact, no claim: leave the finding for the friend and say so in your reply.
   - Need room? Cut in this order: redundant skill wraps, then beginner course badges (keep those on an early-career sheet when the only alternative is empty space). Never cut the only bullet backing a posting keyword, or a large production result.
   - Don't touch a location in `P/locks.json` unless the report says its condition changed.

## `polish-round`

Each round gets a new folder `R = P/runs/polish-<n>/`.

1. **Nits.** Fix the last report's nits the same way, rebuild, and rerun the sharp checks. Keep a nit fix only when the check no longer raises it and `evidence.py check` passes. Nit fixes are not votes and don't count as kept changes.
2. **One change.** Propose one change at one location: a bullet's wording, or one entry's order. One change only, so a bad edit can't ride along with a good one. Save the current `.tex`, PDF and `resume.json` as old, then apply, rebuild and parse into `R/new/`.
3. **Gates.** Run the truth gate and every sharp check on the new version. A failure drops the change (restore old) and the round counts as not kept.
4. **Paired vote.** `rules.py --brief --settings K > R/rules-brief.txt`. For run `i` = 1 to 10:
   - `vote.py judge-prep <old.json> <new.json> <old.pdf> <new.pdf> --seed <round x 100 + i> --out R/judge-<i> --posting P/posting.txt --rules R/rules-brief.txt`. Every run gets its own new folder; judge-prep refuses one that isn't empty.
   - Launch one `resume-kit:resume-judge` agent with only this brief: "Your folder is <abs R/judge-i>. Read only its six files and reply as your instructions say." Never a general-purpose agent: the judge must not be able to run commands or see another run's verdict.
   - Save its reply exactly as returned to `R/judge-<i>/verdict.json` (strip only a code fence). `L record-runs 1`.
   - After runs 5 and 10: `vote.py paired R/judge-*/verdict.json`. `drop` or `reject`: restore old. `keep`: the new version stays. `more`: run 6 to 10.
5. **Kept?** Not kept: `L polish-done --kept no`. Kept: run `check.md` steps 1 to 5 on the new version into `R/check/`, then `L polish-done --kept yes --report R/check/report.json`. If that brought back a blocker or major, `next` says `fix-round`: the fix count carries on from where it was.

## `wait`

The loop needs the friend (a missing fact, a failing build only they can fix). `L wait <reason>`, ask the friend in one plain sentence, and stop. When they answer: `L resume`, then `L next`.

## `done`

Show the last `report.md` with `next`'s `verdict` as the ending ("polished", "not ready, round cap reached", ...), and list each kept change in one line. The verdict from `next` wins over anything you think of the page.
