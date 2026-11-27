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

## Running it

Read the `rhino-owl` skill before proposing a run: it has the roles, the hard lines, the launch
sheet format, limits and statuses. The launcher is `python3 "@PLUGIN_ROOT@/scripts/rhino.py"`
with `launch`, `owl` or `remove`. It needs a budget the guards plugin can see (the user types
`budget $5`).
