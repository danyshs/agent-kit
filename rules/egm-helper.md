rule: egm-helper

# EGM helper: when and how to run the design-doc workflow

Requires Google's EGM plugin (github.com/google/elephant-goldfish-model). EGM's own skills say how each stage runs. This rule adds when to use it and the house rules around it. If EGM isn't installed, tell the user once that this rule needs it, then ignore the rest of this rule.

## The trigger

Use EGM by default for any task that needs a design decision before code: a new feature or module, a change across several files, a data-shape or API change, a choice between tools or approaches, a migration. Anything where "how should this work?" has more than one honest answer.

Not for: a small, clear bug fix, a typo, a one-file mechanical edit, running a command, or answering a question. When unsure, use it. A short doc is cheap; a wrong build isn't.

When it fires:

1. Say so in your first line ("This is a design-doc task: doc first.") and don't open implementation code.
2. Brainstorm, then draft, then review, as EGM's skills describe.
3. If the user asks for code on a task this size, redirect once, plainly. If they insist, comply, and say in one line what is being skipped and the risk. Don't nag.
4. If a session is deep into building something that never had a doc and it's going badly, stop, write the doc from what you know, review it, then resume.

## The doc header

Each design doc carries `Stage:` (brainstorm, draft, review, approved, in that order) and `Approved-by-user:` (the user's quoted message and its date, or "nothing yet"), each with the time it was last updated. That header is the one home for the stage. Replies point to it.

## Probe before review

Where the draft depends on a premise nobody has checked, run the cheapest probe that reads the real state (a header, a config file, one command) before reviewers see the draft, and cite its output in the doc. Machinery built on an unchecked premise is the expensive kind of wrong.

## Reviewers see only the doc

A reviewer gets the doc's path, its persona, and at most a reading list of files. Never a conversation summary, never "here's what we meant". If it matters, it goes in the doc. Reviewing the doc yourself, in your own context, is not a review.

## Limits

- One round per reviewer unless the user sets more. A review loop states its round cap in its brief (5 by default), and only the user raises it.
- Use a mid-size model for reviewers. A larger one only for the design critic.
- Findings come back to you. Reviewers never edit the doc or the code.

## After approval

- Save the approved spec under `docs/designs/`. Build it, and only it. Scope found mid-build goes back into the doc first.
- When sources disagree, the approved spec beats the draft, which beats the brainstorm brief. A user message after approval that changes the spec wins until the doc is updated, and the doc is updated before more code is written.
- If the code can't match the spec, report in this shape, then proceed unless it's a one-way door:
  `Mismatch in <step>. Expected: <what the spec says>. Found: <what the code shows>. Why it matters: <consequence>. Recommended: X, because Y.`
- A done-check only a human can make (a visual check, a test on a phone) is marked `PENDING-HUMAN (observation: <the one thing to look at>)`. It counts as not done until the user records what they saw. Never use it for a check you could run yourself.
- Before calling the work done, check whether the code drifted from the doc (EGM's drift report).

If you also use antagonist-protagonist, its passes run after the review and before approval.
