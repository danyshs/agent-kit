rule: agent-orchestration

# Sub-agents and orchestration

Pick the cheapest structure that fits the task. "One agent does everything" and "spawn a swarm for everything" are both beginner moves.

## The ladder: climb only as high as the task forces you

1. **One agent, one tool call at a time.** Most tasks end here.
2. **One agent, parallel tool calls.** The most underused lever. If calls don't depend on each other, send them in one batch. This is where most real speed-ups come from, not from spawning agents.
3. **Routing.** Send easy work down a cheap path and hard work down an expensive one.
4. **Parallel subtasks, or the same task several times for confidence.** Often the sweet spot before true multi-agent.
5. **A lead plus workers (sub-agents).** The lead splits the work and hands it to sub-agents with their own context. Use it when subtasks can't be predicted up front, or the combined reading would overflow one context.
6. **Generator plus critic, in a loop.** Only when the criteria are clear and iteration measurably helps.

## When to spawn sub-agents

Spawn them only when all three hold:

- the work is breadth-first: several independent leads that can be chased at once;
- the combined reading would flood your context with detail the final answer doesn't need;
- each subtask has a clean boundary and returns a short summary (about 1-2k tokens), not a raw dump.

Don't spawn them when every part needs the same shared context or tight back-and-forth (most coding is like this), when parallel tool calls already fix the speed problem, or when you could just do the thing yourself.

Cost: an agent loop runs about 4 times the tokens of a chat turn, a multi-agent fan-out about 15 times. Only pay that when the task is worth it.

## How to brief a sub-agent

Every brief carries a sharp objective, the shape of the answer you want back, what's in and out of scope, and which tools or sources to use. "Go research X" returns overlap and holes. For broad codebase sweeps where only the conclusion matters, use a read-only explore agent and treat its answer as your own reading: don't re-read what it already returned.

If a brief sets a stop or budget condition ("stop over N dollars"), say how it's implemented, and have the agent see it trip on a known-bad input first, failing closed.

## Loops and fan-outs

- One reviewer per pass. A stop rule that needs two reviewers to agree is a bad stop rule.
- Every loop states its round cap. Only the user raises it. Report when blockers hit zero or the cap is reached.
- Before a fan-out, list each agent's model and round cap, and the worst-case cost: rounds x (start-up cost + your guess of the work, marked as a guess), priced by model. Tool-call caps written in a brief are not enforced.
- Under about 100 words of brief and 5 tool calls, do it yourself instead.
- At most 4 launches per message.
- If you also use the guards module: end a reply that proposes paid launches with the line `proposed budget $N`. A plain "yes" approves it.

Every sub-agent brief carries: "Model: <model>. Round cap: <rounds>; only the user raises it. Return findings, not fixes, in under <words> words."

## Context is a budget, not free space

The more tokens in the window, the worse recall gets. Keep the smallest set of high-signal tokens. Cheapest levers first:

- **Load on demand.** Hold paths, queries and links, and read the content when you need it.
- **Isolate heavy reading in a sub-agent.** Only the distilled result comes back. This is the main reason to delegate.
- **Take notes.** On long tasks, keep progress and decisions in a file so a fresh context can pick up.
- **Compact.** Summarize near the limit: keep decisions, open bugs and recent files; drop old tool output.

## Long work: one piece at a time, clean state each pass

When a task spans more than one context window, finish one feature or unit at a time, and leave a clean, mergeable state after each (committed, documented, nothing half-built). Keep a checklist the agent edits as JSON; Markdown checklists get mangled more often. Never delete tests or criteria to make something pass. Verify end to end, the way a real user would.
