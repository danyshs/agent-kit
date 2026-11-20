rule: rhino-owl

# Rhino-Owl: find out what's true by building throwaway prototypes

Design-doc work is for building. Rhino-Owl is for finding out what's true when only running
things can settle it. Several "rhinos" each build a quick prototype down one direction; then a
fresh "Owl" turns what they found, failures included, into ledger rows. Nothing they build is
kept.

## When it applies

Only running things can settle the question. Before proposing it, name the sources you read and
why none of them settle it: sparse, untrusted, contradicted, or buried in forum noise. If reading
would settle it, read. Why: a hardware question that forums called impossible turned out to be
answered inside closed-source vendor code, which couldn't be read, so it took direct probes.

It ends at ledger rows. Building from them is a new task, and if it needs a design, it gets a
design doc with the Owl's rows as cited evidence.

## Roles

- **You (the invoking agent):** write the launch sheet, show it to the user as a table, get the
  budget approved, run the launcher, relay `human-queue.md` requests, and remove the worktrees
  after the user accepts the Owl's rows.
- **Rhinos:** at most 4 throwaway runs, each down one direction, almost no planning. **Controls**
  go down a route expected to be weaker, so comparisons rest on facts. Failures are findings.
- **Owl:** a fresh run after every rhino has stopped. Writes unnumbered fact, derivation,
  hypothesis and test rows. It never picks a winner.

## Running it

Read the `rhino-owl` skill before writing a launch sheet: it has the sheet format, limits and
statuses. The launcher is `python3 "@PLUGIN_ROOT@/scripts/rhino.py"` with `launch`, `owl` or
`remove`. It needs a budget the guards plugin can see (the user types `budget $5`).

## Hard lines
- The launcher is the only way to start a rhino or the Owl. Nothing blocks a bare `claude -p`;
  don't use one.
- Rhinos aren't bound by licences or build-blind rules: their code dies with them. Whoever builds
  from the rows must not have opened a rhino's worktree, except its `human-queue.md`.
- Devices have no hard fence; "no lasting changes" is prompt text. Device rhinos run only with
  the user present.
