rule: antagonist-protagonist

# Antagonist and protagonist passes

Before the user approves a final spec, plan or rule file, two fresh reviewers argue about it. A reviewer who checks whether a doc is clear doesn't ask whether it's the right thing to build; the antagonist does. And a doc rewritten after one round of criticism tends to over-correct; the protagonist checks for that.

This rule says when the passes run, in what order, and who decides. The `antagonist-protagonist:antagonist-protagonist` skill says how: the briefs, the final table and the report. On any conflict, this rule wins.

## When

Any doc about to be marked final or approved: specs and plans. A rule file edit gets no pass by default; if the update isn't simple, ask the user whether there should be one. Not for running logs that never finalize (handoffs, notes, backlogs).

## Order

1. The doc.
2. The antagonist pass.
3. Your reply to the antagonist: check each claim against the code, concede or hold it, and apply the changes to the doc. Don't stop for the user here. Launching the two agents still follows any budget rule you have.
4. The protagonist pass, on the updated doc.
5. Your reply to the protagonist: check, apply, then show the user the final table and ask for approval.

Before launching either agent, load the `antagonist-protagonist:antagonist-protagonist` skill.

## Who decides

- Neither reviewer is an authority. Check their top claims against the code before you pass them on.
- Where the two disagree and the code doesn't settle it, the row is the user's call.
- One round each. Re-run a side only on points whose meaning changed, at most once. After that the user's call stands.
