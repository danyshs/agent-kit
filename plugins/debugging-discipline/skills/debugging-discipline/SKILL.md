---
name: debugging-discipline
description: The seven debugging rules in full, each with the incident behind it. Read when debugging, fixing a type error or linter warning, or writing a check, gate or stop condition.
---

# Debugging and type-fixing discipline, in full

The debugging-discipline rule lists these seven by title. Each is here with what to do and the incident it came from.

## 1. Read the dependency's own types before searching the web

When typing code against a library, or working out how to call it, the library's own type definitions come first: ahead of docs, web search and issue trackers. It has usually solved its own version of your problem already.

- Before a web search, grep the dependency for its annotations on the symbol: `@class`, `@type`, `@overload` in Lua, `.d.ts` files in TypeScript, type stubs in Python.
- Incident: a long detour through a type checker's issue tracker for a UI library's constructor type. The answer ("the class, or its constructor function") was in the library's own source the whole time.

## 2. A partial win does not reset "two tries, then change tracks"

If an approach fails twice, change tracks. A partial success (the error count dropped a bit) tempts you to keep tuning the same idea.

- After two changes that haven't fully fixed the error class, write one sentence before a third: "what would I do if this approach were off the table?"
- A metric moving partway is not evidence the approach is right. It can be the wrong seam giving a partial result. Incident: a shared base class cut type warnings from 301 to 256, and that partial win kept it alive for hours; the real fix was each module's export type.

## 3. State the code fact that makes a diagnostic true before fixing it

A linter or compiler points at a symptom, not the cause.

- For each warning, write one line of what is actually true in the code there (what this function takes, what this module exports, what the real call passes) before touching it.
- Incident: an "extra argument" warning was "fixed" by annotating the function it pointed at, when a different call was the one passing the extra argument.

## 4. Make the check a gate, not a side quest

A quality metric checked only by hand regresses.

- Commit a check script that runs the real check, prints results by category, and exits non-zero when the count passes a recorded baseline. Compare by message, not line number, so a moved line can't hide a new problem.
- A baseline is for inherited debt and only goes down. New code starts at zero (rule 6).
- Run it in the one done-gate, with formatting and tests.

## 5. Exit code 0 is not proof: assert on the output

Many tools print a fatal error and still exit 0: headless editors, interpreters in script mode, `set +e` shells, `cmd | tee`, `cmd; echo done`.

- When a gate's pass condition is "no errors", capture stdout and stderr and assert they're empty, or that they match the expected success line.
- State the pass condition in words ("startup prints nothing"), then check exactly that. "Tests pass" means the pass line is present and the fail count is 0.
- Incident: an editor config crashed on every launch, but its headless startup check still exited 0. The gate stayed green and the broken config shipped. The error was on stderr the whole time.

## 6. Warnings are defects, and static types are mandatory

- Keep warnings at zero. Once "a few known ones" are tolerated, a real new one hides in the pile.
- Don't silence a warning unless you can say in one line why the tool is wrong here. Scope the suppression to that one spot and write the reason beside it.
- Annotate types at every boundary (function parameters and returns, module exports, anything crossing a file) in any typed and linted language.
- Read the warning's reasoning before dismissing it. Most "false positives" are the tool being right about something you hadn't noticed.

## 7. A stop or budget guard must be seen to trip, and must fail closed

- Before relying on a stop condition (a cost cap, a loop bound, a kill switch), run it once on an input that should trip it, and watch it trip.
- If the check itself errors, stop. In a shell `if`, an erroring check reads as false and the loop runs on.
- For unattended paid loops, read the output on every pass, not once at the end.
- Incident: a "stop over 7 credits" check compared numbers with fish's `math`, which can't compare. It errored on every pass, read as false, and nothing stopped.
- A gate says what it measured and fails when that shrinks. A glob matching no files, or a disabled rule, makes a check pass by seeing nothing. Point the gate at an empty scope once and watch it fail, and have it print its file or rule count on every run.
