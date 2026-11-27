rule: agent-orchestration

# Sub-agents and orchestration

Pick the cheapest structure that fits the task.

- Most tasks need one agent. The big speed-up is parallel tool calls: send calls that don't depend on each other in one batch.
- Spawn sub-agents only when the work is breadth-first, the combined reading would flood your context, and each part has a clean boundary and returns a short summary. Most coding needs shared context: do it yourself.
- An agent loop costs about 4 times a chat turn's tokens, a fan-out about 15 times.

Read the `agent-orchestration` skill before you spawn a sub-agent or plan a fan-out or loop: briefs, round caps, cost estimates and keeping context small.
