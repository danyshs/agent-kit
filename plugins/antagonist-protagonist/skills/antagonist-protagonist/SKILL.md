---
name: antagonist-protagonist
description: How to run the antagonist and protagonist passes on a spec or plan before approval: when they run, the order, the brief template, who decides, the final table and pushback_report.md. Load before launching either reviewer.
---

# Running the antagonist and protagonist passes

A reviewer who checks whether a doc is clear doesn't ask whether it's the right thing to build; the antagonist does. A doc rewritten after one round of criticism tends to over-correct; the protagonist checks for that. What each reviewer returns (tone markers, a ranked top 5, the files it needed) is in its agent file, `antagonist` and `protagonist`, which come with this plugin. Both are read-only, and a mid-size model is enough for both.

## When

Any doc about to be marked final or approved: specs and plans. A rule file edit gets no pass by default; if the update isn't simple, ask the user whether there should be one. Not for running logs that never finalize (handoffs, notes, backlogs).

## Order

1. The doc.
2. The antagonist pass.
3. Your reply to the antagonist: check each claim against the code, concede or hold it, and apply the changes to the doc. Don't stop for the user here. Launching the two agents still follows any budget rule you have.
4. The protagonist pass, on the updated doc.
5. Your reply to the protagonist: check, apply, then show the user the final table and ask for approval.

## Who decides

- Neither reviewer is an authority. Check their top claims against the code before you pass them on.
- Where the two disagree and the code doesn't settle it, the row is the user's call.
- One round each. Re-run a side only on points whose meaning changed, at most once. After that the user's call stands.

## The briefs

Fill the brief template below once per side.

- **REFERENCES** is one block, pasted unchanged into both briefs, so the comparison is fair. A file added for one side is added for both. It holds:
  - the doc and its git diff since the last approved version, pasted in, not just their paths (the whole doc on a first version). With no git history, hand over a list of the points added and say that you wrote it, since a hand-picked list lets you choose the targets;
  - the tiger-style principles file, if the `tiger-style` plugin is installed (a reviewer can't load skills); otherwise leave that line out;
  - an explicit list of the files the doc cites. The agent reads only those, and ends with the files it needed and didn't have; one re-run with those added is fine. A fixed list misses some findings a free search would catch, but it should spend far fewer turns re-reading what it gathered, and turns are where the cost is. The list is an instruction to the agent, not a permission fence, so check the turn count.
- Before launching, check that every path you hand over exists (one `ls` of the whole list) and fix or drop any that doesn't: a dead path costs the agent turns hunting for it, or comes back as a bogus finding.
- **Antagonist:** REFERENCES only. None of your own reasoning: no conversation summary, no list of what you already fixed. If it matters, it belongs in the doc.
- **Protagonist:** REFERENCES with the doc as updated after the antagonist round, plus the antagonist's pushbacks and your response to each (concede or hold, and what changed), so it can also judge whether you conceded too much.

## The final table

Fill the final-table template below. One row per point either side raised; don't drop one.

- Each side's cell quotes that reviewer's verdict in full, with its tone marker: its own sentence, never a paraphrase or a clipped "concede.". Condensed cells lose the argument and the reviewer's voice. A reviewer that gave no marker shows [—]; don't invent one.
- Mark any row where the protagonist only echoes you.
- Put a ★ after the row number on every row where the two disagree, with the legend line under the table.

Write the table, with a verdict per row (adopt, reject or defer), to `pushback_report.md` next to the doc.

## Brief template

```text
Fill once per side. The REFERENCES block is identical in both briefs.

### REFERENCES

<one of: "Its git diff since <rev> is below." | "No history: a first version, attack every point." |
"No history: I wrote this list of the points added, so I chose the targets: ...">

<document path="<doc path>">
<the doc, pasted in>
</document>

<diff>
<git diff of the doc since the last approved version>
</diff>

<only if the tiger-style plugin is installed:>
Tiger-style principles file (read it; you can't load skills): <path to its SKILL.md>

Files you may read (read only these; every path was checked to exist):
- <path>

### PROTAGONIST ONLY (leave this section out of the antagonist's brief)

<antagonist_report>
<the antagonist's report, unedited>
</antagonist_report>

<response>
<per pushback: concede or hold, what was checked, and what changed in the doc>
</response>

### Closing lines

Return findings, not fixes, in under 1,500 words.
```

## Final-table template

| # | Point | Antagonist | Protagonist | Checked against code (y/n) | Your read | Recommendation |
|---|---|---|---|---|---|---|
| 1★ | <the point, a few words> | [strong] "<the antagonist's full verdict sentence>" | [medium] "<the protagonist's full verdict sentence>" | y | <concede or hold, and what changed> | adopt / reject / defer |
| 2 | <point> | [—] not raised | [weak] "<sentence>" *(echoes you)* | n | <read> | <recommendation> |

★ = the antagonist and protagonist disagree. *(echoes you)* = the protagonist only repeated your position.
