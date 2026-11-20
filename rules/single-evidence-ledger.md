rule: single-evidence-ledger

# Single evidence ledger: where facts live, and how a claim becomes one

## Where facts live

Stale notes that argue with each other burn credits, because every new session re-derives
which one is current.

- Each fact has one home. Other files point to it; they don't restate it. Before writing a
  status line, find its home and edit there. A number or count cited by several docs lives
  in one tagged line; the others point to it. Why: a wrong count was once copied
  through a doc, tables and an approval before it was found to be wrong.
- Write current state, not history. No tombstones, i.e. notes about state that's no longer
  true: "X was removed / dropped / merged / superseded / decided against", "Done: committed
  abc123", struck-through text. Git holds what happened. When a claim stops being true,
  rewrite or delete it; don't annotate it.
- Not tombstones, keep them: why a rule exists (the incident behind it), and who decided
  something and when ("user, 2026-11-09").
- A status claim a reader would act on says when it was last checked and against what:
  "X is done (checked 2026-11-11 17:43 -0500 against git log at 9b8afc3)"; say "uncommitted" when
  the files checked had local changes. Take the time from
  `date '+%Y-%m-%d %H:%M %z'` at that moment, and never copy one check time onto claims you
  didn't each check. Decisions and rules keep their decision date instead.
- Precedence: evidence (code, git, ledgers, approved specs) beats any summary, dated or not.
  Between two summaries the more recently checked one wins, and an undated one loses to a dated one.
  A ledger row that points to a refuting row is not evidence.
- An edit queue for someone else (NOTES, proposed rows) is emptied once merged, not marked
  DONE. Open items keep their numbers so pointers to them still work.
- Scope: live docs, i.e. handoffs, resume files, briefs, rules, edit queues and running logs
  (HANDOFF, NOTES). Append-only records (evidence ledgers, below; backlogs, review reports,
  dated decision logs) keep their history, and their own rules (e.g. a backlog's "mark DONE, never delete") still apply.

## How a claim becomes a fact

When it applies: research, debugging or reverse-engineering work that runs past one session,
or where acting on a wrong belief would cost a rebuild. Not for small, clear fixes.

- One ledger file per project, `FACTS.md` at its root, never split. Its header has an
  `Owner:` line naming the session that numbers rows. Its rows are the facts' home under the rules
  above, so handoffs and specs cite row IDs.
- Four tables, one letter each. Each row gets one ID (F1, D7, ...), never reused or renumbered.
  - **F, Fact (observed):** claim | evidence (file:line, a log line, command output). What the user
    saw is evidence too, marked "user observation". If the evidence file will be overwritten, save a
    copy and cite the copy. Memory or hearsay goes in an H row, not an F row.
  - **D, Derivation:** conclusion | the F and D rows it rests on | the rule that connects them. A
    rule we supply ourselves is an assumption: give it a name (R-a, R-b, ...) and every
    conclusion that uses it carries it.
  - **H, Hypothesis (open):** what might be true | what would settle it. An H is never cited as
    a fact.
  - **T, Test:** the plan is written before the change is applied: the one variable changed |
    the result if the hypothesis is true | the result if it is false. The result row keeps the T's ID and
    says what it confirms or refutes.
- One variable per test. A test that changed two things settles neither.
- Append-only. A row is never deleted and its claim is never rewritten. When an H is refuted or
  an F turns out wrong, a new D or T row says so with its evidence ("H5 refuted"); the only
  change to the old row is a pointer to that new row, or a status tail on a T plan row
  ("done, F96").
- To learn from another repo, a GPL file or a decompiled binary, distill, then build blind. Whoever
  reads it writes only F rows in FACTS.md (offsets, flags, sizes, behaviour, each with its source),
  never code. A fresh agent that hasn't seen it writes the code from the rows it's given, and flags
  any fact it can't act on without guessing. This applies to private code too: plagiarism is wrong whoever
  sees it. Why: a probe was written in the same context that had just read the
  driver it learned from.
- Rhinos (the rhino-owl plugin) are exempt: their code is thrown away with them, so they may
  read and run anything. Only the Owl's rows survive, and whoever builds from those rows must not
  have opened a rhino's worktree.
- Parallel sessions don't assign IDs. They write unnumbered rows to a `proposed-ledger.md` next
  to their work; the ledger's owner numbers and merges them, so two sessions never take the same
  ID. That file is an edit queue (above).
