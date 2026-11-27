# antagonist-protagonist

Before you approve a spec, plan or rule file, one reviewer argues against it and another checks the fixes didn't go too far. You get one table to decide from. Part of the Starter pack.

    claude plugin install antagonist-protagonist@agent-kit

Works with or without [`egm-helper`](../egm-helper/README.md); with it, these passes run after EGM's review.

## Parts

| Part | File | When it runs |
|---|---|---|
| Rule | `rules/antagonist-protagonist.1.json` ([plain copy](../../rules/antagonist-protagonist.md)) | In context from the start of every session: when the passes run, in what order, and who decides |
| Skill | `skills/antagonist-protagonist/SKILL.md` | Claude reads it before launching either reviewer: the brief template, the final table and the report |
| Agent | `agents/antagonist.md` | Read-only. Asks whether this should be done at all, or this way |
| Agent | `agents/protagonist.md` | Read-only. Gets the same references, the antagonist's points and Claude's answer to each, and argues the original position from the code, or says "this pushback is right" |

## How a pass runs

1. Claude writes the plan.
2. The antagonist argues against it, from the doc, its diff and the files it cites. It never gets Claude's own reasoning.
3. Claude checks each claim against the code, concedes or holds it, and updates the doc. You aren't asked anything yet.
4. The protagonist reads the updated doc and looks for over-correction.
5. Claude checks those claims too, then shows you the final table, one row per point. Where the two disagree and the code doesn't settle it, the row is yours to decide.

## Files it writes

`pushback_report.md` next to the reviewed doc.

Back to the [plugin list](../../README.md#plugin-docs).
