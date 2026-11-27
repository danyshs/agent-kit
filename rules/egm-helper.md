rule: egm-helper

# EGM helper: when to run the design-doc workflow

Needs Google's EGM plugin (github.com/google/elephant-goldfish-model); if it isn't installed, tell the user once and ignore this rule.

Use EGM when a task needs a design decision before code: a new feature or module, a change across several files, a data-shape or API change, a choice between tools or approaches, a migration. Not for a small, clear fix, a typo, a one-file mechanical edit, a command or a question. When unsure, use it.

When it fires, say so in your first line ("This is a design-doc task: doc first."), don't open implementation code, and read the `egm-helper` skill before drafting.
