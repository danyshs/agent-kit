rule: attention-levels

# Attention levels: Low, Medium, High

The user sets how many questions they want by typing `(ENABLE_LOW_ENERGY)`, `(ENABLE_MEDIUM_ENERGY)` or `(ENABLE_HIGH_ENERGY)`. Nothing else changes it.

- This plugin's hook adds "Attention level: ..." to every prompt. That line is the truth; no line, or "(hook error)", means Medium, your normal behaviour. Never write `~/.claude/attention/` yourself.
- At every level, ask before pushing, deleting, spending past the budget, a costlier model than allowed, raising a round cap, and final approval of a spec.
- When the line says Low or High, read the `attention-levels` skill before your next question or decision. At Low, log each decision with `python3 @PLUGIN_ROOT@/scripts/lowlog.py`; the skill has its arguments.
