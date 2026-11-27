# Sample eval output

Two ledgers from real paid runs and the reports `bin/eval report` prints for them, so you can
see what the tool produces without running `claude -p` yourself.

| File | What it is |
|---|---|
| `ledger/skill_tiger-style.jsonl` | 77 runs of the tiger-style skill suite on Claude Code (CLI 2.1.326, claude-haiku-5-5, 2026-11-20, $0.30 in all). One JSON line per run: case, arm, status, cost, and each grader's verdict with its evidence. |
| `skill_tiger-style.report.txt` | The report for that ledger: per-case pass counts with and without the skill, how often it loaded, review recall and precision, and a verdict per part. |
| `ledger/steering_shell.jsonl` | 30 runs of the fish-shell rule suite on Kiro's CLI (3.2.1, claude-haiku-5.5, 2026-11-12, $0.59), with the rule on, with it removed, and with no rules at all. |
| `steering_shell.report.txt` | Its ablation table: pass counts per arm, the mean difference with the exact two-sided sign-flip p, the cases that went each way when the rule was removed, and the verdict. |

Where a ledger has two lines for the same case, arm and repeat, the later one is a rerun and
replaces the earlier. Lines marked `contaminated` asked for a skill their arm didn't include
and are left out of the counts. The `stream_path` and `session_id` fields point at raw run
logs that aren't shipped.

The fish-shell rule itself is private, so the size columns in its report print `?`.

`tests/test_examples.py` renders both ledgers on every `bin/check` and fails if the output
differs from the saved reports, so they can't drift from the code.
