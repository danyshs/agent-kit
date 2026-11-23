rule: antagonist-protagonist

# Antagonist and protagonist passes

Before the user approves a final spec, plan or rule file, two fresh reviewers argue about it. A reviewer who checks whether a doc is clear doesn't ask whether it's the right thing to build; the antagonist does. And a doc rewritten after one round of criticism tends to over-correct; the protagonist checks for that.

## When

Any doc about to be marked final or approved: specs and plans. A rule file edit gets no pass by default; if the update isn't simple, ask the user whether there should be one. Not for running logs that never finalize (handoffs, notes, backlogs).

## Order

1. The doc.
2. The antagonist pass.
3. Your reply to the antagonist: check each claim against the code, concede or hold it, and apply the changes to the doc. Don't stop for the user here. Launching the two agents still follows any budget rule you have.
4. The protagonist pass, on the updated doc.
5. Your reply to the protagonist: check, apply, then show the user the final table and ask for approval.

## Briefing the two agents

Launch the `antagonist` and `protagonist` agents that come with this module. Both are read-only. Each gets:

- the doc and its git diff since the last approved version (the whole doc on a first version), pasted into the brief, not just their paths;
- an explicit list of the files the doc cites. The agent reads only those, and ends with the files it needed and didn't have; one re-run with those added is fine. A fixed list misses some findings a free search would catch, but it should spend far fewer turns re-reading what it gathered, and turns are where the cost is. The list is an instruction to the agent, not a permission fence, so check the turn count;
- none of your own reasoning: no conversation summary, no list of what you already fixed. If it matters, it belongs in the doc.

The protagonist also gets the antagonist's pushbacks, as plain claims. Never your replies or verdicts. With no git history, hand over a list of the points added and say that you wrote it, since a hand-picked list lets you choose the targets.

## What comes back

- Antagonist: one line per point, opening with a tone marker ([strong], [medium] or [weak]) and then its own sentence, then a ranked top 5 with the full argument.
- Protagonist: for each pushback, one line opening with a tone marker, then the original position argued from the code, or "this pushback is right". Never an invented defence. Then anything the doc is still too weak on.

Neither is an authority. Check their top claims against the code before you pass them on.

## The final table

One row per point either side raised:

| point | antagonist | protagonist | checked against code (y/n) | your read | recommendation |
|---|---|---|---|---|---|

- Each side's cell quotes that reviewer's sentence with its tone marker, never a paraphrase: condensed cells lose the argument and the reviewer's voice. A reviewer that gave no marker shows [—]; don't invent one.
- Don't drop a row. Mark any row where the protagonist only echoes you.
- Where the two disagree and the code doesn't settle it, the row is the user's call.
- Write the table, with a verdict per row (adopt, reject or defer), to `pushback_report.md` next to the doc.

## Limits

- One round each. Re-run a side only on points whose meaning changed, at most once. After that the user's call stands.
- A mid-size model is enough for both agents.
